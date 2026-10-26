"""P2 acceptance: robot agent.

- Given an envelope, the agent accepts, executes all tasks in the simulator and reports.
- A battery-infeasible task is refused with reason ``battery``.
- During simulated link loss, the robot finishes its current task, holds, queues reports
  and flushes them on reconnect.
"""

import contextlib
import json
import math

import httpx
from a6g_common.config import LinkLossPolicy
from a6g_common.models import fake_model
from a6g_common.schemas import (
    Award,
    CallForProposals,
    DecisionKind,
    LinkQuality,
    ReportKind,
    TrafficClass,
)
from a6g_robot_agent.graph import RETURN_HOME_TASK
from a6g_robot_agent.server import build_robot_app
from a6g_transport.messaging import TOPIC_KILLSWITCH, A6GMessage, MessageType, MessagingClient
from conftest import AgentEnv, inspect_poi, make_envelope, return_home, survey, wait_for


async def completed(env: AgentEnv, envelope_id: str = "env_001") -> bool:
    return (await env.state(envelope_id)).get("status") == "completed"


def pause_after(env: AgentEnv, metres: float = 2.0):
    """Freeze simulated time once the robot has moved ``metres`` more (then world.resume()).

    Register it *before* starting work: the simulator runs ahead during any await.
    """
    start = env.robot.odometer_m
    return env.world.pause_when(lambda w: w.robot(env.robot_id).odometer_m > start + metres)


def task_status(state: dict) -> dict[str, str]:
    return {tid: t["status"] for tid, t in state["tasks"].items()}


# --------------------------------------------------------------------------- acceptance 1


async def test_accepts_executes_all_tasks_and_reports(agent_env: AgentEnv) -> None:
    envelope = make_envelope(
        [
            survey("sec_north"),
            inspect_poi("poi_kiosk"),
            return_home(depends_on=["t_survey_sec_north", "t_inspect_poi_kiosk"]),
        ]
    )
    reply = await agent_env.agent.handle_envelope(envelope)
    assert reply.decision == "accept"
    assert sorted(reply.accepted_task_ids) == sorted(t.task_id for t in envelope.tasks)

    await wait_for(lambda: completed(agent_env), what="envelope completed")
    state = await agent_env.state("env_001")
    assert set(task_status(state).values()) == {"succeeded"}
    assert state["sequence"][-1] == "t_home"  # dependency respected
    robot, home = agent_env.robot, agent_env.fleet.cfg.campus.staging
    assert math.hypot(robot.x - home.x, robot.y - home.y) < 0.3  # back at staging

    # Reported upstream: one skill result per task, the decision, and completion.
    reports = agent_env.uplink.reports
    results = [r.payload["task_id"] for r in reports if r.kind == ReportKind.SKILL_RESULT]
    assert sorted(results) == sorted(t.task_id for t in envelope.tasks)
    assert [r.payload["status"] for r in agent_env.uplink.of_kind("envelope_status")] == [
        "completed"
    ]
    accepted = [r for r in agent_env.uplink.of_kind("decision") if r.payload["kind"] == "accept"]
    assert len(accepted) == 1
    assert all(r.mission_id == "m_test" and r.envelope_id == "env_001" for r in reports)
    assert state["outbox"] == []

    decisions = agent_env.fleet.log.read(mission_id="m_test")
    accept = next(d for d in decisions if d.kind is DecisionKind.ACCEPT)
    assert accept.actor == "go2_01.agent"
    assert accept.policy["cost_weights"]["travel_time"] == 1.0
    assert accept.justification.startswith("Accepting all 3 tasks")  # template (no model)


# --------------------------------------------------------------------------- acceptance 2


async def test_battery_infeasible_task_refused_with_reason_battery(agent_env: AgentEnv) -> None:
    agent_env.robot.battery_pct = 30.0  # floor 20 + margin 5 leaves 5 % of travel
    envelope = make_envelope([survey("sec_east")])
    reply = await agent_env.agent.handle_envelope(envelope)

    assert reply.decision == "refuse"
    assert reply.refusals == {"t_survey_sec_east": "battery"}
    assert reply.accepted_task_ids == []
    state = await agent_env.state("env_001")
    assert state["status"] == "refused" and state.get("dispatch") is None
    assert agent_env.robot.odometer_m == 0

    refuse = next(d for d in agent_env.fleet.log.read("m_test") if d.kind is DecisionKind.REFUSE)
    assert refuse.output["refusals"] == {"t_survey_sec_east": "battery"}
    assert "battery would end at" in refuse.output["details"]["t_survey_sec_east"]
    assert refuse.policy["battery_floor_pct"] == 20.0


