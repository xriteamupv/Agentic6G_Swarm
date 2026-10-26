"""P3 acceptance: scenario tests with 3 simulated robots (2 Go2-like, 1 Summit-like).

1. Routine survey goal -> all sectors allocated -> mission completes.
2. robot_fail mid-mission -> pending tasks reallocated within 5 s simulated time.
3. coordinator_kill, then restart -> mission resumes from the checkpoint, no duplicated tasks.
4. Coordinator down AND a robot fails -> survivors redistribute its swappable tasks via
   contract-net; on coordinator return, state reconciles.
5. weather_alert during an open escort -> escort concluded (not dropped), survey preempted,
   robots sent to exposed POIs; a robot added with add_robot is admitted and allocated.
6. anomaly -> observation escalated -> interrupt() -> resumed by the CLI.
7. Every step above appears in ``a6g decisions``.
"""

from collections import Counter
from pathlib import Path

import pytest
from a6g_common.config import load_config
from a6g_common.schemas import DecisionKind
from a6g_devstack import SimStack
from conftest import wait_for

pytestmark = pytest.mark.scenario

M = "m1"
SURVEYS = {
    "rou-survey_sector-sec_north",
    "rou-survey_sector-sec_east",
    "rou-survey_sector-sec_south",
}


@pytest.fixture
async def stack(tmp_path: Path):
    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    s = SimStack(cfg, tmp_path / "stack")
    await s.start()
    try:
        yield s
    finally:
        await s.stop()


async def coord_state(stack: SimStack) -> dict:
    return await stack.coordinator.state(M)


def statuses(state: dict) -> dict[str, tuple[str, str | None]]:
    return {tid: (t["status"], t.get("robot_id")) for tid, t in state["tasks"].items()}


async def completed(stack: SimStack) -> bool:
    return stack.coordinator is not None and (await coord_state(stack)).get("status") == "completed"


def decisions(stack: SimStack, kind: DecisionKind | None = None):
    return [d for d in stack.log.read(M) if kind is None or d.kind is kind]


def skill_runs(stack: SimStack, skill: str) -> Counter:
    """How many times each robot started a given skill (from the skill servers' decisions)."""
    runs = Counter()
    for d in stack.log.read():
        if d.kind is DecisionKind.SKILL and d.inputs_ref == f"skill:{skill}":
            runs[d.actor.split(".")[0]] += 1
    return runs


# --------------------------------------------------------------------------- 1


async def test_1_routine_survey_allocates_all_sectors_and_completes(stack: SimStack) -> None:
    state = await stack.mission("Survey the campus")
    assert set(state["tasks"]) == SURVEYS
    owners = {t["robot_id"] for t in state["tasks"].values()}
    assert owners == {"go2_01", "go2_02", "summit_01"}  # one sector each
    assert "Assignments:" in state["messages"][-1].content

    await wait_for(lambda: completed(stack), timeout=300)  # laps along the footpaths
    state = await coord_state(stack)
    assert {s for s, _ in statuses(state).values()} == {"done"}
    assert "Mission complete" in state["messages"][-1].content
    kinds = {d.kind for d in decisions(stack)}
    assert {DecisionKind.DECOMPOSE, DecisionKind.ALLOCATE, DecisionKind.ACCEPT} <= kinds


# --------------------------------------------------------------------------- 2


