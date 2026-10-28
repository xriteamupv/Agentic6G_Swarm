"""Checkpoint overhead (design principle DP1 "the twin is the memory").

``MeteredSaver`` wraps any LangGraph checkpointer and records, for every ``put`` and
``put_writes`` (and their async variants), the serialized bytes and the write latency, with
the agent and the event type that caused the write (``CHECKPOINT_EVENT``, set by the
coordinator and robot runtimes around each graph run). The serialization for counting runs
outside the timed call, so the latency is the saver's own.
"""

import json
import time
from collections.abc import Sequence
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver

#: the event a graph run handles (goal, report, failure, posture, operator reply, ...)
CHECKPOINT_EVENT: ContextVar[str | None] = ContextVar("a6g_checkpoint_event", default=None)


def event_kind(payload: Any) -> str:
    """The event type of a coordinator or robot graph input."""
    if isinstance(payload, dict):
        ev = payload.get("event")
        if isinstance(ev, dict) and ev.get("type"):
            return str(ev["type"])
        if payload.get("messages"):
            return "chat"
    if type(payload).__name__ == "Command":
        return "operator_reply"
    return "other"


class MeteredSaver(BaseCheckpointSaver):
    def __init__(self, inner: BaseCheckpointSaver, agent: str, sink: Path | None = None) -> None:
        super().__init__(serde=inner.serde)
        self.inner = inner
        self.agent = agent
        self.sink = sink
        self.records: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ accounting

    def _size_checkpoint(self, checkpoint: Any, metadata: Any) -> int:
        try:
            _, blob = self.serde.dumps_typed(checkpoint)
            meta = json.dumps(metadata, default=str).encode()
            return len(blob) + len(meta)
        except Exception:
            return -1

    def _size_writes(self, writes: Sequence[tuple[str, Any]]) -> int:
        total = 0
        try:
            for _, value in writes:
                total += len(self.serde.dumps_typed(value)[1])
        except Exception:
            return -1
        return total

    def _note(self, op: str, size: int, started: float, thread: str | None) -> None:
        rec = {
            "agent": self.agent,
            "op": op,
            "event": CHECKPOINT_EVENT.get() or "other",
            "bytes": size,
            "ms": round((time.perf_counter() - started) * 1000.0, 3),
            "thread": thread,
        }
        self.records.append(rec)
        if self.sink is not None:
            with self.sink.open("a") as fh:
                fh.write(json.dumps(rec) + "\n")

    @staticmethod
    def _thread(config: Any) -> str | None:
        return ((config or {}).get("configurable") or {}).get("thread_id")

    # ------------------------------------------------------------------ writes (metered)

    def put(self, config, checkpoint, metadata, new_versions):  # type: ignore[override]
        size = self._size_checkpoint(checkpoint, metadata)
        started = time.perf_counter()
        out = self.inner.put(config, checkpoint, metadata, new_versions)
        self._note("put", size, started, self._thread(config))
        return out

    async def aput(self, config, checkpoint, metadata, new_versions):  # type: ignore[override]
        size = self._size_checkpoint(checkpoint, metadata)
        started = time.perf_counter()
        out = await self.inner.aput(config, checkpoint, metadata, new_versions)
        self._note("put", size, started, self._thread(config))
        return out

    def put_writes(self, config, writes, task_id, task_path=""):  # type: ignore[override]
        size = self._size_writes(writes)
        started = time.perf_counter()
        out = self.inner.put_writes(config, writes, task_id, task_path)
        self._note("put_writes", size, started, self._thread(config))
        return out

    async def aput_writes(self, config, writes, task_id, task_path=""):  # type: ignore[override]
        size = self._size_writes(writes)
        started = time.perf_counter()
        out = await self.inner.aput_writes(config, writes, task_id, task_path)
        self._note("put_writes", size, started, self._thread(config))
        return out

    # ------------------------------------------------------------------ reads (forwarded)

    def get_tuple(self, config):  # type: ignore[override]
        return self.inner.get_tuple(config)

    async def aget_tuple(self, config):  # type: ignore[override]
        return await self.inner.aget_tuple(config)

    def list(self, config, *, filter=None, before=None, limit=None):  # type: ignore[override]
        return self.inner.list(config, filter=filter, before=before, limit=limit)

    def alist(self, config, *, filter=None, before=None, limit=None):  # type: ignore[override]
        return self.inner.alist(config, filter=filter, before=before, limit=limit)

    def delete_thread(self, thread_id):  # type: ignore[override]
        return self.inner.delete_thread(thread_id)

    async def adelete_thread(self, thread_id):  # type: ignore[override]
        return await self.inner.adelete_thread(thread_id)

    def get_next_version(self, current, channel):  # type: ignore[override]
        return self.inner.get_next_version(current, channel)

    @property
    def config_specs(self):  # type: ignore[override]
        return self.inner.config_specs

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)
