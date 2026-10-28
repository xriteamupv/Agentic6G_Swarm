"""Chat handling, as exercised through agent-chat-ui."""

import asyncio
import json
import time
from pathlib import Path

from a6g_common.config import load_config
from a6g_common.models import fake_model
from a6g_devstack import SimStack
from langchain_core.messages import AIMessage

COMPOUND = "Patrol the campus, Escort the visitor to the bus stop, status and weather alert."


async def test_compound_message_with_weather_alert_is_an_emergency_without_duplicates(
    tmp_path: Path,
) -> None:
    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    # The model (wrongly) calls everything "interactive" and re-plans the patrol.
    model = fake_model(
        [
            '{"intent": "goal", "posture": "interactive"}',
            '{"tasks": [{"kind": "survey_sector", "target_type": "sector",'
            ' "target_id": "sec_north"},'
            ' {"kind": "escort", "target_type": "poi", "target_id": "poi_bus_stop"},'
            ' {"kind": "hold_post", "target_type": "poi", "target_id": "poi_bike_rack"}],'
            ' "summary": "mixed"}',
        ]
    )
    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        stack.world.pause()
        await stack.mission("Patrol the campus")  # routine surveys running
        stack.coordinator.deps.model = model
        stack.coordinator.graph = (
            __import__("a6g_coordinator.graph", fromlist=["x"])
            .build_graph(stack.coordinator.deps)
            .compile(checkpointer=stack.coordinator.graph.checkpointer, store=stack.store)
        )
        state = await stack.coordinator.chat("m1", COMPOUND)
    finally:
        await stack.stop()
    assert state["posture"] == "emergency"  # never downgraded by the model
    surveys = [t for t, e in state["tasks"].items() if e["spec"]["kind"] == "survey_sector"]
    assert sorted(surveys) == [
        "rou-survey_sector-sec_east",
        "rou-survey_sector-sec_north",
        "rou-survey_sector-sec_south",
    ]  # the LLM's re-planned survey was not duplicated
    assert all(state["tasks"][t]["status"] == "preempted" for t in surveys)
    assert "Paused for the emergency" in state["messages"][-1].content  # status answered too


def test_narration_uses_campus_names_not_ids(cfg) -> None:
    from a6g_coordinator.narrate import Narrator
    from conftest import inspect_poi, return_home, survey

    narr = Narrator(cfg.campus)
    assert narr.task(survey("sec_north")) == "patrol the North-west walk"
    assert narr.task(inspect_poi("poi_bike_rack")) == "check the bike rack"
    assert narr.task_done(survey("sec_east")) == "finished patrolling the East corridor"
    assert narr.task(return_home()) == "return to base"
    tasks = {"t1": {"spec": survey("sec_south").model_dump(mode="json")}}
    fleet = {"go2_01": {"platform": "go2"}}
    text = narr.allocation({"go2_01": ["t1"]}, tasks, fleet)
    assert text == "Assignments:\n- **go2_01** (Go2) will patrol the South plaza."
    assert "sec_" not in narr.plan("routine", [survey("sec_north")])


def test_envelope_region_covers_every_campus_target(cfg) -> None:
    from a6g_common.geometry import point_in_polygon
    from a6g_coordinator.graph import campus_region

    region = campus_region(cfg)
    c = cfg.campus
    targets = [(p.pose.x, p.pose.y) for p in c.pois]
    targets += [(r.pose.x, r.pose.y) for r in c.link_recovery_points]
    targets += [(w.x, w.y) for s in c.sectors for w in s.survey_waypoints]
    targets.append((c.staging.x, c.staging.y))
    assert all(point_in_polygon(t, region) for t in targets)


async def test_process_is_streamed_as_tool_calls_with_results(tmp_path: Path) -> None:
    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        stack.world.pause()

        # This test streams the graph directly (as the Agent Server does), bypassing the
        # coordinator's per-mission queue: keep robot reports out so runs don't overlap.
        async def reject(report):
            return False

        stack.coordinator.handle_report = reject
        updates = []
        async for update in stack.coordinator.graph.astream(
            {"messages": [("user", "Patrol the campus")]},
            {"configurable": {"thread_id": "m1"}},
            stream_mode="updates",
        ):
            updates.append(update)
        state = await stack.coordinator.state("m1")
    finally:
        await stack.stop()
    calls = [tc for m in state["messages"] if m.type == "ai" for tc in m.tool_calls]
    results = {m.tool_call_id for m in state["messages"] if m.type == "tool"}
    names = [c["name"] for c in calls if c["name"] != "robot_report"]  # reports may interleave
    assert names == [
        "understand_request",
        "plan_mission",
        "validate_plan",
        "query_robot_states",
        "allocate_tasks",
        "send_envelopes",
    ]
    assert {c["id"] for c in calls} == results  # every call shown gets its result
    tool_results = [json.loads(m.content) for m in state["messages"] if m.type == "tool"]
    assert all("latency" in r for r in tool_results)  # every step reports its latency
    # The slow steps are announced by the node before the one that performs them.
    nodes_with_messages = [n for u in updates for n, v in u.items() if v and v.get("messages")]
    assert nodes_with_messages[:2] == ["ingest", "decompose"]
    summaries = [m for m in state["messages"] if m.type == "ai" and isinstance(m.content, str)]
    assert any("Assignments:" in m.content for m in summaries)


async def test_emergency_all_clear_resumes_paused_patrols(tmp_path: Path) -> None:
    """Operator dialogue: patrol -> weather alert -> 'continue patrolling' -> back to patrol."""
    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        stack.world.pause()
        await stack.mission("Patrol the campus")
        mid = "m1"
        state = await stack.coordinator.chat(mid, "Weather alert")
        assert state["posture"] == "emergency"
        surveys = [t for t, e in state["tasks"].items() if e["spec"]["kind"] == "survey_sector"]
        assert surveys and all(state["tasks"][t]["status"] == "preempted" for t in surveys)

        state = await stack.coordinator.chat(mid, "Continue patrolling")
        asks = await stack.coordinator.interrupts(mid)  # a new order needs approval
        assert asks and asks[0]["action_requests"][0]["name"] == "end_emergency"
        state = await stack.coordinator.resume(mid, [{"type": "reject"}])
        assert state["posture"] == "emergency"
        assert all(state["tasks"][t]["status"] == "preempted" for t in surveys)

        state = await stack.coordinator.chat(mid, "Put all the robots to patrol mode again")
    finally:
        await stack.stop()
    assert state["posture"] == "routine"
    assert "All clear" in state["messages"][-1].content
    assert all(state["tasks"][t]["status"] in ("assigned", "in_progress") for t in surveys)
    posts = [e for e in state["tasks"].values() if e["spec"]["kind"] == "hold_post"]
    assert posts and all(e["status"] == "cancelled" for e in posts)