async def test_2_robot_fail_reallocates_within_5s_sim(stack: SimStack) -> None:
    moving = stack.world.pause_when(lambda w: w.robot("go2_01").odometer_m > 10)
    state = await stack.mission("Survey the campus")
    victim_task = next(t for t, e in state["tasks"].items() if e["robot_id"] == "go2_01")
    await moving.wait()
    t_fail = stack.world.clock.now()
    await stack.fail_robot("go2_01")
    stack.world.resume()

    def realloc_decisions():
        return [
            d
            for d in decisions(stack, DecisionKind.REALLOCATE)
            if d.stamp >= t_fail
            and any(a["task_id"] == victim_task for a in d.output["assignments"])
        ]

    await wait_for(realloc_decisions, timeout=30)  # written by the audit node
    realloc = realloc_decisions()[0]
    latency = (realloc.stamp - t_fail).total_seconds()
    assert latency <= 5.0, f"reallocation took {latency:.1f} s simulated"
    assert any(a["task_id"] == victim_task for a in realloc.output["assignments"])
    assert (await coord_state(stack))["fleet"]["go2_01"]["status"] == "failed"

    await wait_for(lambda: completed(stack), timeout=400)
    assert (await coord_state(stack))["tasks"][victim_task]["status"] == "done"


# --------------------------------------------------------------------------- 3


async def test_3_coordinator_kill_and_restart_resumes_without_duplicates(stack: SimStack) -> None:
    await _scenario_3(stack)


async def test_3_on_the_postgres_checkpointer_path(tmp_path: Path, pg_dsn: str) -> None:
    """SPEC A1 12.3: the plain (no Agent Server) path with PostgresSaver + PostgresStore."""
    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    s = SimStack(cfg, tmp_path / "stack", coordinator_persistence="postgres", pg_dsn=pg_dsn)
    await s.start()
    try:
        assert type(s.coordinator.graph.checkpointer).__name__ == "AsyncPostgresSaver"
        await _scenario_3(s)
    finally:
        await s.stop()


async def _scenario_3(stack: SimStack) -> None:
    moving = stack.world.pause_when(lambda w: w.robot("go2_02").odometer_m > 20)
    before = await stack.mission("Survey the campus")
    await moving.wait()
    await stack.kill_coordinator()
    stack.world.resume()

    # Robots keep working on their envelopes while the coordinator is down.
    down_until = stack.world.clock.now()
    await wait_for(lambda: (stack.world.clock.now() - down_until).total_seconds() > 60, timeout=30)
    assert not any(a.coordinator_reachable for a in (p.agent for p in stack.robots.values()) if a)

    await stack.start_coordinator()  # same checkpointer file + store: resumes the thread
    await wait_for(lambda: completed(stack), timeout=400)
    after = await coord_state(stack)
    assert set(after["tasks"]) == set(before["tasks"]) == SURVEYS  # nothing re-decomposed
    assert len(decisions(stack, DecisionKind.DECOMPOSE)) == 1
    # No task ran twice: one follow_route per leg of each robot's survey (a survey longer
    # than 20 waypoints is walked in legs).
    from a6g_allocator.cost import task_path
    from a6g_common.schemas import TaskSpec
    from a6g_robot_agent.graph import route_legs

    legs = Counter()
    for t in after["tasks"].values():
        spec = TaskSpec.model_validate(t["spec"])
        legs[t["robot_id"]] += len(route_legs(task_path(spec, stack.cfg.campus))[0])
    assert skill_runs(stack, "follow_route") == legs


# --------------------------------------------------------------------------- 4