async def test_a_lap_under_way_is_kept_when_its_envelope_is_resent(agent_env: AgentEnv) -> None:
    """A new version (or a resync after a 5G shadow) re-sends the lap the robot is running:
    only what is left of it counts, and it is kept. Judging the whole lap again from the
    lower battery refused it and stopped the robot halfway, idle."""
    paused = pause_after(agent_env, 10.0)
    reply = await agent_env.agent.handle_envelope(make_envelope([survey("sec_east")]))
    assert reply.decision == "accept"
    await paused.wait()
    agent_env.robot.battery_pct = 30.0  # too low for a whole lap from the start
    again = make_envelope([survey("sec_east")], version=2)
    reply = await agent_env.agent.handle_envelope(again)
    assert reply.decision == "accept" and reply.accepted_task_ids == ["t_survey_sec_east"]
    state = await agent_env.state("env_001")
    assert state["current_task_id"] == "t_survey_sec_east"
    assert task_status(state) == {"t_survey_sec_east": "dispatched"}
    # New work (not under way) is still judged in full, after what is left of the lap.
    fresh = make_envelope([survey("sec_east"), inspect_poi("poi_kiosk")], version=3)
    reply = await agent_env.agent.handle_envelope(fresh)
    assert reply.refusals == {"t_inspect_poi_kiosk": "battery"}
    agent_env.world.resume()


async def test_partially_feasible_envelope_is_renegotiated(agent_env: AgentEnv) -> None:
    # (A patrol lap that does not fit is planned after a recharge; an inspection is not.)
    agent_env.robot.battery_pct = 30.0
    envelope = make_envelope([inspect_poi("poi_kiosk"), inspect_poi("poi_bus_stop")])
    reply = await agent_env.agent.handle_envelope(envelope)
    assert reply.decision == "renegotiate"
    assert reply.accepted_task_ids == ["t_inspect_poi_bus_stop"]  # 36 m away
    assert reply.refusals == {"t_inspect_poi_kiosk": "battery"}  # 182 m away
    await wait_for(lambda: completed(agent_env))
    assert task_status(await agent_env.state("env_001")) == {
        "t_inspect_poi_kiosk": "refused",
        "t_inspect_poi_bus_stop": "succeeded",
    }


async def test_capability_and_region_refusals(agent_env: AgentEnv) -> None:
    payload = survey(
        "sec_north",
        task_id="t_payload",
    )
    payload = payload.model_copy(update={"required_capabilities": ["payload"]})
    north_only = [(10.0, 20.0), (60.0, 20.0), (60.0, 60.0), (10.0, 60.0)]
    envelope = make_envelope([payload, survey("sec_south")], region=north_only)
    reply = await agent_env.agent.handle_envelope(envelope)
    assert reply.decision == "refuse"
    assert reply.refusals == {"t_payload": "capability", "t_survey_sec_south": "region"}


# --------------------------------------------------------------------------- acceptance 3