async def test_emergency_posts_hold_until_the_all_clear(tmp_path: Path) -> None:
    """Posts do not "finish" after their 600 s estimate, leaving the mission at "9 tasks
    done out of 12" with the patrols still stopped: posts hold period after period, the
    mission waits, and the all clear brings the patrols back."""
    from conftest import wait_for

    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        await stack.mission("Patrol the campus")
        state = await stack.coordinator.chat("m1", "Weather alert")
        posts = [t for t, e in state["tasks"].items() if e["spec"]["kind"] == "hold_post"]
        surveys = [t for t, e in state["tasks"].items() if e["spec"]["kind"] == "survey_sector"]
        assert posts and all(state["tasks"][t]["spec"]["until_cleared"] for t in posts)
        t0 = stack.world.clock.now()

        async def three_periods() -> bool:  # 600 s each
            return (stack.world.clock.now() - t0).total_seconds() > 3 * 600

        await wait_for(three_periods, 120, "three hold periods")
        await stack.coordinator.drain()
        state = await stack.coordinator.state("m1")
        assert state["status"] == "active" and state["posture"] == "emergency"
        assert all(state["tasks"][t]["status"] in ("assigned", "in_progress") for t in posts)
        assert all(state["tasks"][t]["status"] == "preempted" for t in surveys)
        assert not any("Mission complete" in m.text for m in state["messages"])

        state = await stack.coordinator.chat("m1", "All clear")
    finally:
        await stack.stop()
    assert state["posture"] == "routine"
    assert all(state["tasks"][t]["status"] == "cancelled" for t in posts)
    assert all(state["tasks"][t]["status"] in ("assigned", "in_progress") for t in surveys)


async def test_unsupported_request_gets_an_honest_reply(tmp_path: Path) -> None:
    from a6g_coordinator.decompose import interpret_chat

    model = fake_model(['{"intent": "unsupported", "summary": "robots have no meeting task"}'])
    intent, _ = await interpret_chat("All robots meet in south plaza", True, model)
    assert intent.intent == "unsupported"
    intent, _ = await interpret_chat("The emergency is over", True, None)
    assert intent.intent == "all_clear"


async def test_new_conversation_ends_the_previous_mission(tmp_path: Path) -> None:
    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        stack.world.pause()
        await stack.mission("Patrol the campus", mission_id="m1")
        started = time.perf_counter()
        state = await stack.coordinator.chat("m2", "Escort the visitor to the bus stop")
        took = time.perf_counter() - started
        # Robots are released by m2's own run, before m2 allocates (not by m1's queued run).
        busy = {
            rid: [t for t in proc.agent._threads if "env-m1-" in t]
            for rid, proc in stack.robots.items()
            if proc.agent is not None
        }
        await stack.coordinator.drain()
        old = await stack.coordinator.state("m1")
        active = await stack.coordinator.active_missions()
    finally:
        await stack.stop()
    assert took < 5.0
    assert active == ["m2"]
    assert old["status"] == "superseded"
    assert not [t for t, e in old["tasks"].items() if e["status"] in ("assigned", "in_progress")]
    assert busy and not any(busy.values())  # every robot dropped the old mission's envelope
    assert "ended 1 previous mission" in "\n".join(str(m.content) for m in state["messages"])
    escort = state["tasks"]["int-escort-poi_bus_stop"]
    assert escort["status"] in ("assigned", "in_progress")


ORDERS = (
    "now make the go2_02 return to base, the go2_01 patrol the north-west walk, "
    "and the summit to inspect the bus stop"
)
EXPECTED = {
    "rou-return_home-go2_02": "go2_02",
    "rou-survey_sector-sec_north": "go2_01",
    "rou-inspect_poi-poi_bus_stop": "summit_01",
}


async def test_orders_for_named_robots_are_pinned_to_them(tmp_path: Path) -> None:
    from a6g_common.schemas import Posture
    from a6g_coordinator.decompose import decompose_goal

    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    robots = {"go2_01": "go2", "go2_02": "go2", "summit_01": "summit"}
    # Template path (no LLM).
    tasks, pinned, used_template, *_ = await decompose_goal(
        ORDERS, Posture.ROUTINE, cfg.campus, None, (), robots
    )
    assert used_template and pinned == EXPECTED and {t.task_id for t in tasks} == set(EXPECTED)
    # LLM path: the model names the robots.
    model = fake_model(
        [
            '{"tasks": [{"kind": "return_home", "target_type": "staging", "robot_id": "go2_02"},'
            ' {"kind": "survey_sector", "target_type": "sector", "target_id": "sec_north",'
            ' "robot_id": "go2_01"},'
            ' {"kind": "inspect_poi", "target_type": "poi", "target_id": "poi_bus_stop",'
            ' "robot_id": "summit_01"}], "summary": "three orders"}'
        ]
    )
    _, pinned, used_template, *_ = await decompose_goal(
        ORDERS, Posture.ROUTINE, cfg.campus, model, (), robots
    )
    assert not used_template and pinned == EXPECTED

    # End to end: the allocator honours the operator, even against cost.
    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        stack.world.pause()
        state = await stack.mission(ORDERS)
    finally:
        await stack.stop()
    assert {t: state["tasks"][t]["robot_id"] for t in EXPECTED} == EXPECTED


async def test_order_moves_running_work_to_the_named_robot(tmp_path: Path) -> None:
    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        stack.world.pause()
        state = await stack.mission("Patrol the campus")
        north = state["tasks"]["rou-survey_sector-sec_north"]["robot_id"]
        other = next(r for r in ("go2_01", "go2_02", "summit_01") if r != north)
        state = await stack.coordinator.chat("m1", f"{other} patrol the north-west walk")
    finally:
        await stack.stop()
    t = state["tasks"]["rou-survey_sector-sec_north"]
    assert (t["robot_id"], t["pinned_to"]) == (other, other)
    assert "As you asked" in "\n".join(str(m.content) for m in state["messages"])
    # Its previous patrol went back to the fleet rather than being dropped.
    assert all(
        e["status"] in ("assigned", "in_progress")
        for k, e in state["tasks"].items()
        if k.startswith("rou-survey")
    )


async def test_model_priority_is_ignored(tmp_path: Path) -> None:
    """A model's 'priority 1 = do first' made a robot decline (old envelope at 40)."""
    from a6g_common.schemas import Posture
    from a6g_coordinator.decompose import decompose_goal

    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    model = fake_model(
        [
            '{"tasks": [{"kind": "survey_sector", "target_type": "sector",'
            ' "target_id": "sec_north", "priority": 1}], "summary": "patrol"}'
        ]
    )
    tasks, *_ = await decompose_goal("Patrol the north", Posture.ROUTINE, cfg.campus, model)
    assert [t.priority for t in tasks] == [40]


def test_expired_envelopes_are_not_commitments(agent_env) -> None:
    from datetime import timedelta

    robot = agent_env.agent
    now = robot.deps.clock.now()
    assert robot._expired({"valid_until": (now - timedelta(seconds=1)).isoformat()})
    assert not robot._expired({"valid_until": (now + timedelta(hours=1)).isoformat()})
    assert not robot._expired({})


