"""On-board VLM benchmark (SPEC P5): latency, tokens/s, RAM and power per triage request.

Sends the robot's real on-board triage prompt (``a6g_robot_agent.perception``) with camera
images to an OpenAI-compatible endpoint, by default the ``onboard_vlm`` role of
``config/models.yaml``, and writes one CSV row per request.

On the Go2 dock (Jetson), with the llama.cpp server running and ``go2_nav`` up:

    uv run python bench/onboard_vlm.py --url http://127.0.0.1:8080/v1 --model qwen2.5-vl-3b \\
        --images /data/camera_frames --runs 20 --server-pid "$(pgrep -f llama-server)"

Elsewhere (development) it runs against whatever ``onboard_vlm`` points at:

    uv run python bench/onboard_vlm.py --runs 5

Columns: ``latency_s`` (request to last token), ``ttft_s`` (first token), prompt and
completion tokens, ``decode_tok_s`` (completion tokens / time after the first token),
``output_tok_s`` (completion tokens / latency; use this one for reasoning models, whose
hidden reasoning tokens come before the first visible token), ``valid`` (the
reply parses as a ``TriageVerdict``), ``notable``, ``server_rss_mb`` (``--server-pid``),
``system_used_mb``, ``power_w`` (mean over the request: ``tegrastats`` on Jetson, RAPL on
x86 when readable; empty otherwise). Without ``--images``, synthetic 640x480 frames (the
D435 resolution) are used: realistic image-token cost, meaningless content.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import io
import re
import shutil
import statistics
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from pathlib import Path

import psutil
from a6g_common.config import ModelRole, load_config
from a6g_common.models import _parse, load_env
from a6g_robot_agent.perception import _ONBOARD_SYSTEM, TriageVerdict
from langchain_core.messages import HumanMessage, SystemMessage

ROOT = Path(__file__).resolve().parents[1]
INSTRUCTION = (
    "Robot go2_01 at POI poi_kiosk. Answer with a JSON object with the fields notable (bool), "
    "label (short string), novel (bool), confidence (0-1) and reasoning (one sentence)."
)


@dataclass
class Row:
    run: int
    stamp: str
    model: str
    endpoint: str
    image: str
    image_kb: float
    latency_s: float
    ttft_s: float | None
    prompt_tokens: int | None
    completion_tokens: int | None
    decode_tok_s: float | None
    output_tok_s: float | None
    valid: bool
    notable: bool | None
    server_rss_mb: float | None
    system_used_mb: float
    power_w: float | None
    error: str


# --------------------------------------------------------------------------- power


class Power:
    """Mean board power over an interval: tegrastats (Jetson) or RAPL (x86), if available."""

    def __init__(self) -> None:
        self.source = "none"
        self._samples: list[tuple[float, float]] = []
        self._proc: subprocess.Popen[str] | None = None
        self._rapl = Path("/sys/class/powercap/intel-rapl:0/energy_uj")
        if shutil.which("tegrastats"):
            self.source = "tegrastats"
            self._proc = subprocess.Popen(
                ["tegrastats", "--interval", "100"], stdout=subprocess.PIPE, text=True
            )
            threading.Thread(target=self._read, daemon=True).start()
        elif self._readable(self._rapl):
            self.source = "rapl"

    @staticmethod
    def _readable(path: Path) -> bool:
        try:
            int(path.read_text())
            return True
        except (OSError, ValueError):
            return False

    def _read(self) -> None:
        assert self._proc and self._proc.stdout
        # Total board input: VDD_IN (Orin) or POM_5V_IN (Xavier/Nano), "<now>mW/<avg>mW".
        pattern = re.compile(r"(?:VDD_IN|POM_5V_IN) (\d+)mW")
        for line in self._proc.stdout:
            m = pattern.search(line)
            if m:
                self._samples.append((time.monotonic(), int(m.group(1)) / 1000.0))

    def start(self) -> tuple[float, int | None]:
        energy = int(self._rapl.read_text()) if self.source == "rapl" else None
        return time.monotonic(), energy

    def mean(self, mark: tuple[float, int | None]) -> float | None:
        t0, e0 = mark
        if self.source == "rapl" and e0 is not None:
            dt = time.monotonic() - t0
            de = int(self._rapl.read_text()) - e0
            return round(de / 1e6 / dt, 2) if dt > 0 and de >= 0 else None
        if self.source == "tegrastats":
            vals = [w for t, w in self._samples if t >= t0]
            return round(statistics.fmean(vals), 2) if vals else None
        return None

    def close(self) -> None:
        if self._proc:
            self._proc.terminate()


# --------------------------------------------------------------------------- images


def synthetic_images(n: int = 3) -> list[tuple[str, bytes]]:
    """640x480 JPEG frames with some structure (plaza, facade, an object)."""
    from PIL import Image, ImageDraw

    out = []
    for i in range(n):
        img = Image.new("RGB", (640, 480), (135, 170, 205))
        d = ImageDraw.Draw(img)
        d.rectangle([0, 300, 640, 480], fill=(150, 150, 140))  # ground
        d.rectangle([60 + 40 * i, 120, 420, 300], fill=(190, 175, 150))  # facade
        for k in range(4):
            d.rectangle([90 + 80 * k, 160, 130 + 80 * k, 220], fill=(70, 90, 110))  # windows
        d.ellipse([430 - 30 * i, 330, 500 - 30 * i, 400], fill=(40, 40, 45))  # an object
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=85)
        out.append((f"synthetic_{i}.jpg", buf.getvalue()))
    return out


def load_images(folder: str | None) -> list[tuple[str, bytes]]:
    if not folder:
        return synthetic_images()
    paths = sorted(p for p in Path(folder).iterdir() if p.suffix.lower() in (".jpg", ".jpeg"))
    if not paths:
        raise SystemExit(f"no .jpg images in {folder}")
    return [(p.name, p.read_bytes()) for p in paths]


# --------------------------------------------------------------------------- requests


def build_model(args: argparse.Namespace):
    """The onboard_vlm role of models.yaml, with --url / --model overrides."""
    import os

    load_env()
    env = dict(os.environ)
    if args.url:
        env["A6G_ONBOARD_VLM_URL"] = args.url
    if args.model:
        env["A6G_ONBOARD_VLM_MODEL"] = args.model
    cfg = load_config(str(ROOT / "config"), env=env)
    endpoint = cfg.models.roles[ModelRole.ONBOARD_VLM]
    from a6g_common.models import chat_model

    model = chat_model(ModelRole.ONBOARD_VLM, cfg.models, env)
    if hasattr(model, "stream_usage"):  # OpenAI-compatible: token counts in the stream
        model.stream_usage = True
    return model, endpoint.model, endpoint.base_url or "provider cloud API"


async def one_request(model, jpeg: bytes) -> dict:
    b64 = base64.b64encode(jpeg).decode()
    messages = [
        SystemMessage(_ONBOARD_SYSTEM),
        HumanMessage(
            content=[
                {"type": "text", "text": INSTRUCTION},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]
        ),
    ]
    started = time.perf_counter()
    first: float | None = None
    text, usage = "", None
    async for chunk in model.astream(messages):
        piece = chunk.text if isinstance(chunk.text, str) else chunk.text()
        if piece and first is None:
            first = time.perf_counter() - started
        text += piece or ""
        if getattr(chunk, "usage_metadata", None):
            usage = chunk.usage_metadata
    latency = time.perf_counter() - started
    try:
        verdict = _parse(text, TriageVerdict)
    except Exception:
        verdict = None
    return {"latency": latency, "ttft": first, "usage": usage, "verdict": verdict}


async def run(args: argparse.Namespace) -> Path:
    model, model_id, endpoint = build_model(args)
    images = load_images(args.images)
    server = psutil.Process(args.server_pid) if args.server_pid else None
    power = Power()
    out = (
        Path(args.out)
        if args.out
        else ROOT
        / "bench"
        / "results"
        / (
            f"onboard_vlm_{re.sub(r'[^A-Za-z0-9.-]+', '_', model_id)}_"
            f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.csv"
        )
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"model {model_id} at {endpoint}; {len(images)} image(s); power: {power.source}")
    rows: list[Row] = []
    try:
        for i in range(args.warmup):  # load the model / caches; not recorded
            await one_request(model, images[i % len(images)][1])
        for i in range(args.runs):
            name, jpeg = images[i % len(images)]
            mark = power.start()
            error = ""
            try:
                r = await one_request(model, jpeg)
            except Exception as exc:
                r, error = (
                    {"latency": float("nan"), "ttft": None, "usage": None, "verdict": None},
                    f"{type(exc).__name__}: {exc}"[:200],
                )
            usage = r["usage"] or {}
            completion = usage.get("output_tokens")
            gen_time = r["latency"] - (r["ttft"] or 0.0)
            vm = psutil.virtual_memory()
            row = Row(
                run=i + 1,
                stamp=datetime.now(UTC).isoformat(timespec="seconds"),
                model=model_id,
                endpoint=endpoint,
                image=name,
                image_kb=round(len(jpeg) / 1024, 1),
                latency_s=round(r["latency"], 3),
                ttft_s=round(r["ttft"], 3) if r["ttft"] is not None else None,
                prompt_tokens=usage.get("input_tokens"),
                completion_tokens=completion,
                decode_tok_s=round(completion / gen_time, 1)
                if completion and gen_time > 0
                else None,
                output_tok_s=round(completion / r["latency"], 1)
                if completion and r["latency"] > 0
                else None,
                valid=r["verdict"] is not None,
                notable=r["verdict"].notable if r["verdict"] else None,
                server_rss_mb=round(server.memory_info().rss / 2**20, 1) if server else None,
                system_used_mb=round((vm.total - vm.available) / 2**20, 1),
                power_w=power.mean(mark),
                error=error,
            )
            rows.append(row)
            print(
                f"  {row.run:3d} {name:22s} {row.latency_s:7.2f} s  ttft {row.ttft_s}  "
                f"{row.completion_tokens} tok  {row.output_tok_s} tok/s  valid={row.valid}"
                + (f"  {error}" if error else "")
            )
    finally:
        power.close()
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[fl.name for fl in fields(Row)])
        w.writeheader()
        w.writerows(asdict(r) for r in rows)
    ok = [r for r in rows if not r.error]
    if ok:
        lat = sorted(r.latency_s for r in ok)
        tps = [r.output_tok_s for r in ok if r.output_tok_s]
        pw = [r.power_w for r in ok if r.power_w is not None]
        p90 = lat[int(0.9 * (len(lat) - 1))]
        print(
            f"median latency {statistics.median(lat):.2f} s, p90 {p90:.2f} s"
            + (f", output {statistics.median(tps):.1f} tok/s" if tps else "")
            + f", valid {sum(r.valid for r in ok)}/{len(ok)}"
            + (f", power {statistics.fmean(pw):.1f} W" if pw else "")
        )
    print(f"CSV: {out}")
    return out


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--url", help="OpenAI-compatible base URL (default: onboard_vlm in models.yaml)")
    p.add_argument("--model", help="model name (default: onboard_vlm in models.yaml)")
    p.add_argument("--images", help="folder of .jpg camera frames (default: synthetic 640x480)")
    p.add_argument("--runs", type=int, default=10)
    p.add_argument("--warmup", type=int, default=1)
    p.add_argument("--server-pid", type=int, help="model server process, for its RSS")
    p.add_argument("--out", help="CSV path (default bench/results/onboard_vlm_<model>_<time>.csv)")
    asyncio.run(run(p.parse_args(argv)))


if __name__ == "__main__":
    main()