async def test_link_loss_finish_current_hold_queue_and_flush(agent_env: AgentEnv) -> None:
    env, world = agent_env, agent_env.world
    # P2 acceptance for the finish_current_then_hold policy (still selectable; the default
    # became hold_then_recover in SPEC A1 12.1, tested in test_link_recovery.py).
    await env.stop_agent()
    env.fleet.cfg.fleet.defaults.link_loss_policy = LinkLossPolicy.FINISH_CURRENT_THEN_HOLD
    await env.start_agent()
    envelope = make_envelope(
        [
            inspect_poi("poi_bus_stop"),
            inspect_poi("poi_library_door"),
            return_home(depends_on=["t_inspect_poi_bus_stop", "t_inspect_poi_library_door"]),
        ]
    )
    moving = pause_after(env)
    assert (await env.agent.handle_envelope(envelope)).decision == "accept"

    # Lose the link while the first task is under way (sim time frozen at that moment).
    await moving.wait()
    first = (await env.state("env_001"))["current_task_id"]
    assert first == "t_inspect_poi_bus_stop"  # nearest first
    delivered_before = len(env.uplink.reports)
    world.inject_link_loss("go2_01", 1e9)
    world.resume()

    # The current task finishes, then the robot holds.
    async def holding() -> bool:
        s = await env.state("env_001")
        return s.get("holding") and s["current_task_id"] is None

    await wait_for(holding, what="robot holding")
    state = await env.state("env_001")
    assert state["hold_reason"] == "link_lost" and state["status"] == "holding"
    assert task_status(state) == {
        "t_inspect_poi_bus_stop": "succeeded",
        "t_inspect_poi_library_door": "pending",
        "t_home": "pending",
    }
    assert env.robot.job is None  # not moving
    odometer_at_hold = env.robot.odometer_m
    kiosk = env.fleet.cfg.campus.poi("poi_bus_stop").pose
    assert math.hypot(env.robot.x - kiosk.x, env.robot.y - kiosk.y) < 0.3  # at the kiosk

    # Nothing was delivered while lost; the reports are queued, in order.
    assert len(env.uplink.reports) == delivered_before
    queued = state["outbox"]
    kinds = [q["kind"] for q in queued]
    assert "skill_result" in kinds and "decision" in kinds
    queued_ids = [q["report_id"] for q in queued]
    # (Agents also log a fallback when they notice there is no coordinator: pick ours.)
    link_fallbacks = [
        d
        for d in env.fleet.log.read("m_test")
        if d.kind is DecisionKind.FALLBACK and "link_loss_policy" in d.policy
    ]
    assert link_fallbacks[0].policy["link_loss_policy"] == "finish_current_then_hold"

    # No new tasks are accepted while the link is lost.
    late = make_envelope([survey("sec_south")], envelope_id="env_late")
    reply = await env.agent.handle_envelope(late)
    assert reply.decision == "refuse" and reply.refusals == {"t_survey_sec_south": "link"}

    # Reconnect: queued reports flush in their original order, then work resumes.
    env.robot.link_lost_until = None
    await wait_for(lambda: completed(env), what="envelope completed after reconnect")
    delivered = [r.report_id for r in env.uplink.reports[delivered_before:]]
    assert [rid for rid in delivered if rid in queued_ids] == queued_ids
    assert env.robot.odometer_m > odometer_at_hold
    assert set(task_status(await env.state("env_001")).values()) == {"succeeded"}
    assert (await env.state("env_001"))["outbox"] == []


async def test_link_loss_return_home_policy(agent_env: AgentEnv) -> None:
    env = agent_env
    await env.stop_agent()
    env.fleet.cfg.fleet.defaults.link_loss_policy = LinkLossPolicy.RETURN_HOME
    await env.start_agent()
    envelope = make_envelope([inspect_poi("poi_bus_stop"), inspect_poi("poi_library_door")])
    moving = pause_after(env)
    await env.agent.handle_envelope(envelope)
    await moving.wait()
    env.world.inject_link_loss("go2_01", 1e9)
    env.world.resume()

    async def home_and_holding() -> bool:
        s = await env.state("env_001")
        return s.get("holding") and s["current_task_id"] is None and s.get("returned_home")

    await wait_for(home_and_holding)
    home = env.fleet.cfg.campus.staging
    assert math.hypot(env.robot.x - home.x, env.robot.y - home.y) < 0.3
    assert task_status(await env.state("env_001"))["t_inspect_poi_bus_stop"] == "succeeded"
    assert RETURN_HOME_TASK not in (await env.state("env_001"))["tasks"]


# --------------------------------------------------------------------------- robustness