async def test_robot_out_of_coverage_is_released_when_it_reports(tmp_path: Path) -> None:
    """A robot in a 5G shadow when its mission is superseded stops patrolling for the old
    mission once it reports again."""
    from datetime import UTC, datetime

    from a6g_common.schemas import Report, ReportKind, TrafficClass

    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        stack.world.pause()
        await stack.mission("Patrol the campus", mission_id="m1")
        links = stack.coordinator.deps.links

        class Shadowed:
            async def send_envelope(self, envelope):
                raise ConnectionError("summit_01 is out of 5G coverage")

            async def get_state(self):
                raise ConnectionError("summit_01 is out of 5G coverage")

        stack.coordinator.deps.links = lambda rid, e: (
            Shadowed() if rid == "summit_01" else links(rid, e)
        )
        await stack.coordinator.chat("m2", "Escort the visitor to the bus stop")
        await stack.coordinator.drain()
        summit = stack.robots["summit_01"].agent
        stuck = [t for t in summit._threads if "env-m1-" in t]
        stack.coordinator.deps.links = links  # back in coverage: its queued report arrives
        await stack.coordinator.handle_report(
            Report(
                report_id="r-summit-back",
                robot_id="summit_01",
                mission_id="m1",
                kind=ReportKind.TASK_STATUS,
                traffic_class=TrafficClass.CONTROL_MSGS,
                payload={"task_id": "rou-survey_sector-sec_north", "status": "started"},
                stamp=datetime.now(UTC),
            )
        )
        await stack.coordinator.drain()
        after = [t for t in summit._threads if "env-m1-" in t]
    finally:
        await stack.stop()
    assert stuck and not after


def _lap_done(robot_id: str, task_id: str, n: int):
    from datetime import UTC, datetime

    from a6g_common.schemas import Report, ReportKind, TrafficClass

    return Report(
        report_id=f"r-lap-{task_id}-{n}",
        robot_id=robot_id,
        mission_id="m1",
        kind=ReportKind.SKILL_RESULT,
        traffic_class=TrafficClass.CONTROL_MSGS,
        payload={
            "task_id": task_id,
            "result": {"skill": "follow_route", "outcome": "succeeded", "duration_s": 60.0},
        },
        stamp=datetime.now(UTC),
    )


async def test_patrols_go_on_lap_after_lap_until_stopped(tmp_path: Path) -> None:
    """A patrol is not a single pass: the East-corridor robot does not stand still after
    its pass in the 5G shadow."""
    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        stack.world.pause()
        state = await stack.mission("Patrol the campus")
        east = "rou-survey_sector-sec_east"
        assert state["tasks"][east]["repeat"]
        robot = state["tasks"][east]["robot_id"]
        await stack.coordinator.handle_report(_lap_done(robot, east, 1))
        await stack.coordinator.drain()
        state = await stack.coordinator.state("m1")
        lap2 = state["tasks"][f"{east}-lap2"]
        assert state["tasks"][east]["status"] == "done"
        assert lap2["status"] in ("assigned", "in_progress") and lap2["lap"] == 2
        assert state["status"] == "active"
        agent = stack.robots[lap2["robot_id"]].agent
        thread = next(t for t in agent._threads if "env-m1-" in t)
        tasks = (await agent.graph.aget_state(agent._config(thread))).values["tasks"]
        assert f"{east}-lap2" in tasks  # the robot got the new lap

        await stack.coordinator.handle_report(_lap_done(lap2["robot_id"], f"{east}-lap2", 2))
        await stack.coordinator.drain()
        state = await stack.coordinator.state("m1")
        assert state["tasks"][f"{east}-lap3"]["lap"] == 3

        state = await stack.coordinator.chat("m1", "Stop patrolling")
    finally:
        await stack.stop()
    assert not [t for t, e in state["tasks"].items() if e["status"] in ("assigned", "in_progress")]
    assert "Stopped" in state["messages"][-1].content


async def test_send_one_robot_home_and_give_its_patrol_lap_to_another(tmp_path: Path) -> None:
    """ "Send summit to base and Go2_02 to patrol north-west walk": the summit's running lap
    is not "already covered" and left on the summit, and the summit is not planned to go
    home and then patrol."""
    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        stack.world.pause()
        await stack.mission("Patrol the campus")
        north = "rou-survey_sector-sec_north"
        await stack.coordinator.chat("m1", "summit patrol the north-west walk")
        await stack.coordinator.handle_report(_lap_done("summit_01", north, 1))
        await stack.coordinator.drain()
        state = await stack.coordinator.state("m1")
        assert state["tasks"][f"{north}-lap2"]["robot_id"] == "summit_01"
        state = await stack.coordinator.chat(
            "m1", "Send summit to base and Go2_02 to patrol north-west walk"
        )
    finally:
        await stack.stop()
    lap2 = state["tasks"][f"{north}-lap2"]
    assert (lap2["robot_id"], lap2["pinned_to"]) == ("go2_02", "go2_02")
    summit = [
        e["spec"]["kind"]
        for e in state["tasks"].values()
        if e["status"] in ("assigned", "in_progress") and e["robot_id"] == "summit_01"
    ]
    assert summit == ["return_home"]  # nothing else after going home
    assert "As you asked" in "\n".join(str(m.content) for m in state["messages"])


async def test_return_to_base_sends_everyone_home_and_ends_patrols(tmp_path: Path) -> None:
    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        stack.world.pause()
        await stack.mission("Patrol the campus")
        state = await stack.coordinator.chat("m1", "Return to base")
    finally:
        await stack.stop()
    homes = {e["pinned_to"] for e in state["tasks"].values() if e["spec"]["kind"] == "return_home"}
    assert homes == {"go2_01", "go2_02", "summit_01"}
    patrols = [e for e in state["tasks"].values() if e.get("repeat")]
    assert patrols and all(e["status"] == "cancelled" for e in patrols)


def test_survey_is_one_pass_patrol_repeats() -> None:
    from a6g_coordinator.decompose import is_patrol, is_stop

    assert is_patrol("Patrol the campus") and is_patrol("patrolling the east corridor")
    assert not is_patrol("Survey the north-west walk")
    assert is_stop("Stop patrolling") and is_stop("please stop the robots")


async def _robot_view(stack, robot_id: str) -> dict:
    agent = stack.robots[robot_id].agent
    thread = next(t for t in agent._threads if "env-m1-" in t)
    return (await agent.graph.aget_state(agent._config(thread))).values


