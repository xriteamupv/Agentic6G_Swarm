"""asyncio client of the ``ros_adapter`` JSON-lines protocol.

One connection, requests multiplexed by id (a ``cancel`` can overtake a ``navigate``).
Reconnects on the next request after the adapter restarts.
"""

import asyncio
import contextlib
import itertools
import json
import logging
from typing import Any

log = logging.getLogger(__name__)


class AdapterUnavailable(Exception):
    """The adapter socket cannot be reached or the connection dropped."""


class AdapterRequestError(Exception):
    """The adapter rejected the request (``ok: false``)."""


class AdapterClient:
    def __init__(self, socket_path: str, connect_timeout_s: float = 5.0) -> None:
        self.socket_path = socket_path
        self.connect_timeout_s = connect_timeout_s
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def _connect(self) -> None:
        async with self._lock:
            if self._writer is not None and not self._writer.is_closing():
                return
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_unix_connection(self.socket_path, limit=2**24),
                    self.connect_timeout_s,
                )
            except (OSError, TimeoutError) as exc:
                raise AdapterUnavailable(f"ros_adapter at {self.socket_path}: {exc}") from exc
            self._writer = writer
            self._reader_task = asyncio.create_task(self._read(reader))

    async def _read(self, reader: asyncio.StreamReader) -> None:
        try:
            while line := await reader.readline():
                msg = json.loads(line)
                fut = self._pending.pop(msg.get("id"), None)
                if fut is None or fut.done():
                    continue
                if msg.get("ok"):
                    fut.set_result(msg.get("result"))
                else:
                    fut.set_exception(AdapterRequestError(msg.get("error", "adapter error")))
        except (OSError, ValueError) as exc:
            log.warning("adapter connection lost: %s", exc)
        finally:
            self._writer = None
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(AdapterUnavailable("adapter connection closed"))
            self._pending.clear()

    async def request(self, op: str, args: dict[str, Any] | None = None, timeout_s: float = 10.0):
        await self._connect()
        assert self._writer is not None
        rid = next(self._ids)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        self._writer.write((json.dumps({"id": rid, "op": op, "args": args or {}}) + "\n").encode())
        await self._writer.drain()
        try:
            return await asyncio.wait_for(fut, timeout_s)
        finally:
            self._pending.pop(rid, None)

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(OSError):
                await self._writer.wait_closed()
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
