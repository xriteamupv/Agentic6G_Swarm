"""Clocks. Code takes a :class:`Clock` so the simulator can drive time explicitly."""

import contextlib
from collections.abc import AsyncIterator, Callable
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

#: Set by a simulator while an agent's computation holds simulated time still; a model call
#: (or a remote service) awaits through it so that the world keeps running meanwhile.
_OUTSIDE: ContextVar[Callable[[], Any] | None] = ContextVar("a6g_outside_time", default=None)


@contextlib.asynccontextmanager
async def time_flows() -> AsyncIterator[None]:
    """Wrap a wait on something external (a model, a remote service): in a simulator that
    freezes time while agents compute, time flows again for the length of the wait (the
    wait then counts in simulated time as wall latency x the speed factor). Elsewhere it
    does nothing."""
    thaw = _OUTSIDE.get()
    if thaw is None:
        yield
        return
    async with thaw():
        yield


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time, timezone-aware UTC."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class ManualClock:
    """Clock advanced explicitly (simulator, tests)."""

    def __init__(self, start: datetime | None = None) -> None:
        start = start or datetime(2026, 1, 1, tzinfo=UTC)
        if start.tzinfo is None:
            raise ValueError("ManualClock start must be timezone-aware")
        self._now = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> datetime:
        if seconds < 0:
            raise ValueError("time cannot go backwards")
        self._now += timedelta(seconds=seconds)
        return self._now


def utc_now() -> datetime:
    return datetime.now(UTC)