async def test_escort_interrupts_the_patrol_of_its_robot(tmp_path: Path) -> None:
    """The escort is not queued behind a patrol lap while the robot keeps patrolling."""
    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        stack.world.pause()
        await stack.mission("Patrol the campus")
        state = await stack.coordinator.chat("m1", "Escort the visitor to the bus stop")
        await stack.coordinator.drain()
        escort = state["tasks"]["int-escort-poi_bus_stop"]
        rid = escort["robot_id"]
        await asyncio.sleep(0.5)
        robot = await _robot_view(stack, rid)
    finally:
        await stack.stop()
    assert robot["current_task_id"] == "int-escort-poi_bus_stop"
    patrols = [t for t, e in robot["tasks"].items() if t.startswith("rou-survey")]
    assert patrols and all(
        robot["tasks"][t]["status"] == "pending" for t in patrols
    )  # resumes later


async def test_weather_alert_gives_each_robot_one_post(tmp_path: Path) -> None:
    """The Building 9B door is not queued behind go2_02's indefinite bus-stop post."""
    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        stack.world.pause()
        await stack.mission("Patrol the campus")
        await stack.coordinator.chat("m1", "Escort the visitor to the bus stop")
        state = await stack.coordinator.chat("m1", "Weather alert")
    finally:
        await stack.stop()
    posts = {
        t: e
        for t, e in state["tasks"].items()
        if e["spec"]["kind"] == "hold_post" and e["status"] in ("assigned", "in_progress")
    }
    holders = [e["robot_id"] for e in posts.values()]
    assert len(posts) == 3 and len(set(holders)) == 3  # one post each, all covered


async def test_a_fresh_stack_does_not_resume_old_envelopes(tmp_path: Path) -> None:
    """go2_02 started patrolling on its own: it resumed a days-old envelope from its
    checkpoint (the 2D sim clock never reaches the coordinator's valid_until)."""
    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    first = SimStack(cfg, tmp_path / "s")
    await first.start()
    try:
        first.world.pause()
        await first.mission("Patrol the campus")
        busy = [rid for rid, p in first.robots.items() if p.agent and p.agent._threads]
    finally:
        await first.stop()
    assert busy  # robots were mid-patrol when the stack stopped

    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    second = SimStack(cfg, tmp_path / "s")
    await second.start()
    try:
        resumed = {rid: list(p.agent._threads) for rid, p in second.robots.items() if p.agent}
        saver = second.robots["go2_01"].agent.checkpointer
        left = {c.config["configurable"]["thread_id"] async for c in saver.alist(None)}
    finally:
        await second.stop()
    assert not any(resumed.values()), resumed
    assert not left  # old threads deleted: the SQLite file no longer grows across sessions


async def test_post_waiting_for_a_robot_out_of_contact_is_not_escalated(tmp_path: Path) -> None:
    """Weather alert with go2_02 in a shadow: two robots, three posts. The bus stop waits
    for go2_02 instead of asking the operator (about a task that may already be assigned)."""
    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        stack.world.pause()
        await stack.mission("Patrol the campus")
        links = stack.coordinator.deps.links

        class Shadowed:
            async def send_envelope(self, envelope):
                raise ConnectionError("go2_02 is out of 5G coverage")

            async def get_state(self):
                raise ConnectionError("go2_02 is out of 5G coverage")

        def shadowed(rid, entry):
            return Shadowed() if rid == "go2_02" else links(rid, entry)

        stack.coordinator.deps.links = shadowed
        state = await stack.coordinator.chat("m1", "Weather alert")
    finally:
        await stack.stop()
    posts = [e for e in state["tasks"].values() if e["spec"]["kind"] == "hold_post"]
    waiting = [e for e in posts if e["status"] == "pending"]
    assert len(posts) == 3 and len(waiting) == 1
    assert not state.get("escalations")
    text = "\n".join(str(m.content) for m in state["messages"])
    assert "already holding a post" in text and "go2_02** (Go2) is back in contact" in text


async def test_posts_the_model_gives_robots_still_go_one_per_robot(tmp_path: Path) -> None:
    """Weather alert with go2_02 in a shadow; the model puts two posts on the Summit: they
    do not become a bundle for the Summit while go2_02, back in contact, stands still."""
    from a6g_coordinator.graph import build_graph

    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        stack.world.pause()
        await stack.mission("Patrol the campus")
        links = stack.coordinator.deps.links

        class Shadowed:
            async def send_envelope(self, envelope):
                raise ConnectionError("go2_02 is out of 5G coverage")

            async def get_state(self):
                raise ConnectionError("go2_02 is out of 5G coverage")

        stack.coordinator.deps.links = lambda rid, entry: (
            Shadowed() if rid == "go2_02" else links(rid, entry)
        )
        stack.coordinator.deps.model = fake_model(
            [
                '{"intent": "posture", "posture": "emergency"}',
                '{"tasks": ['
                '{"kind": "hold_post", "target_type": "poi", "target_id": "poi_library_door",'
                ' "robot_id": "summit_01"},'
                ' {"kind": "hold_post", "target_type": "poi", "target_id": "poi_bike_rack",'
                ' "robot_id": "go2_01"},'
                ' {"kind": "hold_post", "target_type": "poi", "target_id": "poi_bus_stop",'
                ' "robot_id": "summit_01"}], "summary": "posts"}',
            ]
        )
        stack.coordinator.graph = build_graph(stack.coordinator.deps).compile(
            checkpointer=stack.coordinator.graph.checkpointer, store=stack.store
        )
        state = await stack.coordinator.chat("m1", "Weather alert")
        posts = {t: e for t, e in state["tasks"].items() if e["spec"]["kind"] == "hold_post"}
        holders = [e["robot_id"] for e in posts.values() if e["robot_id"]]
        assert len(holders) == 2 and len(set(holders)) == 2  # one post per robot
        waiting = [t for t, e in posts.items() if e["status"] == "pending"]
        assert len(waiting) == 1

        stack.coordinator.deps.links = links  # go2_02 is back in coverage
        state = await stack.coordinator.submit(
            "m1", {"type": "robot_recovered", "robot_id": "go2_02"}
        )
    finally:
        await stack.stop()
    post = state["tasks"][waiting[0]]
    assert post["robot_id"] == "go2_02" and post["status"] in ("assigned", "in_progress")


def test_resolved_tasks_are_never_escalated() -> None:
    from a6g_coordinator.graph import _live_escalations

    tasks = {
        "t_assigned": {"status": "assigned", "escalated": True},
        "t_pending": {"status": "pending", "escalated": True},
    }
    escs = [
        {"esc_id": "esc-1", "kind": "unallocatable", "task_id": "t_assigned"},
        {"esc_id": "esc-2", "kind": "unallocatable", "task_id": "t_pending"},
        {"esc_id": "esc-3", "kind": "observation", "observation": {}},
    ]
    assert [e["esc_id"] for e in _live_escalations(escs, tasks)] == ["esc-2", "esc-3"]
    assert tasks["t_assigned"]["escalated"] is False  # may be escalated again later


