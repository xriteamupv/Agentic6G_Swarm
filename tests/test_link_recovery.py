"""SPEC A1 12.1: hold_then_recover (2D simulator).

A robot finishes a task inside a coverage shadow, holds for link_loss_hold_s, then drives to
the nearest reachable link recovery point, reconnects and flushes its queued reports.
"""

from datetime import timedelta
from pathlib import Path

import pytest
from a6g_common.config import load_config
from a6g_common.geometry import point_in_polygon
from a6g_common.schemas import (
    DecisionKind,
    Envelope,
    EnvelopeBudgets,
    LinkQuality,
    Pose2D,
    PoseTarget,
    ReportKind,
    TaskKind,
    TaskSpec,
)
from a6g_devstack import SimStack
from conftest import WHOLE_CAMPUS, wait_for

pytestmark = pytest.mark.scenario
SHADOW_TASK = "t_guard_in_shadow"


@pytest.fixture
async def stack(tmp_path: Path):
    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    s = SimStack(cfg, tmp_path / "stack")
    await s.start()
    try:
        yield s
    finally:
        await s.stop()


def _inside_shadow(stack: SimStack) -> Pose2D:
    """The footpath spot nearest the middle of the campus's first coverage shadow (the
    robots walk the footpaths only)."""
    from a6g_common.footpaths import walker_for

    campus = stack.cfg.campus
    shadow = campus.link_shadows[0]
    x = sum(p[0] for p in shadow) / len(shadow)
    y = sum(p[1] for p in shadow) / len(shadow)
    x, y = walker_for(campus.route_graph).project((x, y)).point
    assert point_in_polygon((x, y), shadow)
    return Pose2D(x=x, y=y)


def fallbacks(stack: SimStack) -> list:
    return [
        d
        for d in stack.log.read("m_lr")
        if d.kind is DecisionKind.FALLBACK and d.actor == "go2_01.agent" and "step" in d.output
    ]


async def test_robot_leaves_shadow_for_recovery_point_after_hold(stack: SimStack) -> None:
    now = stack.world.clock.now()
    task = TaskSpec(
        task_id=SHADOW_TASK,
        kind=TaskKind.HOLD_POST,
        target=PoseTarget(pose=_inside_shadow(stack)),
        required_capabilities=["hold_post"],
        est_duration_s=5,
    )
    envelope = Envelope(
        envelope_id="env_lr",
        robot_id="go2_01",
        mission_id="m_lr",
        tasks=[task],
        region=WHOLE_CAMPUS,
        valid_from=now,
        valid_until=now + timedelta(hours=2),
        budgets=EnvelopeBudgets(battery_floor_pct=20, uplink_mbps=3),
        swappable=True,
    )
    agent = stack.robots["go2_01"].agent
    assert (await agent.handle_envelope(envelope)).decision == "accept"

    # 1. It finishes the task inside the shadow, then holds.
    await wait_for(lambda: [d for d in fallbacks(stack) if d.output["step"] == "hold"], timeout=60)
    hold = next(d for d in fallbacks(stack) if d.output["step"] == "hold")
    robot = stack.world.robot("go2_01")
    assert stack.world.link_quality("go2_01") is LinkQuality.LOST

    async def task_done() -> bool:  # the decision is logged mid-run; state lands at run end
        return (await agent.state("env_lr"))["tasks"][SHADOW_TASK]["status"] == "succeeded"

    await wait_for(task_done, timeout=10)

    # 2. After the hold it drives to the nearest reachable recovery point.
    await wait_for(
        lambda: [d for d in fallbacks(stack) if d.output["step"] == "seek_coverage"], timeout=60
    )
    seek = next(d for d in fallbacks(stack) if d.output["step"] == "seek_coverage")
    held_for = (seek.stamp - hold.stamp).total_seconds()
    assert 30.0 <= held_for < 35.0, held_for
    assert seek.output["recovery_point"] == "lrp_east_gate"

    # 3. It leaves the shadow, reconnects and flushes the queued reports.
    async def reconnected() -> bool:
        s = await agent.state("env_lr")
        return (
            s.get("link_quality") == "ok"
            and not s.get("outbox")
            and s.get("current_task_id") is None
        )

    await wait_for(reconnected, timeout=60)
    assert stack.world.link_quality("go2_01") is LinkQuality.OK
    shadow = stack.cfg.campus.link_shadows[0]
    assert not point_in_polygon((robot.x, robot.y), shadow)  # out of the coverage shadow
    delivered = [r for r in stack.delivered if r.robot_id == "go2_01"]
    assert any(
        r.kind is ReportKind.SKILL_RESULT and r.payload["task_id"] == SHADOW_TASK for r in delivered
    )
    restored = [
        d
        for d in stack.log.read("m_lr")
        if d.kind is DecisionKind.FALLBACK and d.output.get("link_quality") == "ok"
    ]
    assert restored, "link restoration is a logged decision"
    assert not any(d.output.get("step") == "return_home" for d in fallbacks(stack))
