"""A robot at the base in full coverage does not refuse new work with "it has no
connection" when its envelope thread finished while the link was lost: link changes go
to active threads only, so the robot must still hear the link come back."""

from datetime import timedelta
from pathlib import Path

from a6g_common.config import load_config
from a6g_common.schemas import Envelope, EnvelopeBudgets, PoiTarget, TaskKind, TaskSpec
from a6g_devstack import SimStack
from conftest import WHOLE_CAMPUS


def _envelope(stack: SimStack, version: int) -> Envelope:
    now = stack.world.clock.now()
    return Envelope(
        envelope_id="env_stale",
        version=version,
        robot_id="summit_01",
        mission_id="m_stale",
        tasks=[
            TaskSpec(
                task_id=f"t{version}",
                kind=TaskKind.GO_TO,
                target=PoiTarget(poi_id="poi_kiosk"),
                est_duration_s=5,
            )
        ],
        region=WHOLE_CAMPUS,
        valid_from=now,
        valid_until=now + timedelta(hours=2),
        budgets=EnvelopeBudgets(battery_floor_pct=15, uplink_mbps=3),
    )


async def test_an_idle_thread_takes_the_live_link_reading(tmp_path: Path) -> None:
    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        stack.world.pause()
        agent = stack.robots["summit_01"].agent
        await agent.handle_envelope(_envelope(stack, 1))
        thread = agent.thread_id("env_stale")
        # finished while out of coverage: the thread remembers the loss
        await agent.graph.aupdate_state(
            agent._config(thread), {"link_quality": "lost", "status": "completed"}
        )
        agent._threads = [t for t in agent._threads if t != thread]
        reply = await agent.handle_envelope(_envelope(stack, 2))
    finally:
        await stack.stop()
    assert reply.accepted_task_ids == ["t2"], reply