async def test_orders_during_an_emergency_need_approval(tmp_path: Path) -> None:
    """After "Weather alert", a new order ends the emergency only once approved; return to
    base means the staging area for every robot (not "stop", not back to patrolling)."""
    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        stack.world.pause()
        await stack.mission("Patrol the campus")
        await stack.coordinator.chat("m1", "Weather alert")
        state = await stack.coordinator.chat("m1", "Return to base then")
        assert state["posture"] == "emergency"
        assert [
            i["action_requests"][0]["name"] for i in await stack.coordinator.interrupts("m1")
        ] == ["end_emergency"]
        state = await stack.coordinator.resume("m1", [{"type": "approve"}])
    finally:
        await stack.stop()
    assert state["posture"] == "routine"
    homes = {
        e["pinned_to"]
        for e in state["tasks"].values()
        if e["spec"]["kind"] == "return_home" and e["status"] in ("assigned", "in_progress")
    }
    assert homes == {"go2_01", "go2_02", "summit_01"}
    for e in state["tasks"].values():
        if e["spec"]["kind"] in ("hold_post", "survey_sector"):
            assert e["status"] in ("cancelled", "done"), e  # posts released, patrols ended


async def test_return_to_base_for_one_named_robot_is_not_for_everyone() -> None:
    """ "Send summit to base and Go2_02 to patrol north-west walk": a return_home the model
    gives no robot does not mean "everyone", which would send the whole fleet home and leave
    the patrol without a robot."""
    from a6g_common.schemas import Posture
    from a6g_coordinator.decompose import decompose_goal

    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    robots = {"go2_01": "go2", "go2_02": "go2", "summit_01": "summit-xl"}
    plan = (
        '{"tasks": [{"kind": "return_home", "target_type": "staging"},'
        ' {"kind": "survey_sector", "target_type": "sector", "target_id": "sec_north"}],'
        ' "summary": "x"}'
    )
    tasks, pinned, *_ = await decompose_goal(
        "Send summit to base and Go2_02 to patrol north-west walk",
        Posture.ROUTINE,
        cfg.campus,
        fake_model([plan]),
        (),
        robots,
    )
    assert sorted((t.kind.value, pinned.get(t.task_id)) for t in tasks) == [
        ("return_home", "summit_01"),
        ("survey_sector", "go2_02"),
    ]
    # Without names, "return to base" is still for every robot.
    tasks, pinned, *_ = await decompose_goal(
        "Return to base", Posture.ROUTINE, cfg.campus, fake_model([plan]), (), robots
    )
    assert sorted(pinned[t.task_id] for t in tasks if t.kind.value == "return_home") == sorted(
        robots
    )


async def test_an_order_that_ends_an_emergency_does_not_resume_paused_patrols(
    tmp_path: Path,
) -> None:
    """Approving "Send summit to base and Go2_02 to patrol north-west walk" during an
    emergency does not resume every paused patrol (go2_02 would get the South plaza as well
    and go2_01 would leave its post for the East corridor)."""
    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        stack.world.pause()
        await stack.mission("Patrol the campus")
        await stack.coordinator.chat("m1", "Weather alert")
        await stack.coordinator.chat(
            "m1", "Send summit to base and Go2_02 to patrol north-west walk"
        )
        state = await stack.coordinator.resume("m1", [{"type": "approve"}])
    finally:
        await stack.stop()
    assert state["posture"] == "routine"
    work = sorted(
        (e["robot_id"], e["spec"]["kind"], e["spec"]["target"].get("sector_id"))
        for e in state["tasks"].values()
        if e["status"] in ("pending", "assigned", "in_progress")
    )
    assert work == [
        ("go2_02", "survey_sector", "sec_north"),
        ("summit_01", "return_home", None),
    ]  # go2_01 stays where it is; the other patrols stay stopped
    assert "stays stopped" in "\n".join(str(m.content) for m in state["messages"])


async def test_all_clear_with_an_order_runs_the_order(tmp_path: Path) -> None:
    """ "All clear, return to base" resumed the patrols and ignored "return to base"."""
    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        stack.world.pause()
        await stack.mission("Patrol the campus")
        await stack.coordinator.chat("m1", "Weather alert")
        state = await stack.coordinator.chat("m1", "All clear, return to base")
        asks = await stack.coordinator.interrupts("m1")
    finally:
        await stack.stop()
    assert not asks and state["posture"] == "routine"
    open_kinds = {
        e["spec"]["kind"]
        for e in state["tasks"].values()
        if e["status"] in ("pending", "assigned", "in_progress")
    }
    assert open_kinds == {"return_home"}


async def test_return_to_base_is_an_order_not_a_stop() -> None:
    from a6g_coordinator.decompose import interpret_chat, order_after_all_clear

    model = fake_model(['{"intent": "stop"}'])  # what the LLM answered in the transcript
    intent, _ = await interpret_chat("Return to base then", True, model)
    assert intent.intent == "goal"
    assert order_after_all_clear("All clear, return to base") == "return to base"
    assert order_after_all_clear("The emergency is over") is None
    assert order_after_all_clear("All clear, then patrol the campus") == "patrol the campus"


TWO_GO2 = "Send the summit to patrol the east corridor and the 2 Go2 to inspect the Bike rack"


async def test_the_same_task_for_several_named_robots_gets_one_task_each() -> None:
    """Robots named for the same bike-rack inspection get one task each, with distinct ids."""
    from a6g_common.schemas import Posture
    from a6g_coordinator.decompose import decompose_goal

    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    robots = {"go2_01": "go2", "go2_02": "go2", "summit_01": "summit"}
    model = fake_model(
        [
            '{"tasks": [{"kind": "survey_sector", "target_type": "sector", "target_id": "sec_east",'
            ' "robot_id": "summit_01"},'
            ' {"kind": "inspect_poi", "target_type": "poi", "target_id": "poi_bike_rack",'
            ' "robot_id": "go2_01"},'
            ' {"kind": "inspect_poi", "target_type": "poi", "target_id": "poi_bike_rack",'
            ' "robot_id": "go2_02"}], "summary": "as asked"}'
        ]
    )
    want = {
        "rou-survey_sector-sec_east": "summit_01",
        "rou-inspect_poi-poi_bike_rack": "go2_01",
        "rou-inspect_poi-poi_bike_rack-go2_02": "go2_02",
    }
    tasks, pinned, *_ = await decompose_goal(
        TWO_GO2, Posture.ROUTINE, cfg.campus, model, (), robots
    )
    assert pinned == want and len(tasks) == 3
    tasks, pinned, used_template, *_ = await decompose_goal(
        TWO_GO2, Posture.ROUTINE, cfg.campus, None, (), robots
    )
    assert used_template and pinned == want  # the template understands "the 2 Go2" too