async def test_survives_process_restart_without_repeating_work(agent_env: AgentEnv) -> None:
    env = agent_env
    envelope = make_envelope([inspect_poi("poi_bus_stop"), inspect_poi("poi_library_door")])
    # Freeze sim time once the robot is on its way to the second POI.
    reached = pause_after(env, 40.0)
    await env.agent.handle_envelope(envelope)
    await reached.wait()
    assert (await env.state("env_001"))["current_task_id"] == "t_inspect_poi_library_door"
    skill_calls_before = len(
        [d for d in env.fleet.log.read("m_test") if d.kind is DecisionKind.SKILL]
    )

    await env.stop_agent()  # crash: process gone, edge never involved
    await env.start_agent()  # same local SQLite file
    env.world.resume()
    await wait_for(lambda: completed(env))

    state = await env.state("env_001")
    assert task_status(state) == {
        "t_inspect_poi_bus_stop": "succeeded",
        "t_inspect_poi_library_door": "succeeded",
    }
    inspects = [
        d
        for d in env.fleet.log.read("m_test")
        if d.kind is DecisionKind.SKILL and d.inputs_ref == "skill:inspect"
    ]
    near_runs = [d for d in inspects if d.output["args"]["poi_id"] == "poi_bus_stop"]
    assert len(near_runs) == 1  # completed work is not repeated
    assert len(inspects) >= 2 and skill_calls_before >= 1


async def test_killswitch_stops_and_holds_until_resume(agent_env: AgentEnv) -> None:
    env = agent_env
    moving = pause_after(env)
    await env.agent.handle_envelope(make_envelope([inspect_poi("poi_library_door")]))
    await moving.wait()
    await env.bus.publish(TOPIC_KILLSWITCH, json.dumps({"state": "on"}), qos=1, retain=True)

    async def stopped() -> bool:
        s = await env.state("env_001")
        return s.get("holding") and s["current_task_id"] is None

    await wait_for(stopped)
    assert env.robot.job is None and env.robot.stop_count >= 1
    assert env.agent.killswitch_engaged
    state = await env.state("env_001")
    assert state["hold_reason"] == "killswitch"
    assert task_status(state) == {"t_inspect_poi_library_door": "pending"}  # not failed

    env.world.resume()
    await env.agent.handle_operator("resume")
    await wait_for(lambda: completed(env))


async def test_observation_triaged_and_escalated(agent_env: AgentEnv) -> None:
    env = agent_env
    env.world.inject_anomaly("poi_kiosk", "unattended backpack")
    await env.agent.handle_envelope(make_envelope([inspect_poi("poi_kiosk")]))
    await wait_for(lambda: completed(env))
    await env.agent.idle()
    await wait_for(lambda: env.uplink.of_kind("observation"))

    obs = env.uplink.of_kind("observation")[0]
    assert obs.traffic_class == TrafficClass.EVENT_IMAGE
    assert obs.payload["label"] == "unattended backpack"
    assert obs.payload["requires_confirmation"] is True
    assert obs.payload["evidence"]["image_uri"].startswith("file://")
    escalations = [d for d in env.fleet.log.read("m_test") if d.kind is DecisionKind.ESCALATE]
    assert escalations and escalations[0].output["label"] == "unattended backpack"


async def test_llm_writes_justification_only(agent_env: AgentEnv) -> None:
    """With the cognitive core off (SPEC A2.3 off), the model only words the code's decision."""
    env = agent_env
    env.fleet.cfg.fleet.defaults.agent.cognition = False
    await env.stop_agent()
    await env.start_agent(model=fake_model(["The kiosk is close and the battery is ample. Extra."]))
    reply = await env.agent.handle_envelope(make_envelope([inspect_poi("poi_kiosk")]))
    assert reply.decision == "accept"  # decided by code
    assert reply.justification == "The kiosk is close and the battery is ample."

    class Down:
        async def ainvoke(self, *_a, **_k):
            raise ConnectionError("endpoint down")

    await env.stop_agent()
    await env.start_agent(model=Down())
    reply = await env.agent.handle_envelope(
        make_envelope([inspect_poi("poi_kiosk")], envelope_id="env_002")
    )
    assert reply.justification.startswith("Accepting all 1 tasks")  # template fallback
    env.fleet.cfg.fleet.defaults.agent.cognition = True


async def test_state_published_on_telemetry_bus(agent_env: AgentEnv) -> None:
    await wait_for(lambda: agent_env.bus.messages("a6g/fleet/go2_01/state"))
    state = agent_env.bus.messages("a6g/fleet/go2_01/state")[-1].json()
    assert state["robot_id"] == "go2_01" and state["armed"] is True


