"""Append-only decision log (JSONL). Every record is also emitted as an OpenTelemetry span.

A Postgres-backed sink is added with the coordinator (P3); it implements the same
:class:`DecisionSink` protocol.
"""

import fcntl
import hashlib
import json
import os
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Protocol

from a6g_common.clock import Clock, SystemClock
from a6g_common.ids import IdFactory, new_id
from a6g_common.schemas import Decision, DecisionKind
from opentelemetry import trace

_tracer = trace.get_tracer("a6g.decisions")


class DecisionSink(Protocol):
    def record(self, decision: Decision) -> Decision: ...


def current_trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


def make_decision(
    actor: str,
    kind: DecisionKind | str,
    *,
    mission_id: str | None = None,
    inputs_ref: str = "",
    output: Mapping[str, Any] | None = None,
    policy: Mapping[str, Any] | None = None,
    justification: str = "",
    clock: Clock | None = None,
    ids: IdFactory = new_id,
) -> Decision:
    """Build a :class:`Decision` stamped with the clock and the current trace id."""
    return Decision(
        decision_id=ids("dec"),
        actor=actor,
        kind=DecisionKind(kind),
        mission_id=mission_id,
        inputs_ref=inputs_ref,
        output=dict(output or {}),
        policy=dict(policy or {}),
        justification=justification,
        stamp=(clock or SystemClock()).now(),
        trace_id=current_trace_id(),
    )


class DecisionLog:
    """Thread-safe JSONL decision log."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def record(self, decision: Decision) -> Decision:
        with _tracer.start_as_current_span(f"decision.{decision.kind.value}") as span:
            span.set_attribute("a6g.decision_id", decision.decision_id)
            span.set_attribute("a6g.actor", decision.actor)
            span.set_attribute("a6g.kind", decision.kind.value)
            if decision.mission_id:
                span.set_attribute("a6g.mission_id", decision.mission_id)
            if decision.trace_id is None:
                decision = decision.model_copy(update={"trace_id": current_trace_id()})
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a+", encoding="utf-8") as fh:
                # Several processes append to one log: the file lock keeps the chain linear.
                fcntl.flock(fh, fcntl.LOCK_EX)
                try:
                    decision = chained(decision, _last_hash(fh))
                    fh.seek(0, os.SEEK_END)
                    fh.write(decision.model_dump_json() + "\n")
                    fh.flush()
                finally:
                    fcntl.flock(fh, fcntl.LOCK_UN)
        return decision

    def read(self, mission_id: str | None = None) -> list[Decision]:
        """All decisions (optionally for one mission), ordered by stamp."""
        if not self.path.exists():
            return []
        decisions = []
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    decision = Decision.model_validate_json(line)
                    if mission_id is None or decision.mission_id == mission_id:
                        decisions.append(decision)
        return sorted(decisions, key=lambda d: d.stamp)


# --------------------------------------------------------------------------- the chain
# Tamper-evident log (SPEC A3.9; NFR-A6G-06, AI Act Art. 12 and 19): each record carries the
# hash of the one before it and its own, the SHA-256 of its canonical JSON.


def canonical(decision: Decision) -> str:
    body = decision.model_dump(mode="json", exclude={"hash"})
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(decision: Decision) -> str:
    return hashlib.sha256(canonical(decision).encode()).hexdigest()


def chained(decision: Decision, prev_hash: str | None) -> Decision:
    linked = decision.model_copy(update={"prev_hash": prev_hash, "hash": None})
    return linked.model_copy(update={"hash": digest(linked)})


def _last_hash(fh: Any) -> str | None:
    """The hash of the last record of an open log (read from its end)."""
    fh.seek(0, os.SEEK_END)
    end = fh.tell()
    if end == 0:
        return None
    pos, chunk, tail = end, 4096, ""
    while pos > 0:
        step = min(chunk, pos)
        pos -= step
        fh.seek(pos)
        tail = fh.read(step) + tail
        lines = [x for x in tail.split("\n") if x.strip()]
        if len(lines) >= 2 or pos == 0:
            break
    lines = [x for x in tail.split("\n") if x.strip()]
    if not lines:
        return None
    return json.loads(lines[-1]).get("hash")


def verify(path: Path | str, mission_id: str | None = None) -> dict[str, Any]:
    """Recompute the chain over the whole log, in file order: a modified record (its hash),
    a deleted or reordered one (the next record's ``prev_hash``) and an unchained record
    after the chain started all fail. Records written before the chain existed are
    counted as ``legacy``. ``mission_id`` only counts that mission's records."""
    problems: list[str] = []
    prev: str | None = None
    started = False
    total = legacy = mine = 0
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        total += 1
        d = Decision.model_validate_json(line)
        mine += mission_id is None or d.mission_id == mission_id
        if d.hash is None:
            if started:
                problems.append(f"line {n}: unchained record inside the chain ({d.decision_id})")
            else:
                legacy += 1
            prev = None
            continue
        started = True
        if digest(d) != d.hash:
            problems.append(f"line {n}: modified ({d.decision_id}: hash does not match)")
        if d.prev_hash != prev:
            problems.append(
                f"line {n}: broken link ({d.decision_id}: a record before it was deleted, "
                "inserted or moved)"
            )
        prev = d.hash
    return {
        "ok": not problems,
        "records": total,
        "mission_records": mine,
        "legacy": legacy,
        "problems": problems,
    }


def format_timeline(decisions: Iterable[Decision]) -> str:
    """Human-readable, auditable timeline (``a6g decisions --mission <id>``)."""
    lines = []
    for d in decisions:
        out = json.dumps(d.output, sort_keys=True, separators=(",", ":"))
        line = f"{d.stamp.isoformat()}  {d.kind.value:<11} {d.actor:<14} {out}"
        if d.policy:
            line += f"  policy={json.dumps(d.policy, sort_keys=True, separators=(',', ':'))}"
        if d.justification:
            line += f"  -- {d.justification}"
        lines.append(line)
    return "\n".join(lines)