async def test_why_questions_are_answered_from_the_decision_log(tmp_path: Path) -> None:
    from a6g_coordinator.decompose import interpret_chat

    intent, _ = await interpret_chat(
        "Why the Go2_01 did not inspect the bike rack?", True, fake_model(['{"intent": "status"}'])
    )
    assert intent.intent == "explain"

    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        stack.world.pause()
        await stack.mission("Patrol the campus")
        stack.coordinator.deps.model = fake_model(
            ['{"intent": "explain"}', '{"answer": "go2_01 was given the South plaza patrol."}']
        )
        stack.coordinator.graph = (
            __import__("a6g_coordinator.graph", fromlist=["x"])
            .build_graph(stack.coordinator.deps)
            .compile(checkpointer=stack.coordinator.graph.checkpointer, store=stack.store)
        )
        state = await stack.coordinator.chat("m1", "Why is go2_01 not in the north?")
    finally:
        await stack.stop()
    calls = [c["name"] for m in state["messages"] for c in getattr(m, "tool_calls", None) or []]
    assert "explain_decisions" in calls
    assert state["messages"][-1].content == "go2_01 was given the South plaza patrol."


async def test_all_robots_to_a_poi_get_one_spot_each(tmp_path: Path) -> None:
    """ "Send all robots to the bus stop" and "... to inspect the bus stop" send every robot,
    each to its own spot."""
    from a6g_common.schemas import Posture
    from a6g_coordinator.decompose import decompose_goal, interpret_chat

    intent, _ = await interpret_chat(
        "Send all robots to the bus stop", True, fake_model(['{"intent": "unsupported"}'])
    )
    assert intent.intent == "goal"

    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    robots = {"go2_01": "go2", "go2_02": "go2", "summit_01": "summit"}
    for text, kind in (
        ("Send all robots to the bus stop", "go_to"),
        ("Send all robots to inspect the bus stop", "inspect_poi"),
    ):
        # The model plans a single, unassigned task: the per-robot plan wins.
        one = f'{{"kind": "{kind}", "target_type": "poi", "target_id": "poi_bus_stop"}}'
        model = fake_model([f'{{"tasks": [{one}]}}'])
        tasks, pinned, *_ = await decompose_goal(
            text, Posture.ROUTINE, cfg.campus, model, (), robots
        )
        assert sorted(pinned.values()) == sorted(robots), text
        assert {t.kind.value for t in tasks} == {kind}
        assert sorted(t.target.slot for t in tasks) == [0, 1, 2]  # distinct spots
        spots = {
            (round(p.x, 2), round(p.y, 2))
            for t in tasks
            for p in [cfg.campus.poi_slot("poi_bus_stop", t.target.slot)]
        }
        assert len(spots) == 3

    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        stack.world.pause()
        state = await stack.mission("Send all robots to inspect the bus stop")
        owners = {
            e["robot_id"]
            for e in state["tasks"].values()
            if e["status"] in ("assigned", "in_progress")
        }
    finally:
        await stack.stop()
    assert owners == set(robots)


def test_poi_slots_stay_out_of_buildings(cfg) -> None:
    from a6g_common.geometry import point_in_polygon

    for poi in cfg.campus.pois:
        spots = [cfg.campus.poi_slot(poi.poi_id, k) for k in range(4)]
        assert len({(s.x, s.y) for s in spots}) == 4
        for s in spots:
            assert not any(point_in_polygon((s.x, s.y), o) for o in cfg.campus.obstacles)


def test_slotted_inspection_passes_its_own_viewpoint(cfg) -> None:
    from a6g_common.schemas import PoiTarget, SkillName, TaskKind, TaskSpec
    from a6g_robot_agent.graph import task_to_skill

    spec = TaskSpec(
        task_id="t1",
        kind=TaskKind.INSPECT_POI,
        target=PoiTarget(poi_id="poi_bus_stop", slot=2),
        est_duration_s=10,
    )
    tool, args = task_to_skill(spec, cfg.campus, cfg.fleet.defaults.agent, None)
    assert (
        tool is SkillName.INSPECT
        and args["viewpoint"]["x"] == cfg.campus.poi_slot("poi_bus_stop", 2).x
    )
    go = spec.model_copy(update={"kind": TaskKind.GO_TO})
    tool, args = task_to_skill(go, cfg.campus, cfg.fleet.defaults.agent, None)
    assert tool is SkillName.NAVIGATE_TO and "pose" in args


TWO_AND_OTHER = "Send two robots to inspect the bus stop and other to patrol the campus"
TWO_AND_THE_OTHER = (
    "Send two robots to inspect the building 9B door and the other to patrol the campus"
)
_SURVEY = {"kind": "survey_sector", "target_type": "sector", "robot_id": "go2_01"}
_INSPECT = {"kind": "inspect_poi", "target_type": "poi", "target_id": "poi_bus_stop"}
PLAN_TWO_AND_OTHER = json.dumps(
    {
        "tasks": [_INSPECT, _INSPECT]
        + [{**_SURVEY, "target_id": s} for s in ("sec_north", "sec_south", "sec_east")],
        "summary": "two inspect, one patrols",
    }
)


async def test_a_robot_the_model_picks_on_its_own_becomes_a_bundle() -> None:
    """ "... and other to patrol the campus": one robot does the three patrols. The model
    picked go2_01 on its own: that is a bundle for the allocator, not a pin."""
    from a6g_common.schemas import Posture
    from a6g_coordinator.decompose import decompose_goal

    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    robots = {"go2_01": "go2", "go2_02": "go2", "summit_01": "summit"}
    tasks, pinned, *_ = await decompose_goal(
        TWO_AND_OTHER, Posture.ROUTINE, cfg.campus, fake_model([PLAN_TWO_AND_OTHER]), (), robots
    )
    surveys = [t.task_id for t in tasks if t.kind.value == "survey_sector"]
    assert len(surveys) == 3 and len({pinned[t] for t in surveys}) == 1  # ONE robot patrols
    assert pinned[surveys[0]].startswith("bundle:")  # chosen by the allocator, not pinned
    inspections = [t for t in tasks if t.kind.value == "inspect_poi"]
    assert len({t.task_id for t in inspections}) == 2
    assert len({pinned[t.task_id] for t in inspections}) == 2  # two different robots
    assert sorted(t.target.slot for t in inspections) == [0, 1]


