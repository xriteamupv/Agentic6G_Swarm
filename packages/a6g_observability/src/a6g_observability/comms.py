"""Per-message communication metrics (SPEC A2.2 rule 23) and the per-mission traffic report.

Every inter-agent message produces one record, written by the messaging layer:

* ``send``: a request or publication leaving its sender (pattern, performative, binding,
  payload and wire bytes, serialization and queueing time; for requests also network and
  end-to-end time, retries and the outcome);
* ``reply``: the reply to a request, recorded by the requester (it knows both directions);
* ``recv``: a publication arriving at one subscriber, with its end-to-end latency.

Records go to an in-memory ring (tests), to a JSON-lines file (``config/comms.yaml``
``metrics.path`` or ``$A6G_COMMS_LOG``; several processes may append to the same file) and,
when tracing is set up, to OpenTelemetry as one span event per message.
"""

import csv
import json
import os
import statistics
import threading
from collections import deque
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from opentelemetry import trace

_tracer = trace.get_tracer("a6g.comms")

FIELDS = (
    "ts",
    "direction",
    "agent",
    "msg_id",
    "in_reply_to",
    "conversation_id",
    "mission_id",
    "sender",
    "receivers",
    "pattern",
    "performative",
    "schema",
    "binding",
    "encoding",
    "payload_bytes",
    "nl_bytes",
    "wire_bytes",
    "serialize_ms",
    "queue_ms",
    "network_ms",
    "service_ms",
    "e2e_ms",
    "attempt",
    "outcome",
)


class CommsLog:
    def __init__(self, path: str | Path | None = None, keep: int = 50_000) -> None:
        self.path = Path(path) if path else None
        self.records: deque[dict[str, Any]] = deque(maxlen=keep)
        self._lock = threading.Lock()
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, rec: dict[str, Any]) -> None:
        self.records.append(rec)
        if self.path is not None:
            line = json.dumps(rec, separators=(",", ":")) + "\n"
            with self._lock, self.path.open("a") as f:
                f.write(line)
        span = trace.get_current_span()
        if span.is_recording():
            span.add_event(
                "a6g.message",
                {k: v for k, v in rec.items() if isinstance(v, (str, int, float, bool))},
            )


_default: CommsLog | None = None


def comms_log() -> CommsLog:
    """The process-wide sink (memory only unless configured or ``$A6G_COMMS_LOG`` is set)."""
    global _default
    if _default is None:
        _default = CommsLog(os.environ.get("A6G_COMMS_LOG") or None)
    return _default


def configure_comms_log(path: str | Path | None) -> CommsLog:
    """Point the process-wide sink at ``path`` (``None``: memory only)."""
    global _default
    _default = CommsLog(path)
    return _default


# --------------------------------------------------------------------------- report


def fresh_log(path: str | Path) -> Path:
    """Start a stack run on an empty message log: the last run's log becomes
    ``<name>.prev.jsonl`` (replacing the one before), so one run is kept for
    ``a6g comms report`` and the log no longer grows across runs (2026-10-19)."""
    path = Path(path)
    if path.exists() and path.stat().st_size > 0:
        path.replace(path.with_name(f"{path.stem}.prev{path.suffix}"))
    return path


def load_records(paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in paths:
        path = Path(p)
        if not path.exists():
            continue
        with path.open() as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    out.sort(key=lambda r: r["ts"])
    return out


_FLEET_PATTERNS = ("P4", "P5", "P8")


def mission_records(records: list[dict[str, Any]], mission_id: str) -> list[dict[str, Any]]:
    """The mission's own messages, plus the fleet traffic (state, liveness, kill-switch)
    of its agents while it ran (marked ``scope = fleet``)."""
    own = [r for r in records if r.get("mission_id") == mission_id]
    if not own:
        return []
    agents = {r["sender"] for r in own} | {x for r in own for x in r.get("receivers") or []}
    start, end = own[0]["ts"], own[-1]["ts"]
    ids = {r["msg_id"] for r in own}
    # Replies (and receptions) of the mission's requests carry its id through in_reply_to.
    linked = [
        r for r in records if r.get("in_reply_to") in ids and r.get("mission_id") != mission_id
    ]
    fleet = [
        {**r, "scope": "fleet"}
        for r in records
        if r["pattern"] in _FLEET_PATTERNS
        and r.get("mission_id") != mission_id
        and start <= r["ts"] <= end
        and r["sender"] in agents
    ]
    return sorted([{**r, "scope": "mission"} for r in own + linked] + fleet, key=lambda r: r["ts"])


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, round(0.95 * (len(values) - 1)))]


def summarize(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per pattern: messages sent (requests, replies, publications), bytes,
    end-to-end latency (requests: round trip; publications: per subscriber) and failures."""
    rows = []
    records = [r for r in records if r["direction"] != "model"]  # see model_summary()
    for pattern in sorted({r["pattern"] for r in records}):
        recs = [r for r in records if r["pattern"] == pattern]
        sent = [r for r in recs if r["direction"] in ("send", "reply")]
        lat = [
            r["e2e_ms"]
            for r in recs
            if r.get("e2e_ms") is not None
            and (
                r["direction"] == "recv"
                or (r["direction"] == "send" and r.get("in_reply_to") is None)
            )
        ]
        rows.append(
            {
                "pattern": pattern,
                "messages": len(sent),
                "payload_bytes": sum(r.get("payload_bytes") or 0 for r in sent),
                "nl_bytes": sum(r.get("nl_bytes") or 0 for r in sent),
                "wire_bytes": sum(r.get("wire_bytes") or 0 for r in sent),
                "e2e_ms_mean": round(statistics.fmean(lat), 2) if lat else None,
                "e2e_ms_p95": round(_p95(lat), 2) if lat else None,
                "failed": sum(1 for r in sent if r.get("outcome") not in (None, "ok")),
                "bindings": ",".join(sorted({r["binding"] for r in sent})),
            }
        )
    return rows


def write_records_csv(path: str | Path, records: list[dict[str, Any]]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[*FIELDS, "scope"], extrasaction="ignore")
        w.writeheader()
        for r in records:
            w.writerow({**r, "receivers": " ".join(r.get("receivers") or [])})
    return path


def model_summary(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Model calls per function (rule 23): calls, accepted proposals, tokens, inference."""
    calls = [r for r in records if r["direction"] == "model"]
    rows = []
    for function in sorted({r["performative"] for r in calls}):
        recs = [r for r in calls if r["performative"] == function]
        ms = [r["inference_ms"] for r in recs if r.get("inference_ms") is not None]
        rows.append(
            {
                "function": function,
                "calls": len(recs),
                "failed": sum(1 for r in recs if r.get("outcome") == "failed"),
                "input_tokens": sum(r.get("input_tokens") or 0 for r in recs),
                "output_tokens": sum(r.get("output_tokens") or 0 for r in recs),
                "inference_ms_mean": round(statistics.fmean(ms), 1) if ms else None,
                "inference_ms_p95": round(_p95(ms), 1) if ms else None,
                "prompt": ",".join(sorted({r["schema"] for r in recs})),
            }
        )
    return rows