# ------------------------------------------------------------------- contract-net participant


async def test_cfp_bid_and_idempotent_award(agent_env: AgentEnv) -> None:
    env = agent_env
    north = [(0.0, -80.0), (60.0, -80.0), (60.0, 30.0), (0.0, 30.0)]  # bus stop + 9B door
    await env.agent.handle_envelope(make_envelope([inspect_poi("poi_bus_stop")], region=north))
    now = env.world.clock.now()
    cfp = CallForProposals(
        cfp_id="cfp_1",
        mission_id="m_test",
        envelope_id="env_peer",
        initiator="summit_01",
        task=inspect_poi("poi_library_door", task_id="t_peer_door"),
        issued_at=now,
    )
    bid = await env.agent.handle_cfp(cfp)
    assert bid.cost is not None and bid.cost > 0 and bid.refusal_reason is None

    far = CallForProposals(
        cfp_id="cfp_2",
        mission_id="m_test",
        envelope_id="env_peer",
        initiator="summit_01",
        task=inspect_poi("poi_bike_rack", task_id="t_peer_bike"),
        issued_at=now,
    )
    assert (await env.agent.handle_cfp(far)).refusal_reason == "region"

    award = Award(
        award_id="aw_1",
        cfp_id="cfp_1",
        mission_id="m_test",
        envelope_id="env_peer",
        task_id="t_peer_door",
        awarded_by="summit_01",
        awarded_to="go2_01",
        cost=bid.cost,
        stamp=now,
    )
    assert (await env.agent.handle_award(award))["accepted"] is True
    assert (await env.agent.handle_award(award))["accepted"] is True  # idempotent
    await wait_for(lambda: completed(env))
    state = await env.state("env_001")
    assert task_status(state)["t_peer_door"] == "succeeded"
    awards = [d for d in env.fleet.log.read("m_test") if d.kind is DecisionKind.AWARD]
    assert len(awards) == 1


# --------------------------------------------------------------------------- A2A


async def test_a2a_agent_card_and_envelope_round_trip(agent_env: AgentEnv) -> None:
    base = "http://go2-01.test"
    app = build_robot_app(agent_env.agent, base)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=base) as http:
        card = (await http.get("/.well-known/agent-card.json")).json()
        assert card["name"] == "a6g-robot-go2_01"
        assert {s["id"] for s in card["skills"]} == {
            "envelope",
            "cfp",
            "award",
            "operator",
            "state",
        }

        client = MessagingClient(base, httpx_client=http)
        envelope = make_envelope([inspect_poi("poi_kiosk")])
        reply = await client.send(
            A6GMessage(MessageType.ENVELOPE, envelope.model_dump(mode="json"))
        )
    assert reply.type is MessageType.ENVELOPE_REPLY
    assert reply.payload["decision"] == "accept"
    assert reply.payload["version"] == 1  # integers survive the A2A data part


async def test_envelope_redelivery_is_idempotent(agent_env: AgentEnv) -> None:
    envelope = make_envelope([inspect_poi("poi_kiosk")])
    first = await agent_env.agent.handle_envelope(envelope)
    again = await agent_env.agent.handle_envelope(envelope)
    assert again == first
    accepts = [d for d in agent_env.fleet.log.read("m_test") if d.kind is DecisionKind.ACCEPT]
    assert len(accepts) == 1


async def test_link_quality_lost_reported_by_state(agent_env: AgentEnv) -> None:
    agent_env.world.inject_link_loss("go2_01", 1e9)
    await wait_for(lambda: agent_env.agent.link_quality is LinkQuality.LOST)


# --------------------------------------------------------------------------- operator kill-switch