async def test_two_inspect_and_stay_while_the_other_patrols(tmp_path: Path) -> None:
    """Two robots inspect the bus stop and stay; the third patrols the three sectors. Idle
    robots patrol only when told to ("... the idle robots to patrol the campus too")."""
    from a6g_coordinator.graph import build_graph

    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        stack.world.pause()
        # A counted order takes the careful path: the model plans it in the
        # full format (two inspections, the three sectors as one robot's group) and the
        # assignment is checked before the robots move.
        careful = json.dumps(
            {
                "tasks": [
                    {**_INSPECT, "target_id": "poi_library_door", "robots": 2},
                    *[
                        {
                            "kind": "survey_sector",
                            "target_type": "sector",
                            "target_id": s,
                            "group": "patrol",
                            "continuous": True,
                        }
                        for s in ("sec_north", "sec_south", "sec_east")
                    ],
                ]
            }
        )
        stack.coordinator.deps.model = fake_model(['{"intent": "goal"}', careful, '{"ok": true}'])
        stack.coordinator.graph = build_graph(stack.coordinator.deps).compile(
            checkpointer=stack.coordinator.graph.checkpointer, store=stack.store
        )
        await stack.coordinator.chat("m1", TWO_AND_THE_OTHER)
        await stack.coordinator.drain()
        state = await stack.coordinator.state("m1")

        def owners(state, kind):
            return {
                e["robot_id"]
                for e in state["tasks"].values()
                if e["spec"]["kind"] == kind and e["status"] in ("assigned", "in_progress")
            }

        inspectors, patroller = owners(state, "inspect_poi"), owners(state, "survey_sector")
        assert len(inspectors) == 2 and len(patroller) == 1 and not inspectors & patroller
        sectors = {
            e["spec"]["target"]["sector_id"]
            for e in state["tasks"].values()
            if e["spec"]["kind"] == "survey_sector"
        }
        assert sectors == {"sec_north", "sec_south", "sec_east"}  # the whole campus

        for t, e in list(state["tasks"].items()):  # the inspections finish
            if e["spec"]["kind"] == "inspect_poi":
                await stack.coordinator.handle_report(_done(e["robot_id"], t))
        await stack.coordinator.drain()
        state = await stack.coordinator.state("m1")
        assert owners(state, "survey_sector") == patroller  # the inspectors stay put

        # The patroller enters the 5G shadow: its three patrols wait for it (they were handed
        # to the idle summit, which then patrolled while go2_02 only kept the East corridor).
        (rid,) = patroller
        state = await stack.coordinator.submit(
            "m1", {"type": "robot_lost", "robot_id": rid, "reason": "5G shadow"}
        )
        mine = {
            e["spec"]["target"]["sector_id"]
            for e in state["tasks"].values()
            if e["spec"]["kind"] == "survey_sector"
            and e.get("robot_id") == rid
            and e["status"] in ("assigned", "in_progress")
        }
        assert mine == {"sec_north", "sec_south", "sec_east"}
        assert owners(state, "survey_sector") == patroller
        state = await stack.coordinator.submit("m1", {"type": "robot_recovered", "robot_id": rid})

        stack.coordinator.deps.model = None  # templates: "the idle robots" are resolved by code
        links = stack.coordinator.deps.links

        class Shadowed:  # the patroller is in the 5G shadow when the order comes
            async def send_envelope(self, envelope):
                raise ConnectionError(f"{rid} is out of 5G coverage")

            async def get_state(self):
                raise ConnectionError(f"{rid} is out of 5G coverage")

        def shadowed(robot, entry):
            return Shadowed() if robot == rid else links(robot, entry)

        stack.coordinator.deps.links = shadowed
        stack.coordinator.graph = build_graph(stack.coordinator.deps).compile(
            checkpointer=stack.coordinator.graph.checkpointer, store=stack.store
        )
        state = await stack.coordinator.chat(
            "m1", "Now send the idle robots to patrol the campus too"
        )
    finally:
        await stack.stop()
    assert owners(state, "survey_sector") == patroller | inspectors  # everyone patrols now
    text = "\n".join(str(m.content) for m in state["messages"])
    assert "already under way" not in text and "declined" not in text
    for robot in patroller | inspectors:  # three patrols each: nobody got the patroller's
        mine = [
            e
            for e in state["tasks"].values()
            if e["spec"]["kind"] == "survey_sector"
            and e.get("robot_id") == robot
            and e["status"] in ("assigned", "in_progress")
        ]
        assert len(mine) == 3, (robot, len(mine))


def _done(robot_id: str, task_id: str):
    from datetime import UTC, datetime

    from a6g_common.schemas import Report, ReportKind, TrafficClass

    return Report(
        report_id=f"r-done-{task_id}",
        robot_id=robot_id,
        mission_id="m1",
        kind=ReportKind.SKILL_RESULT,
        traffic_class=TrafficClass.CONTROL_MSGS,
        payload={
            "task_id": task_id,
            "result": {"skill": "inspect", "outcome": "succeeded", "duration_s": 10.0},
        },
        stamp=datetime.now(UTC),
    )


async def test_low_battery_robot_recharges_and_hands_over_important_work(tmp_path: Path) -> None:
    """A robot does not drain to 0 % and fail. Low battery means: release the tasks, go
    to the staging area and charge; the escort goes to another robot, the patrol waits."""
    from conftest import wait_for

    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    assert cfg.manifests["summit_01"].battery_reliable  # the simulator models its battery
    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        stack.world.pause()
        await stack.mission("Patrol the campus")
        state = await stack.coordinator.chat("m1", "Escort the visitor to the bus stop")
        rid = state["tasks"]["int-escort-poi_bus_stop"]["robot_id"]
        patrol = next(
            t
            for t, e in state["tasks"].items()
            if e.get("robot_id") == rid and e["spec"]["kind"] == "survey_sector"
        )
        stack.world.charge_pct_per_s = 20.0  # quick charging for the test
        stack.world.robot(rid).battery_pct = 23.0  # below floor + margin (25 %)
        stack.world.resume()

        async def fleet_status(want: str) -> bool:
            return (await stack.coordinator.state("m1"))["fleet"][rid]["status"] == want

        await wait_for(lambda: fleet_status("charging"), 30, "robot recharging")
        await stack.coordinator.drain()
        state = await stack.coordinator.state("m1")
        escort = state["tasks"]["int-escort-poi_bus_stop"]
        assert escort["robot_id"] not in (None, rid)  # important: another robot took it
        assert state["tasks"][patrol]["status"] == "paused"  # routine: waits for it
        text = "\n".join(str(m.content) for m in state["messages"])
        assert "low on battery" in text and "unknown" not in text

        await wait_for(lambda: fleet_status("available"), 60, "robot charged")
        await stack.coordinator.drain()
        state = await stack.coordinator.state("m1")
    finally:
        await stack.stop()
    assert state["tasks"][patrol]["robot_id"] == rid  # back at its patrol
    assert stack.world.robot(rid).battery_pct >= cfg.allocator.recharge_resume_pct


