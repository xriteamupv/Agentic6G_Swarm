"""The simulator does not freeze while a model answers: the world keeps running
at full speed, so a model's wait counts as wall latency x the speed factor in simulated
time. ``model_time="freeze"`` freezes it instead."""

from pathlib import Path

from a6g_common.clock import time_flows
from a6g_common.config import load_config
from a6g_devstack import SimStack
from a6g_sim.world import SimWorld
from langchain_core.language_models import FakeListChatModel


async def test_time_flows_releases_the_callers_freeze_only() -> None:
    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    world = SimWorld(cfg.campus)
    async with time_flows():  # outside any freeze: nothing to do
        assert not world.paused
    async with world.frozen(), world.frozen():  # re-entrant: both levels released
        assert world.paused
        async with time_flows():
            assert not world.paused
        assert world.paused
    world.models_freeze_time = True  # freeze mode: the world waits for the model
    async with world.frozen(), time_flows():
        assert world.paused
    assert not world.paused


async def test_a_wait_that_outlives_its_freeze_does_not_freeze_the_world() -> None:
    """A killed coordinator: its step (the freeze) ends while a send it started is still
    waiting outside time; when that send finishes, the ended freeze must not come back
    (it would freeze the world for good)."""
    import asyncio

    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    world = SimWorld(cfg.campus)
    go_on = asyncio.Event()

    async def send() -> None:  # started inside the step, awaited outside time
        async with time_flows():
            await go_on.wait()

    async with world.frozen():
        pending = asyncio.create_task(send())
        await asyncio.sleep(0)
        assert not world.paused  # the send waits with the world running
    go_on.set()  # the step is gone; then the send returns
    await pending
    assert not world.paused


async def _clock_advance(tmp_path: Path, model_time: str) -> float:
    """Simulated seconds that pass while the robots' (slow) models deliberate on their
    first envelopes."""
    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    slow = FakeListChatModel(responses=['{"action": "accept_all"}'], sleep=1.0)
    stack = SimStack(cfg, tmp_path / model_time, robot_model=slow, model_time=model_time)
    await stack.start()
    try:
        before = stack.world.clock.now()
        await stack.mission("Patrol the campus")
        return (stack.world.clock.now() - before).total_seconds()
    finally:
        await stack.stop()


async def test_the_world_runs_on_while_robots_think(tmp_path: Path) -> None:
    frozen = await _clock_advance(tmp_path, "freeze")
    flowing = await _clock_advance(tmp_path, "full")
    assert frozen < 5.0, frozen  # each 1 s deliberation took no simulated time
    assert flowing > 30.0, flowing  # ~1 s wall x the speed factor, per deliberation round


async def test_an_event_queued_behind_a_thinking_robot_does_not_stop_the_world(
    tmp_path: Path,
) -> None:
    """Leaving a coverage shadow queues a link change and a flush behind the robot's model
    call; waiting for the robot's lock must not hold simulated time still."""
    import asyncio

    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    slow = FakeListChatModel(responses=['{"action": "accept_all"}'], sleep=1.5)
    stack = SimStack(cfg, tmp_path / "s", robot_model=slow, realtime_factor=10.0)
    await stack.start()
    try:
        mission = asyncio.create_task(stack.mission("Patrol the campus"))  # robots deliberate
        await asyncio.sleep(0.3)
        agent = stack.robots["go2_01"].agent
        before = stack.world.clock.now()
        queued = asyncio.create_task(agent.invoke(agent.thread_id("probe"), {"type": "tick"}))
        await asyncio.sleep(1.0)  # the robot is still thinking; the tick waits for it
        advanced = (stack.world.clock.now() - before).total_seconds()
        await asyncio.gather(mission, queued)
    finally:
        await stack.stop()
    assert advanced > 5.0, advanced  # ~1 s wall x 10: the world ran on meanwhile
