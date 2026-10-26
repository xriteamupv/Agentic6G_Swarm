"""``a6g comms report``: the traffic of one mission, per message and per pattern."""

from pathlib import Path
from typing import Any

from a6g_common.config import load_config
from a6g_observability.comms import load_records, mission_records, model_summary, summarize


def default_logs(config_dir: str | Path | None = None) -> list[Path]:
    """The configured message log plus every stack's own (``var/<stack>/comms.jsonl``, and
    the previous run's ``comms.prev.jsonl``)."""
    paths: list[Path] = []
    try:
        comms = load_config(config_dir).comms
        if comms is not None and comms.metrics.path:
            paths.append(Path(comms.metrics.path))
    except Exception:  # the report also works without a valid config
        pass
    paths += sorted(Path("var").glob("*/comms.jsonl"))
    paths += sorted(Path("var").glob("*/comms.prev.jsonl"))
    return list(dict.fromkeys(paths))


def _fmt(value: Any, width: int) -> str:
    text = "-" if value is None else (f"{value:.1f}" if isinstance(value, float) else str(value))
    return text.rjust(width)


def render(
    mission_id: str, paths: list[Path], *, messages: bool = True
) -> tuple[str, list[dict[str, Any]]]:
    records = mission_records(load_records(paths), mission_id)
    if not records:
        return "", []
    lines = [f"Mission {mission_id}: traffic per interaction pattern (SPEC A2.5.2)", ""]
    header = (
        f"{'pattern':8}{'messages':>9}{'payload B':>11}{'NL B':>7}{'wire B':>10}"
        f"{'e2e ms':>9}{'p95 ms':>9}{'failed':>8}  bindings"
    )
    lines.append(header)
    rows = summarize(records)
    for r in rows:
        lines.append(
            f"{r['pattern']:8}{_fmt(r['messages'], 9)}{_fmt(r['payload_bytes'], 11)}"
            f"{_fmt(r['nl_bytes'], 7)}{_fmt(r['wire_bytes'], 10)}{_fmt(r['e2e_ms_mean'], 9)}"
            f"{_fmt(r['e2e_ms_p95'], 9)}{_fmt(r['failed'], 8)}  {r['bindings']}"
        )
    total_wire = sum(r["wire_bytes"] for r in rows)
    lines.append(
        f"{'total':8}{_fmt(sum(r['messages'] for r in rows), 9)}{'':28}{_fmt(total_wire, 10)}"
    )
    models = model_summary(records)
    if models:
        lines += ["", "Model calls (rule 23; inference time apart from network time):"]
        lines.append(
            f"{'function':14}{'calls':>6}{'failed':>8}{'tokens in':>11}{'tokens out':>12}"
            f"{'mean ms':>9}{'p95 ms':>9}  prompt"
        )
        for m in models:
            lines.append(
                f"{m['function']:14}{_fmt(m['calls'], 6)}{_fmt(m['failed'], 8)}"
                f"{_fmt(m['input_tokens'], 11)}{_fmt(m['output_tokens'], 12)}"
                f"{_fmt(m['inference_ms_mean'], 9)}{_fmt(m['inference_ms_p95'], 9)}  {m['prompt']}"
            )
    if messages:
        lines += [
            "",
            "Messages (send / reply; recv = one subscriber; fleet = state of its robots):",
        ]
        t0 = records[0]["ts"]
        for r in records:
            if r["direction"] in ("recv", "model"):
                continue  # receptions: latency columns; model calls: the table above
            to = ",".join(r.get("receivers") or [])
            lines.append(
                f"{r['ts'] - t0:8.2f}s {r['pattern']:3} {r['direction']:5} "
                f"{r['schema']:15} {r['sender']:>11} -> {to:30.30} "
                f"{_fmt(r.get('payload_bytes'), 6)} B {_fmt(r.get('wire_bytes'), 6)} B wire "
                f"{_fmt(r.get('e2e_ms'), 7)} ms {r.get('outcome') or ''}"
                + (" [fleet]" if r.get("scope") == "fleet" else "")
            )
    return "\n".join(lines), records
