"""P1 acceptance: skill server on the simulator.

- An MCP client lists exactly 9 tools.
- Motion while disarmed returns refused_disarmed.
- stop works while disarmed.
- A Summit manifest refuses reverse motion.
"""

import asyncio
import itertools
import math
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from a6g_cli import main as cli
from a6g_common.config import Motion, load_config
from a6g_common.schemas import (
    MOTION_SKILLS,
    Decision,
    DecisionKind,
    Pose2D,
    RobotState,
    SkillName,
    SkillResult,
)
from a6g_sim.world import plan_motion
from conftest import CONFIG_DIR, OPERATOR_TOKEN, SimFleet
from mcp import Client
from starlette.testclient import TestClient
from typer.testing import CliRunner

KIOSK = {"poi_id": "poi_kiosk"}

MOTION_CALLS: dict[SkillName, dict[str, Any]] = {
    SkillName.NAVIGATE_TO: {"pose": {"x": 3.0, "y": 0.0}},
    SkillName.FOLLOW_ROUTE: {"waypoints": [{"x": 1.0, "y": 0.0}, {"x": 2.0, "y": 0.0}]},
    SkillName.INSPECT: {"poi_id": "poi_kiosk", "dwell_s": 2.0},
    SkillName.HOLD_POST: {"pose": {"x": 1.0, "y": 0.0}, "until": "2030-01-01T00:00:00Z"},
    SkillName.RETURN_HOME: {},
}


@pytest.fixture
def cfg():
    """Motion mechanics on free ground: the footpath planner has its own tests."""
    full = load_config(CONFIG_DIR, env={})
    return full.model_copy(
        update={"campus": full.campus.model_copy(update={"footpaths_only": False})}
    )


async def call(client: Client, tool: SkillName | str, args: dict[str, Any] | None = None) -> dict:
    result = await client.call_tool(str(tool), args or {})
    assert not result.is_error, result.content
    return result.structured_content


async def skill(client: Client, tool: SkillName, args: dict[str, Any] | None = None) -> SkillResult:
    return SkillResult.model_validate(await call(client, tool, args))


# --------------------------------------------------------------------------- acceptance


async def test_mcp_client_lists_exactly_nine_tools(fleet: SimFleet) -> None:
    for robot_id in fleet.services:
        async with Client(fleet.server(robot_id)) as client:
            tools = (await client.list_tools()).tools
        names = sorted(t.name for t in tools)
        assert len(tools) == 9
        assert names == sorted(s.value for s in SkillName)
        assert not any("topic" in n or "service" in n or "publish" in n for n in names)


async def test_motion_while_disarmed_returns_refused_disarmed(fleet: SimFleet) -> None:
    assert set(MOTION_CALLS) == set(MOTION_SKILLS)
    for robot_id in fleet.services:
        robot = fleet.world.robot(robot_id)
        start = (robot.x, robot.y, robot.yaw)
        async with Client(fleet.server(robot_id)) as client:
            assert (await call(client, SkillName.GET_STATE))["armed"] is False
            for tool, args in MOTION_CALLS.items():
                result = await skill(client, tool, args)
                assert result.outcome == "refused_disarmed", (robot_id, tool, result)
        assert (robot.x, robot.y, robot.yaw) == start
        assert robot.odometer_m == 0.0


async def test_stop_works_while_disarmed(fleet: SimFleet) -> None:
    async with Client(fleet.server("go2_01")) as client:
        result = await skill(client, SkillName.STOP)
        assert result.outcome == "succeeded"
        assert fleet.world.robot("go2_01").stop_count == 1
        # cancel and capture_event_image are also allowed while disarmed
        assert (await skill(client, SkillName.CANCEL)).outcome == "succeeded"
        assert (await skill(client, SkillName.CAPTURE_EVENT_IMAGE)).outcome == "succeeded"


