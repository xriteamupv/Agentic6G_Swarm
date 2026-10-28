"""Drive a :class:`SimWorld` from asyncio."""

import asyncio
import contextlib

from a6g_sim.world import SimWorld


class SimRunner:
    """Ticks the world every ``dt`` simulated seconds.

    ``realtime_factor=None`` runs as fast as possible and, with ``idle_when_quiet``, stops
    simulated time while no robot has a job (deterministic tests). A realtime factor
    (1.0 = wall clock) is used when serving the simulator to real agents.
    """

    def __init__(
        self,
        world: SimWorld,
        dt: float = 0.1,
        realtime_factor: float | None = None,
        idle_when_quiet: bool = True,
    ) -> None:
        if dt <= 0:
            raise ValueError("dt must be > 0")
        self.world = world
        self.dt = dt
        self.realtime_factor = realtime_factor
        self.idle_when_quiet = idle_when_quiet
        self._task: asyncio.Task[None] | None = None

    async def _loop(self) -> None:
        while True:
            await self.world.unpaused.wait()
            if self.idle_when_quiet and not self.world.busy():
                await self.world.activity.wait()
                await self.world.unpaused.wait()
            self.world.step(self.dt)
            if self.realtime_factor:
                await asyncio.sleep(self.dt / self.realtime_factor)
            else:
                await asyncio.sleep(0)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="sim-runner")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def __aenter__(self) -> "SimRunner":
        self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()