async def test_4_coordinator_down_and_robot_fails_contract_net_then_reconcile(
    stack: SimStack,
) -> None:
    moving = stack.world.pause_when(lambda w: w.robot("go2_01").odometer_m > 10)
    state = await stack.mission("Survey the campus")
    victim_task = next(t for t, e in state["tasks"].items() if e["robot_id"] == "go2_01")
    await moving.wait()
    await stack.kill_coordinator()
    await stack.fail_robot("go2_01")
    stack.world.resume()

    # Survivors redistribute go2_01's swappable task peer-to-peer.
    awards = []

    async def awarded() -> bool:
        awards[:] = [
            d
            for d in decisions(stack, DecisionKind.AWARD)
            if d.output.get("from_robot") == "go2_01"
        ]
        return bool(awards)

    await wait_for(awarded, timeout=30)
    award = awards[0].output["award"]
    winner = award["awarded_to"]
    assert award["task_id"] == victim_task and winner in ("go2_02", "summit_01")
    assert award["awarded_by"] == "go2_02"  # lowest alive robot_id initiates
    assert awards[0].output["bids"]  # bids collected from the survivors

    # Coordinator returns: robots reconcile, the coordinator adopts the award.
    await stack.start_coordinator()

    async def reconciled() -> bool:
        return (await coord_state(stack))["tasks"][victim_task]["robot_id"] == winner

    await wait_for(reconciled, timeout=30)
    await wait_for(lambda: completed(stack), timeout=150)
    final = await coord_state(stack)
    assert final["tasks"][victim_task]["status"] == "done"
    # The victim's sector was surveyed to completion exactly once, by a survivor (the
    # winner, or whoever the coordinator re-planned it to if the winner later went silent).
    south = stack.cfg.campus.sector(victim_task.rsplit("-", 1)[-1])
    from a6g_robot_agent.graph import route_headings, route_legs

    leg0 = [(w.x, w.y) for w in route_legs(route_headings(list(south.survey_waypoints)))[0][0]]
    south_runs = [  # the survey's first leg (zones may share their starting junction)
        d
        for d in stack.log.read()
        if d.kind is DecisionKind.SKILL
        and d.inputs_ref == "skill:follow_route"
        and d.output["result"]["outcome"] == "succeeded"
        and [(w["x"], w["y"]) for w in d.output["args"]["waypoints"]] == leg0
    ]
    assert len(south_runs) == 1 and not south_runs[0].actor.startswith("go2_01")
    assert any(d.output.get("moves") for d in decisions(stack, DecisionKind.FALLBACK))
    # The contract-net round travelled as P3 messages, measured (SPEC A2.5, stage C0).
    from a6g_observability.comms import load_records, mission_records

    p3 = [
        r
        for r in mission_records(load_records([stack.workdir / "comms.jsonl"]), M)
        if r["pattern"] == "P3"
    ]
    assert {"cfp", "bid", "award"} <= {r["schema"] for r in p3}
    assert all(r["wire_bytes"] > 0 for r in p3)


# --------------------------------------------------------------------------- 5


async def test_5_weather_alert_preempts_concludes_escort_and_admits_new_robot(
    stack: SimStack,
) -> None:
    await stack.mission("Survey the campus")
    await stack.coordinator.goal(M, "Escort the visitor to the bus stop", "interactive")
    escort = "int-escort-poi_bus_stop"

    # The escort is open (accepted by its robot) while the patrol is still under way.
    async def escort_open() -> bool:
        s = await coord_state(stack)
        return s["tasks"][escort]["status"] in ("assigned", "in_progress") and any(
            s["tasks"][t]["status"] == "in_progress" for t in SURVEYS
        )

    await wait_for(escort_open, timeout=30)
    stack.world.pause()
    escort_robot = (await coord_state(stack))["tasks"][escort]["robot_id"]

    # Hot fleet change: go2_03 joins from the staging area and is admitted.
    await stack.add_robot(robot_id="go2_03")

    async def admitted() -> bool:
        return (await coord_state(stack))["fleet"].get("go2_03", {}).get("status") == "available"

    await wait_for(admitted, timeout=30)
    await stack.coordinator.drain()
    assert (await coord_state(stack))["tasks"][escort]["robot_id"] == escort_robot  # escort stays

    # Weather alert: emergency planned for the enlarged fleet.
    await stack.weather_alert()
    await stack.coordinator.drain()
    state = await coord_state(stack)
    assert state["posture"] == "emergency"
    assert state["tasks"][escort]["status"] in ("assigned", "in_progress")  # concluded, not dropped
    # The escort ends at the bus stop, an exposed POI: the escort robot then holds that post
    # (no second robot converging on the same spot).
    conclusion = "eme-hold_post-poi_bus_stop"
    assert state["tasks"][conclusion]["pinned_to"] == escort_robot
    assert state["tasks"][conclusion]["spec"]["depends_on"] == [escort]
    assert f"eme-return_home-{escort_robot}" not in state["tasks"]
    preempted = {t for t, e in state["tasks"].items() if e["status"] == "preempted"}
    assert preempted and preempted <= SURVEYS
    holds = {t: e for t, e in state["tasks"].items() if t.startswith("eme-hold_post")}
    assert set(holds) == {
        "eme-hold_post-poi_bike_rack",
        "eme-hold_post-poi_bus_stop",
        "eme-hold_post-poi_library_door",
    }
    assert all(e["robot_id"] for e in holds.values())  # robots sent to exposed POIs
    assert "go2_03" in {e["robot_id"] for e in holds.values()}  # the newcomer is allocated
    arb = decisions(stack, DecisionKind.ARBITRATE)[-1]
    assert escort in " ".join(arb.output["concluded"]) and set(arb.output["preempted"]) == preempted
    assert any(
        d.output.get("robot_id") == "go2_03" for d in decisions(stack, DecisionKind.FALLBACK)
    )

    stack.world.resume()

    async def escort_done() -> bool:
        return (await coord_state(stack))["tasks"][escort]["status"] == "done"

    await wait_for(escort_done, timeout=150)

    async def holding() -> bool:
        return (await coord_state(stack))["tasks"][conclusion]["status"] == "in_progress"

    await wait_for(holding, timeout=150)
    # The posts hold until the operator's all clear (2026-10-19): the mission stays open.
    final = await coord_state(stack)
    assert final["status"] == "active" and final["posture"] == "emergency"