async def test_stop_halts_motion_even_after_disarm(fleet: SimFleet) -> None:
    service = fleet.services["go2_01"]
    service.arm("tester")
    async with Client(fleet.server("go2_01")) as client:
        nav = asyncio.create_task(skill(client, SkillName.NAVIGATE_TO, KIOSK))
        await _wait_until(lambda: fleet.world.robot("go2_01").odometer_m > 1.0)
        service.disarm("tester")
        assert (await skill(client, SkillName.STOP)).outcome == "succeeded"
        result = await nav
    assert result.outcome == "cancelled"
    robot = fleet.world.robot("go2_01")
    assert robot.job is None
    kiosk = fleet.cfg.campus.poi("poi_kiosk").pose
    assert math.hypot(robot.x - kiosk.x, robot.y - kiosk.y) > 5


async def test_summit_reverses_only_short_and_slow(fleet: SimFleet) -> None:
    summit = fleet.world.robot("summit_01")
    limits = summit.manifest.reverse_limits
    assert Motion.REVERSE not in summit.manifest.forbidden_motions
    assert limits is not None

    # 1. A goal just behind (within max_distance_m) is reached by a slow reverse hop ...
    near = Pose2D(x=summit.x - 0.4, y=summit.y, yaw=summit.yaw)
    assert [p.reverse for p in plan_motion(summit.pose, near, summit.manifest)] == [True]
    # ... a goal further behind by turning round: never a long reverse.
    far = Pose2D(x=summit.x - 0.8, y=summit.y, yaw=summit.yaw)
    plan = plan_motion(summit.pose, far, summit.manifest)
    assert not any(p.reverse for p in plan)
    assert [p.kind for p in plan] == ["turn", "drive", "turn"]

    # 2. End to end through MCP: the reverse hop runs at the reverse speed cap.
    fleet.services["summit_01"].arm("tester")
    t0 = fleet.world.clock.now()
    async with Client(fleet.server("summit_01")) as client:
        result = await skill(client, SkillName.NAVIGATE_TO, {"pose": near.model_dump()})
    assert result.outcome == "succeeded", result
    assert [p.reverse for p in summit.motion_log] == [True]
    took = (fleet.world.clock.now() - t0).total_seconds()
    assert took >= 0.4 / limits.max_speed_mps - 0.2


def test_reverse_limits_match_the_nav2_and_governor_settings(cfg) -> None:
    """One source of truth: the manifest; Nav2, the speed wiring and the monitor agree."""
    import sys

    import yaml

    sys.path.insert(0, "sim_gazebo/src/a6g_gz_bringup")
    from a6g_gz_bringup.speed import SUMMIT_REVERSE_MAX

    limits = cfg.manifests["summit_01"].reverse_limits
    nav2 = yaml.safe_load(Path("sim_gazebo/src/a6g_gz_bringup/config/nav2/summit.yaml").read_text())
    mppi = nav2["controller_server"]["ros__parameters"]["FollowPath"]
    smoother = nav2["velocity_smoother"]["ros__parameters"]
    assert mppi["plugin"] == "nav2_mppi_controller::MPPIController"
    assert mppi["vx_min"] == -limits.max_speed_mps == -SUMMIT_REVERSE_MAX
    assert smoother["min_velocity"][0] == -limits.max_speed_mps
    assert mppi["PreferForwardCritic"]["threshold_to_consider"] == limits.max_distance_m
    behaviors = nav2["behavior_server"]["ros__parameters"]["behavior_plugins"]
    assert not any("back" in b.lower() for b in behaviors)  # no blind BackUp recovery


# --------------------------------------------------------------------------- skill behaviour


