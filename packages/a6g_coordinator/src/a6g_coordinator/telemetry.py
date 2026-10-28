"""Coordinator side of the telemetry plane, shared by both hosts.

* publishes ``a6g/coordinator/heartbeat`` every ``heartbeat_period_s`` (retained; the MQTT
  connection's last-will is ``offline``);
* watches ``a6g/fleet/+/+`` with a :class:`FleetMonitor` and turns robot failures, losses,
  recoveries and arrivals into events for the active missions (``broadcast``);
* after a (re)start, holds failure-driven events for ``robot_timeout_s`` so robots can
  reconcile first and contract-net awards are not duplicated.
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from a6g_common.clock import Clock
from a6g_common.config import A6GConfig
from a6g_transport.messaging import TOPIC_HEARTBEAT, TelemetryBus

from a6g_coordinator.fleet_monitor import FleetMonitor

log = logging.getLogger(__name__)


class CoordinatorTelemetry:
    def __init__(
        self,
        cfg: A6GConfig,
        bus: TelemetryBus,
        clock: Clock,
        broadcast: Callable[[dict[str, Any]], Awaitable[None]],
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_robot_added: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self.cfg = cfg
        self.bus = bus
        self.clock = clock
        self.broadcast = broadcast
        self.on_robot_added = on_robot_added
        self._sleep = sleep
        coord = cfg.fleet.defaults.coordinator
        self.monitor = FleetMonitor(
            {r.robot_id for r in cfg.fleet.robots},
            coord.robot_timeout_s,
            coord.lost_release_s,
            cfg.allocator.recharge_resume_pct,
        )
        self._started_at = clock.now()
        self._held: list[dict[str, Any]] = []
        self._tasks: list[asyncio.Task[None]] = []
        #: set when a heartbeat cannot be published (broker gone): the host reconnects
        self.failed = asyncio.Event()

    def start(self) -> None:
        self._started_at = self.clock.now()
        loops = {"heartbeat": self._heartbeat(), "watch": self._watch(), "check": self._check()}
        for name, coro in loops.items():
            self._tasks.append(asyncio.create_task(coro, name=f"coord-{name}"))

    async def stop(self, graceful: bool = True) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks.clear()
        if graceful:
            with contextlib.suppress(Exception):
                await self.bus.publish(TOPIC_HEARTBEAT, "offline", qos=1, retain=True)

    async def _heartbeat(self) -> None:
        period = self.cfg.fleet.defaults.coordinator.heartbeat_period_s
        while True:
            try:
                payload = json.dumps({"state": "online", "stamp": self.clock.now().isoformat()})
                await self.bus.publish(TOPIC_HEARTBEAT, payload, qos=1, retain=True)
            except Exception as exc:
                log.warning("heartbeat publish failed: %s", exc)
                self.failed.set()
            await self._sleep(period)

    async def _watch(self) -> None:
        while True:
            try:
                async for msg in self.bus.subscribe("a6g/fleet/+/+"):
                    for event in self.monitor.observe(msg, self.clock.now()):
                        await self._dispatch(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # broker down: retry
                log.warning("fleet subscription failed (%s); retrying", exc)
                await asyncio.sleep(2.0)

    async def _check(self) -> None:
        while True:
            for event in self.monitor.check(self.clock.now()):
                await self._dispatch(event)
            if self._held and not self.in_grace():
                held, self._held = self._held, []
                for event in held:
                    await self._dispatch(event)
            await self._sleep(1.0)

    def in_grace(self) -> bool:
        grace = self.cfg.fleet.defaults.coordinator.robot_timeout_s
        return (self.clock.now() - self._started_at).total_seconds() < grace

    async def _dispatch(self, event: dict[str, Any]) -> None:
        if event["type"] in ("robot_failed", "robot_lost") and self.in_grace():
            self._held.append(event)
            return
        log.warning("fleet event: %s", event)
        if event["type"] == "robot_added" and self.on_robot_added is not None:
            await self.on_robot_added(event)
        await self.broadcast(event)