# --------------------------------------------------------------------------- 6 + 7


async def test_6_anomaly_escalated_interrupt_resumed_by_cli(stack: SimStack, monkeypatch) -> None:
    from a6g_cli import main as cli

    stack.anomaly("poi_kiosk", "unattended backpack")
    await stack.mission("Check the info kiosk", posture="interactive")

    async def interrupted() -> bool:
        return bool(await stack.coordinator.interrupts(M))

    await wait_for(interrupted, timeout=60)
    (request,) = await stack.coordinator.interrupts(M)
    action = request["action_requests"][0]
    assert action["name"] == "confirm_observation"
    assert action["args"]["label"] == "unattended backpack"
    assert request["review_configs"][0]["allowed_decisions"] == ["approve", "edit", "reject"]

    # Robot reports keep flowing while the escalation waits; it is re-raised each run.
    await stack.coordinator.submit(M, {"type": "tick"})
    assert await stack.coordinator.interrupts(M)

    class InProcessRuns:  # what `a6g resume` calls on the LangGraph Agent Server
        async def wait(self, thread_id, assistant_id, *, command, **_):
            return await stack.coordinator.resume(thread_id, command["resume"]["decisions"])

    class Client:
        runs = InProcessRuns()

    await cli._resume(Client(), M, "approve", None, "coordinator")
    state = await coord_state(stack)
    assert not await stack.coordinator.interrupts(M)
    guard = [
        t
        for t in state["tasks"]
        if t.startswith("rou-hold_post") or ("hold_post" in t and "obs" in t)
    ]
    assert guard, state["tasks"].keys()
    assert state["tasks"][guard[0]]["robot_id"]
    esc = decisions(stack, DecisionKind.ESCALATE)
    assert esc[-1].output["operator_decision"] == {"type": "approve"}

    # 7. Every step is in the auditable timeline (`a6g decisions --mission m1`).
    from typer.testing import CliRunner

    result = CliRunner().invoke(
        cli.app, ["decisions", "--mission", M, "--log", str(stack.log.path)]
    )
    assert result.exit_code == 0
    for kind in ("decompose", "allocate", "accept", "skill", "escalate"):
        assert f" {kind} " in result.stdout, kind