async def _wait_until(predicate, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


async def test_navigate_to_poi_when_armed(fleet: SimFleet) -> None:
    fleet.services["go2_01"].arm("tester")
    robot = fleet.world.robot("go2_01")
    goal = fleet.cfg.campus.poi("poi_kiosk").pose
    route = [(robot.x, robot.y), *fleet.world.planner.plan((robot.x, robot.y), (goal.x, goal.y))]
    length = sum(math.dist(a, b) for a, b in itertools.pairwise(route))
    async with Client(fleet.server("go2_01")) as client:
        result = await skill(client, SkillName.NAVIGATE_TO, KIOSK)
        state = RobotState.model_validate(await call(client, SkillName.GET_STATE))
    assert result.outcome == "succeeded"
    kiosk = fleet.cfg.campus.poi("poi_kiosk").pose
    assert math.hypot(state.pose.x - kiosk.x, state.pose.y - kiosk.y) < 0.25
    assert state.armed is True
    # The planned path at 0.30 m/s plus turns: duration is simulated time, not wall time.
    walk = length / 0.30
    assert walk <= result.duration_s <= walk + 60
    assert state.battery_pct is not None and state.battery_pct < 100


async def test_summit_battery_reported_unknown(fleet: SimFleet) -> None:
    async with Client(fleet.server("summit_01")) as client:
        state = await call(client, SkillName.GET_STATE)
    assert state["battery_pct"] is None  # battery_reliable: false


async def test_inspect_captures_event_image(fleet: SimFleet) -> None:
    fleet.services["go2_01"].arm("tester")
    async with Client(fleet.server("go2_01")) as client:
        result = await skill(client, SkillName.INSPECT, {"poi_id": "poi_kiosk", "dwell_s": 5})
    assert result.outcome == "succeeded", result
    image = Path(urlsplit(result.artifact_uri).path)
    data = image.read_bytes()
    assert data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9"
    assert len(data) <= 300 * 1024


async def test_argument_validation(fleet: SimFleet) -> None:
    fleet.services["go2_01"].arm("tester")
    async with Client(fleet.server("go2_01")) as client:
        both = await skill(client, SkillName.NAVIGATE_TO, {"pose": {"x": 1, "y": 1}, **KIOSK})
        unknown = await skill(client, SkillName.NAVIGATE_TO, {"poi_id": "poi_nowhere"})
        frame = await skill(
            client, SkillName.NAVIGATE_TO, {"pose": {"x": 1, "y": 1, "frame": "robot_map"}}
        )
        route = await skill(client, SkillName.FOLLOW_ROUTE, {"waypoints": [{"x": 1, "y": 0}] * 21})
        image = await skill(client, SkillName.CAPTURE_EVENT_IMAGE, {"max_kb": 301})
        bad_type = await client.call_tool("navigate_to", {"pose": {"x": "far"}})
    for result in (both, unknown, frame, route, image):
        assert result.outcome == "refused_precondition", result
    assert bad_type.is_error
    assert fleet.world.robot("go2_01").odometer_m == 0


async def test_follow_route_stops_on_first_failure(fleet: SimFleet) -> None:
    fleet.world.blocked_areas.append([(4, -1), (6, -1), (6, 1), (4, 1)])
    fleet.services["go2_01"].arm("tester")
    route = [{"x": 2, "y": 0}, {"x": 5, "y": 0}, {"x": 8, "y": 0}]
    async with Client(fleet.server("go2_01")) as client:
        result = await skill(client, SkillName.FOLLOW_ROUTE, {"waypoints": route})
    assert result.outcome == "no_path"
    assert result.detail.startswith("waypoint 1")
    assert abs(fleet.world.robot("go2_01").x - 2) < 0.25


async def test_preconditions_clearance_covariance_battery(fleet: SimFleet) -> None:
    service = fleet.services["go2_01"]
    service.arm("tester")
    robot = fleet.world.robot("go2_01")
    async with Client(fleet.server("go2_01")) as client:
        obstacle = fleet.world.add_obstacle(robot.x + 0.5, robot.y, 0.2)
        result = await skill(client, SkillName.NAVIGATE_TO, KIOSK)
        assert result.outcome == "refused_precondition" and "clearance" in result.detail
        fleet.world.obstacles.remove(obstacle)

        robot.cov_xy = 5.0
        result = await skill(client, SkillName.NAVIGATE_TO, KIOSK)
        assert result.outcome == "refused_precondition" and "covariance" in result.detail
        robot.cov_xy = 0.01

        robot.battery_pct = 10.0
        result = await skill(client, SkillName.NAVIGATE_TO, KIOSK)
        assert result.outcome == "refused_precondition" and "battery" in result.detail
    assert robot.odometer_m == 0


async def test_obstacle_on_route_blocks(fleet: SimFleet) -> None:
    robot = fleet.world.robot("go2_01")
    x0, y0 = robot.x, robot.y
    fleet.world.add_obstacle(x0 + 10, y0, 0.5)  # halfway along the straight drive
    fleet.services["go2_01"].arm("tester")
    async with Client(fleet.server("go2_01")) as client:
        result = await skill(client, SkillName.NAVIGATE_TO, {"pose": {"x": x0 + 20, "y": y0}})
    assert result.outcome == "blocked"
    assert robot.x < x0 + 10


async def test_timeout_from_manifest(fleet: SimFleet) -> None:
    service = fleet.services["go2_01"]
    service.manifest.skill_timeouts[SkillName.NAVIGATE_TO] = 5.0
    service.arm("tester")
    async with Client(fleet.server("go2_01")) as client:
        result = await skill(client, SkillName.NAVIGATE_TO, KIOSK)
    assert result.outcome == "timeout"
    assert result.duration_s == pytest.approx(5.0, abs=0.2)


async def test_hold_post_until_and_cancel(fleet: SimFleet) -> None:
    fleet.services["go2_01"].arm("tester")
    now = fleet.world.clock.now()
    async with Client(fleet.server("go2_01")) as client:
        held = await skill(
            client,
            SkillName.HOLD_POST,
            {"pose": {"x": 1, "y": 0}, "until": (now + timedelta(seconds=60)).isoformat()},
        )
        assert held.outcome == "succeeded"
        assert fleet.world.clock.now() >= now + timedelta(seconds=60)

        until = fleet.world.clock.now() + timedelta(seconds=600)
        hold = asyncio.create_task(
            skill(
                client, SkillName.HOLD_POST, {"pose": {"x": 1, "y": 0}, "until": until.isoformat()}
            )
        )
        await _wait_until(lambda: fleet.services["go2_01"]._active is SkillName.HOLD_POST)
        busy = await skill(client, SkillName.RETURN_HOME)
        assert busy.outcome == "refused_precondition" and "busy" in busy.detail
        assert (await skill(client, SkillName.CANCEL)).outcome == "succeeded"
        assert (await hold).outcome == "cancelled"
        past = await skill(
            client, SkillName.HOLD_POST, {"pose": {"x": 1, "y": 0}, "until": now.isoformat()}
        )
        assert past.outcome == "refused_precondition"


async def test_every_skill_call_is_a_decision(fleet: SimFleet) -> None:
    async with Client(fleet.server("go2_01")) as client:
        await call(client, SkillName.GET_STATE)
        await skill(client, SkillName.NAVIGATE_TO, KIOSK)
        await skill(client, SkillName.STOP)
    decisions = fleet.log.read(mission_id="m_test")
    assert [d.kind for d in decisions] == [DecisionKind.SKILL, DecisionKind.SKILL]
    first: Decision = decisions[0]
    assert first.actor == "go2_01.skill_mcp"
    assert first.output["result"]["outcome"] == "refused_disarmed"
    assert first.output["args"] == {"pose": None, "poi_id": "poi_kiosk"}
    assert first.policy["armed"] is False
    assert first.justification == "robot is disarmed"


# --------------------------------------------------------------------------- operator channel


async def test_operator_channel_requires_token(fleet: SimFleet) -> None:
    service = fleet.services["go2_01"]
    app = fleet.server("go2_01").streamable_http_app()
    with TestClient(app) as http:
        assert http.post("/operator/arm").status_code == 401
        bad = {"Authorization": "Bearer nope"}
        assert http.post("/operator/arm", headers=bad).status_code == 401
        assert service.armed is False
        ok = {"Authorization": f"Bearer {OPERATOR_TOKEN}"}
        response = http.post("/operator/arm", json={"operator": "alice"}, headers=ok)
        assert response.status_code == 200 and response.json()["armed"] is True
        assert service.arming.armed_by == "alice"
        assert http.post("/operator/disarm", headers=ok).json()["armed"] is False

    closed = fleet.services["summit_01"]
    from a6g_skill_mcp.server import build_mcp_server

    with TestClient(build_mcp_server(closed, operator_token="").streamable_http_app()) as http:
        assert http.post("/operator/arm", headers={"Authorization": "Bearer "}).status_code == 503
    assert closed.armed is False


async def test_cli_arm_and_disarm(fleet: SimFleet, monkeypatch, config_dir) -> None:
    seen: list[str] = []

    def fake_client() -> httpx.Client:
        # Each CLI call gets a fresh app (an MCP session manager runs once per instance).
        test_client = TestClient(fleet.server("go2_01").streamable_http_app())
        original = test_client.post

        def post(url: str, **kwargs):
            seen.append(url)
            return original(urlsplit(url).path, **kwargs)

        test_client.post = post  # type: ignore[method-assign]
        return test_client

    monkeypatch.setattr(cli, "_http_client", fake_client)
    runner = CliRunner()

    monkeypatch.delenv(cli.OPERATOR_TOKEN_ENV, raising=False)
    assert runner.invoke(cli.app, ["arm", "go2_01", "-c", str(config_dir)]).exit_code == 1

    monkeypatch.setenv(cli.OPERATOR_TOKEN_ENV, OPERATOR_TOKEN)
    result = runner.invoke(cli.app, ["arm", "go2_01", "-c", str(config_dir), "--operator", "bob"])
    assert result.exit_code == 0, result.output
    assert "ARMED" in result.stdout and fleet.services["go2_01"].armed
    assert seen == ["http://192.168.123.18:8201/operator/arm"]

    result = runner.invoke(cli.app, ["disarm", "go2_01", "-c", str(config_dir)])
    assert result.exit_code == 0 and not fleet.services["go2_01"].armed

    monkeypatch.setenv(cli.OPERATOR_TOKEN_ENV, "wrong")
    assert runner.invoke(cli.app, ["arm", "go2_01", "-c", str(config_dir)]).exit_code == 1
    assert runner.invoke(cli.app, ["arm", "ghost_01", "-c", str(config_dir)]).exit_code == 1
    assert not fleet.services["go2_01"].armed


async def test_leg_budget_follows_distance_and_scaled_campus(fleet, cfg) -> None:
    """A robot rocking a few cm from a waypoint must not burn the whole skill timeout."""
    import math

    from a6g_common.schemas import Pose2D, SkillName
    from a6g_skill_mcp.service import LEG_BASE_S, LEG_DETOUR, SkillService

    service = fleet.services["go2_01"]
    m = service.manifest
    pose = (await service.backend.get_state()).pose
    goal = Pose2D(x=pose.x + 6.0, y=pose.y + 8.0, yaw=0.0, frame=pose.frame)
    budget = await service._leg_budget(goal)
    assert budget == pytest.approx(
        LEG_BASE_S + LEG_DETOUR * 10.0 / m.max_speed_mps + math.pi * m.turn_in_place_s_per_rad
    )
    scaled = SkillService(
        service.backend, cfg.campus.model_copy(update={"scale": 0.25}), time_scale=2.0
    )
    full = m.skill_timeouts
    assert scaled.manifest.skill_timeouts[SkillName.FOLLOW_ROUTE] == max(
        full[SkillName.FOLLOW_ROUTE] * 0.25, 240.0
    )
    assert scaled.manifest.skill_timeouts[SkillName.HOLD_POST] == full[SkillName.HOLD_POST]
    assert await scaled._leg_budget(goal) == pytest.approx(2 * budget)