async def test_idle_robot_too_low_for_a_lap_goes_to_charge(tmp_path: Path) -> None:
    """Idle robots at 29 % and 27 % away from the staging area are too low to be given a
    patrol lap (it must end above 25 %), and an idle robot hardly drains, so the 25 %
    trigger does not fire. An idle robot below recharge_idle_pct (40 %) charges,
    after waiting recharge_idle_grace_s for an order (the next lap is not refused)."""
    from conftest import wait_for

    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        stack.world.pause()
        await stack.mission("Patrol the campus")
        await stack.coordinator.chat("m1", "Stop patrolling")  # every robot idle
        low, ok = "go2_01", "summit_01"
        for rid, (x, y) in ((low, (-50.0, 5.0)), (ok, (-55.0, -30.0))):  # out on the campus
            stack.world.robot(rid).x, stack.world.robot(rid).y = x, y
        stack.world.charge_pct_per_s = 20.0  # quick charging for the test
        stack.world.robot(low).battery_pct = 30.0  # idle, above 25 %, below 40 %
        stack.world.robot(ok).battery_pct = 60.0  # idle and fine: stays where it is
        stack.world.resume()
        agent = stack.robots[low].agent
        assert (await agent.activity())["recharge"] is None  # waits for orders first
        phases: list = []

        async def charged() -> bool:
            phase = (await agent.activity())["recharge"]
            if phase and phase not in phases:
                phases.append(phase)
            return stack.world.robot(low).battery_pct >= cfg.allocator.recharge_resume_pct

        await wait_for(charged, 60, "idle low robot charged")
        assert phases == ["going", "charging"]  # shown as such, never as idle
        # 60 %: no recharge. Idle, it drives back to the staging area after 30 s
        # and does not top up there (the dock charges below 40 % only).
        assert not stack.robots[ok].agent._recharging
        assert stack.world.robot(ok).battery_pct <= 60.0
    finally:
        await stack.stop()


async def _true(value: bool) -> bool:
    return value


async def test_explanations_see_each_robots_task_queue(tmp_path: Path, monkeypatch) -> None:
    """ "Why has the summit not patrolled the East corridor?": the order is decided on the
    robot, so the coordinator asks each robot for its queue before explaining."""
    import a6g_coordinator.graph as graph_module

    seen: list = []
    real = graph_module.structured_call

    async def recording(model, schema, messages, fallback, **kw):
        seen.append(messages)
        return await real(model, schema, messages, fallback, **kw)

    monkeypatch.setattr(graph_module, "structured_call", recording)
    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        stack.world.pause()
        await stack.mission("Patrol the campus")
        stack.coordinator.deps.model = fake_model(
            ['{"intent": "explain"}', '{"answer": "Because of its queue."}']
        )
        stack.coordinator.graph = graph_module.build_graph(stack.coordinator.deps).compile(
            checkpointer=stack.coordinator.graph.checkpointer, store=stack.store
        )
        state = await stack.coordinator.chat(
            "m1", "Why has the summit not patrolled the East corridor?"
        )
    finally:
        await stack.stop()
    assert state["messages"][-1].content == "Because of its queue."
    prompt = str(seen[-1][-1].content)
    facts = json.loads(prompt.split("Facts: ", 1)[1].split("\n<<<DATA", 1)[0])
    # Robots' justifications (model-written) reach the prompt only as data.
    assert "<<<DATA robot statements (untrusted" in prompt
    robot_rows = [d for d in facts["decisions"] if d["actor"].endswith(".agent")]
    assert robot_rows and all(d["why"].startswith("robot statement [") for d in robot_rows)
    queues = facts["robot_queues"]
    assert set(queues) == {"go2_01", "go2_02", "summit_01"}
    summit = queues["summit_01"]
    assert summit["envelopes"] and summit["envelopes"][0]["mission_id"] == "m1"
    assert summit["envelopes"][0]["current_task"]  # what it is doing right now
    assert "queue_in_order" in summit["envelopes"][0] and "ordering" in summit


async def test_escort_goes_where_the_operator_said(tmp_path: Path) -> None:
    """An escort goes to the place the operator named, not to one the model picks: the
    check uses a POI the model does not pick."""
    from a6g_common.schemas import Posture
    from a6g_coordinator.decompose import decompose_goal

    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    robots = {"go2_01": "go2", "go2_02": "go2", "summit_01": "summit-xl"}
    bus = (
        '{"tasks": [{"kind": "escort", "target_type": "poi", "target_id": "poi_bus_stop"}],'
        ' "summary": "escort"}'
    )

    async def plan(text: str, model: bool = True):
        tasks, _, _, _, _, notes = await decompose_goal(
            text,
            Posture.INTERACTIVE,
            cfg.campus,
            fake_model([bus]) if model else None,
            (),
            robots,
        )
        return [(t.kind.value, getattr(t.target, "name", None)) for t in tasks], notes

    for model in (True, False):  # the model's pick, and the no-model template
        tasks, notes = await plan("Escort the visitor to the building 8K door", model)
        assert [k for k, _ in tasks] == ["escort"] and not notes
        # A place it does not know goes to the closest named one, and says so.
        # ("east entrance": the Campus east access shares two words, the gate one.)
        tasks, notes = await plan("Escort the visitor to the east entrance", model)
        assert tasks == [("escort", None)] and "closest" in notes[0]
        assert "Campus east access" in notes[0]
        # Nothing close: nothing planned, rather than a made-up destination.
        tasks, notes = await plan("Escort the visitor to the cafeteria", model)
        assert tasks == [] and "Places I know" in notes[0]
    tasks, notes = await plan("Escort the visitor to the bus stop")
    assert tasks == [("escort", None)] and not notes
    two = (
        '{"tasks": [{"kind": "inspect_poi", "target_type": "poi", "target_id": "poi_bus_stop"},'
        ' {"kind": "go_to", "target_type": "poi", "target_id": "poi_bike_rack"}], "summary": "x"}'
    )
    _, _, _, _, _, notes = await decompose_goal(
        "Send a robot to inspect the bus stop and go2_01 to the bike rack",
        Posture.ROUTINE,
        cfg.campus,
        fake_model([two]),
        (),
        robots,
    )
    assert not notes  # "to inspect ..." is an order, not an unknown place

    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        stack.world.pause()
        await stack.mission("Patrol the campus")
        state = await stack.coordinator.chat("m1", "Escort the visitor to the building 8K door")
    finally:
        await stack.stop()
    escort = state["tasks"]["int-escort-poi_kiosk"]
    assert escort["status"] in ("assigned", "in_progress")
    assert "escort the visitor to the building 8k door" in state["messages"][-1].content.lower()


async def test_swarm_updates_are_told_apart_from_answers(tmp_path: Path) -> None:
    """The chat UI shows what the swarm reports on its own (a robot failing) as a labelled
    "Swarm update" as it comes; answers to the operator's messages carry no such label."""
    from conftest import wait_for

    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    stack = SimStack(cfg, tmp_path / "s")
    await stack.start()
    try:
        answer = await stack.coordinator.chat("m1", "Patrol the campus")
        await stack.fail_robot("go2_02")

        async def told() -> bool:
            state = await stack.coordinator.state("m1")
            return any(
                m.response_metadata.get("a6g_update")
                for m in state["messages"]
                if isinstance(m, AIMessage) and "go2_02" in m.text
            )

        await wait_for(told, 60, "the failure narrated as a swarm update")
    finally:
        await stack.stop()
    replies = [m for m in answer["messages"] if isinstance(m, AIMessage) and m.text]
    assert replies and not any(m.response_metadata.get("a6g_update") for m in replies)