async def test_cli_killswitch_both_paths(agent_env: AgentEnv, monkeypatch) -> None:
    from a6g_cli import main as cli

    env = agent_env
    monkeypatch.setattr(cli, "_bus", lambda cfg: contextlib.nullcontext(env.bus))
    monkeypatch.setattr(cli, "_mcp_target", lambda robot: env.fleet.server(robot.robot_id))
    moving = pause_after(env)
    await env.agent.handle_envelope(make_envelope([inspect_poi("poi_library_door")]))
    await moving.wait()

    report = await cli._killswitch(env.fleet.cfg, True, ["go2_01"], "tester", targeted=True)
    assert report == {"go2_01": {"mqtt": "published", "mcp_stop": "succeeded"}}
    retained = env.bus.retained["a6g/killswitch"].json()
    assert retained == {"state": "on", "robot_id": "go2_01", "issued_by": "tester"}

    async def holding() -> bool:
        return (await env.state("env_001")).get("hold_reason") == "killswitch"

    await wait_for(holding)
    assert env.robot.job is None and env.robot.stop_count >= 1
    env.world.resume()


def test_cli_killswitch_reports_failure_when_no_path_works(monkeypatch, config_dir) -> None:
    from a6g_cli import main as cli
    from typer.testing import CliRunner

    class DeadBroker:
        async def __aenter__(self):
            raise ConnectionRefusedError("broker down")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(cli, "_bus", lambda cfg: DeadBroker())
    monkeypatch.setattr(cli, "_mcp_target", lambda robot: object())  # MCP unreachable
    result = CliRunner().invoke(cli.app, ["killswitch", "on", "-c", str(config_dir)])
    assert result.exit_code == 1
    assert "NOT CONFIRMED" in result.stderr
    assert "go2_01: mqtt=FAILED" in result.stdout and "summit_01: mqtt=FAILED" in result.stdout


def test_cli_killswitch_one_path_is_enough(monkeypatch, config_dir) -> None:
    from a6g_cli import main as cli
    from a6g_transport.messaging import memory_bus
    from typer.testing import CliRunner

    bus = memory_bus()
    monkeypatch.setattr(cli, "_bus", lambda cfg: contextlib.nullcontext(bus))
    monkeypatch.setattr(cli, "_mcp_target", lambda robot: object())  # MCP down, MQTT up
    result = CliRunner().invoke(cli.app, ["killswitch", "on", "-c", str(config_dir)])
    assert result.exit_code == 0, result.output
    assert bus.retained["a6g/killswitch"].json()["robot_id"] is None  # whole fleet


def test_survey_waypoints_face_along_their_legs() -> None:
    import math

    from a6g_common.schemas import Pose2D
    from a6g_robot_agent.graph import route_headings

    square = [Pose2D(x=0, y=0), Pose2D(x=10, y=0), Pose2D(x=10, y=5), Pose2D(x=0, y=5)]
    yaws = [w.yaw for w in route_headings(square)]
    assert yaws == [0.0, 0.0, round(math.pi / 2, 6), round(math.pi, 6)]
    assert route_headings(square[:1]) == square[:1]


async def test_patrol_laps_cycle_through_every_sector(agent_env: AgentEnv) -> None:
    """The summit kept alternating the two western sectors' new laps and never reached the
    far East corridor: a robot finishes lap n everywhere before starting lap n+1."""
    from a6g_common.footpaths import walker_for

    campus = agent_env.fleet.cfg.campus
    # West, between north and south, on a footpath; three laps on one battery for the test.
    x, y = walker_for(campus.route_graph).project((-55.0, -20.0)).point
    agent_env.robot.x, agent_env.robot.y = x, y
    deps = agent_env.agent.deps
    deps.manifest = deps.manifest.model_copy(update={"battery_drain_pct_per_m": 0.01})
    envelope = make_envelope(
        [
            survey("sec_east", task_id="t_east"),  # lap 1, far away
            survey("sec_north", task_id="t_north-lap2"),  # lap 2, near
            survey("sec_south", task_id="t_south-lap2"),  # lap 2, nearest
        ]
    )
    reply = await agent_env.agent.handle_envelope(envelope)
    assert reply.decision == "accept"
    state = await agent_env.state("env_001")
    assert state["current_task_id"] == "t_east"
    assert state["sequence"][0] == "t_east"


def test_lap_numbers_are_read_from_task_ids() -> None:
    from a6g_robot_agent.graph import lap_number

    assert lap_number("rou-survey_sector-sec_east") == 1
    assert lap_number("rou-survey_sector-sec_east-summit_01-lap3") == 3
