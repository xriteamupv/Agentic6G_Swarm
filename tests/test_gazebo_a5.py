"""SPEC A1 stage A5 acceptance in Gazebo (``pytest --gazebo``): multi-robot scenarios.

``GzStack`` with the Summit and two Go2 proxies, all on ``gnss_ekf`` with ideal RTK (the Go2
2D scan is empty in the open plaza, see A4) and an in-process coordinator (template
narration, no LLM), so the scenarios run unattended. The same stack runs behind
``langgraph dev`` and agent-chat-ui with ``a6g-gz-stack`` (README).

- G2: a routine survey completes with every robot physically driving its sector;
- G5 (same run): the east-corridor survey ends inside ``shadow_east``: after the hold the
  robot drives to a recovery point, reconnects and flushes its reports;
- G3: ``robot_fail go2_01`` mid-survey: the model stops; its task is reallocated and driven
  by another robot;
- G4: ``weather_alert`` during an open escort: the escort is concluded, the survey is
  preempted and robots drive to the exposed POIs;
- G7: ``a6g killswitch on`` stops every moving model within 1 s of sim time, through both
  paths and with either path alone;
- every run: the mission's decision timeline is complete; run metrics go to CSV.

Each scenario takes tens of minutes of wall time (Gazebo runs at ~0.7x with three robots).
"""

import asyncio
import contextlib
import itertools
import math
import os
import socket
import time
from pathlib import Path

import pytest
from a6g_common.config import load_config
from a6g_common.geometry import point_in_polygon
from a6g_common.schemas import DecisionKind, LinkQuality, Pose2D
from a6g_observability.metrics import mission_metrics, write_csv
from gz_harness import available

pytestmark = pytest.mark.gazebo

SURVEYS = {
    "rou-survey_sector-sec_north": "sec_north",
    "rou-survey_sector-sec_south": "sec_south",
    "rou-survey_sector-sec_east": "sec_east",
}
METRICS = Path("var/gz/a5")
#: The campus scale under test (``A6G_WORLD_SCALE``; robots keep their real size).
SCALE = float(os.environ.get("A6G_WORLD_SCALE") or 1.0)


def metres(full_scale: float, floor: float = 1.0) -> float:
    """A distance designed at scale 1, in the campus under test (never below ``floor``)."""
    return max(floor, full_scale * SCALE)


@pytest.fixture(scope="module", autouse=True)
def _gazebo_available() -> None:
    reason = available()
    if reason:
        pytest.skip(reason)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("A6G_DECISION_LOG", str(tmp_path / "decisions.jsonl"))
    if SCALE != 1.0:
        # the A6G_WORLD_SCALE world must have been generated (a6g campus build --scale)
        from a6g_common.campus import world_name

        world = (
            Path("sim_gazebo/src/a6g_gz_worlds/worlds") / f"{world_name('upv_vera_cpi', SCALE)}.sdf"
        )
        if not world.exists():
            pytest.skip(f"run `uv run a6g campus build --scale {SCALE:g}` first")
    return tmp_path


def load():
    return load_config("config", fleet_file="gazebo/fleet.yaml", profile="gazebo")


@contextlib.asynccontextmanager
async def gz_stack(workdir: Path, **kw):
    from a6g_devstack.gz_stack import GzStack

    coordinator = kw.pop("coordinator", True)
    stack = GzStack(load(), workdir, **kw)
    try:
        await stack.start(coordinator=coordinator)
        yield stack
    finally:
        await stack.stop()


class Tracker:
    """Ground-truth trajectories and link qualities, sampled while a scenario runs."""

    def __init__(self, stack) -> None:
        self.stack = stack
        self.paths: dict[str, list[tuple[float, float]]] = {}
        self.links: dict[str, list[LinkQuality]] = {}
        self._task: asyncio.Task | None = None

    async def _run(self) -> None:
        while True:
            for rid, p in self.stack.links.poses.items():
                if "x" in p:
                    self.paths.setdefault(rid, []).append((p["x"], p["y"]))
                    self.links.setdefault(rid, []).append(self.stack.links.quality(rid))
            await asyncio.sleep(1.0)

    def __enter__(self) -> "Tracker":
        self._task = asyncio.create_task(self._run())
        return self

    def __exit__(self, *exc) -> None:
        assert self._task is not None
        self._task.cancel()

    def travelled(self, rid: str) -> float:
        p = self.paths.get(rid, [])
        return sum(math.dist(a, b) for a, b in itertools.pairwise(p))

    def visited(self, rid: str, polygon) -> int:
        return sum(point_in_polygon(xy, polygon) for xy in self.paths.get(rid, []))

    def came_within(self, rid: str, xy, radius: float) -> bool:
        return any(math.dist(p, xy) <= radius for p in self.paths.get(rid, []))


async def tasks(stack, mission: str) -> dict:
    return (await stack.coordinator.state(mission)).get("tasks") or {}


async def wait_for(pred, timeout_s: float, every_s: float = 5.0, what: str = "") -> None:
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if await pred():
            return
        await asyncio.sleep(every_s)
    raise TimeoutError(f"timed out waiting for {what or pred.__name__}")


def timeline_kinds(stack, mission: str) -> set[DecisionKind]:
    return {d.kind for d in stack.log.read(mission)}


def save_metrics(stack, mission: str) -> list[dict]:
    rows = mission_metrics(
        mission, stack.log.read(mission), stack.injections, stack.bytes_per_robot()
    )
    write_csv(METRICS / f"{mission}.csv", rows)
    print(f"metrics {mission}:", rows)
    return rows


# --------------------------------------------------------------------------- G2 + G5


async def test_g2_g5_survey_completes_and_shadow_recovery(env: Path) -> None:
    async with gz_stack(env / "stack") as stack:
        campus = stack.cfg.campus
        with Tracker(stack) as track:
            await stack.mission("Patrol the campus", "g2")

            async def surveys_done() -> bool:
                t = await tasks(stack, "g2")
                return bool(t) and all(t.get(k, {}).get("status") == "done" for k in SURVEYS)

            await wait_for(surveys_done, 90 * 60, 10, "all surveys done")
            final = await tasks(stack, "g2")
        kinds = timeline_kinds(stack, "g2")
        decisions = stack.log.read("g2") + [d for d in stack.log.read() if d.mission_id is None]
        rows = save_metrics(stack, "g2")

    for task_id, sector_id in SURVEYS.items():
        rid = final[task_id]["robot_id"]
        polygon = campus.sector(sector_id).polygon
        print(f"G2 {sector_id}: {rid} travelled {track.travelled(rid):.0f} m, "
              f"{track.visited(rid, polygon)} samples inside the sector")  # fmt: skip
        assert track.visited(rid, polygon) > 30  # physically drove its sector
    east = final["rou-survey_sector-sec_east"]["robot_id"]
    links = track.links[east]
    assert LinkQuality.LOST in links, "the east survey never entered the coverage shadow"
    assert links[-1] is LinkQuality.OK  # reconnected
    # SPEC A1 12.1, from the robot's decisions: finish the task, hold, seek coverage at the
    # nearest recovery point, reconnect (the drive stops once coverage is back) and flush.
    steps = [
        d.output for d in decisions if d.actor.startswith(east) and d.kind is DecisionKind.FALLBACK
    ]
    print(f"G5 {east}:", steps)
    seq = ["lost", "hold", "seek_coverage", "ok"]
    found = []
    for o in steps:
        tag = o.get("step") or o.get("link_quality")
        if len(found) < len(seq) and tag == seq[len(found)]:
            found.append(tag)
    assert found == seq, f"hold_then_recover sequence incomplete: {found}"
    flushed = [o for o in steps if o.get("link_quality") == "ok"]
    assert flushed[-1]["queued_reports"] > 0  # reports queued offline were flushed
    target = next(o["recovery_point"] for o in steps if o.get("step") == "seek_coverage")
    point = next(r for r in campus.link_recovery_points if r.point_id == target)
    # It drove towards the recovery point: it ends closer to it than where the survey ended.
    end_wp = campus.sector("sec_east").survey_waypoints[-1]
    goal = (point.pose.x, point.pose.y)
    assert math.dist(track.paths[east][-1], goal) < math.dist((end_wp.x, end_wp.y), goal) - metres(
        5, floor=0.0
    )
    # The survey ended in the shadow: "done" reached the coordinator only after the flush.
    assert {DecisionKind.DECOMPOSE, DecisionKind.ALLOCATE, DecisionKind.SKILL} <= kinds
    assert any(r["metric"] == "completion_time_s" for r in rows)


# --------------------------------------------------------------------------- G3


async def test_g3_robot_fail_reallocates_to_the_others(env: Path) -> None:
    async with gz_stack(env / "stack") as stack:
        with Tracker(stack) as track:
            await stack.mission("Patrol the campus", "g3")

            async def go2_01_underway() -> bool:
                t = await tasks(stack, "g3")
                mine = [k for k, e in t.items() if e.get("robot_id") == "go2_01"]
                return bool(mine) and track.travelled("go2_01") > metres(10)

            await wait_for(go2_01_underway, 20 * 60, 5, "go2_01 surveying")
            lost = next(
                k for k, e in (await tasks(stack, "g3")).items() if e.get("robot_id") == "go2_01"
            )
            await stack.fail_robot("go2_01")
            frozen_at = track.paths["go2_01"][-1]
            await asyncio.sleep(15)
            assert math.dist(track.paths["go2_01"][-1], frozen_at) < 0.2  # the model stopped

            async def reallocated_and_done() -> bool:
                e = (await tasks(stack, "g3")).get(lost, {})
                return e.get("robot_id") not in (None, "go2_01") and e.get("status") == "done"

            await wait_for(reallocated_and_done, 120 * 60, 10, f"{lost} redone by another robot")
            taker = (await tasks(stack, "g3"))[lost]["robot_id"]
        kinds = timeline_kinds(stack, "g3")
        rows = save_metrics(stack, "g3")
    polygon = stack.cfg.campus.sector(SURVEYS[lost]).polygon
    print(f"G3: {lost} reallocated to {taker}")
    assert track.visited(taker, polygon) > 30  # driven by the other robot
    assert DecisionKind.REALLOCATE in kinds
    assert any(r["metric"] == "reallocation_latency_s" for r in rows)


# --------------------------------------------------------------------------- G4


async def test_g4_weather_alert_concludes_escort_and_holds_exposed_pois(env: Path) -> None:
    escort = "int-escort-poi_bus_stop"
    async with gz_stack(env / "stack") as stack:
        campus = stack.cfg.campus
        with Tracker(stack) as track:
            # Interactive goals queue behind a robot's current routine task (only emergencies
            # preempt), so the escort goes first and the patrol follows while it is under way.
            await stack.coordinator.goal("g4", "Escort the visitor to the bus stop", "interactive")

            async def escort_open() -> bool:
                e = (await tasks(stack, "g4")).get(escort, {})
                return e.get("status") == "in_progress" and track.travelled(e["robot_id"]) > metres(
                    5
                )

            await wait_for(escort_open, 20 * 60, 5, "escort under way")
            await stack.coordinator.goal("g4", "Patrol the campus", "routine")
            await asyncio.sleep(20)  # surveys allocated to the other robots
            await stack.weather_alert()
            await asyncio.sleep(10)
            t = await tasks(stack, "g4")
            assert t[escort]["status"] in ("assigned", "in_progress", "done")  # not dropped
            preempted = {k for k, e in t.items() if e["status"] == "preempted"}
            assert preempted and preempted <= set(SURVEYS)
            holds = {k: e for k, e in t.items() if k.startswith("eme-hold_post")}
            assert {k.removeprefix("eme-hold_post-") for k in holds} == {
                p.poi_id for p in campus.pois if p.exposed
            }

            async def at_exposed_pois() -> bool:
                t = await tasks(stack, "g4")
                if t[escort]["status"] != "done":
                    return False
                for k, e in t.items():
                    if not k.startswith("eme-hold_post") or not e.get("robot_id"):
                        continue
                    pose = campus.poi(k.removeprefix("eme-hold_post-")).pose
                    if track.came_within(e["robot_id"], (pose.x, pose.y), 1.5):
                        return True
                return False

            await wait_for(at_exposed_pois, 60 * 60, 10, "escort concluded and a post reached")
            final = await tasks(stack, "g4")
        save_metrics(stack, "g4")
    reached = {
        k: e["robot_id"]
        for k, e in final.items()
        if k.startswith("eme-hold_post")
        and e.get("robot_id")
        and track.came_within(
            e["robot_id"],
            (campus.poi(k.removeprefix("eme-hold_post-")).pose.x,
             campus.poi(k.removeprefix("eme-hold_post-")).pose.y),
            1.5,
        )
    }  # fmt: skip
    print("G4: escort", final[escort]["status"], "posts reached:", reached)
    assert final[escort]["status"] == "done"  # concluded, not dropped
    assert reached


# --------------------------------------------------------------------------- G7


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.parametrize("paths", [("mqtt", "mcp"), ("mqtt",), ("mcp",)], ids="+".join)
async def test_g7_killswitch_stops_all_models_within_1s(env: Path, monkeypatch, paths) -> None:
    from a6g_cli.main import _killswitch
    from a6g_transport.messaging import mqtt_bus
    from amqtt.broker import Broker

    port = _free_port()
    monkeypatch.setenv("A6G_MQTT_PORT", str(port))
    broker = Broker(
        {
            "listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{port}"}},
            "plugins": {
                "amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True}
            },
        }
    )
    await broker.start()
    try:
        async with (
            mqtt_bus("127.0.0.1", port, agent_id="operator", client_id="a6g-gz-test") as bus,
            gz_stack(env / "stack", bus=bus) as stack,
        ):
            if SCALE == 1.0:
                goals = {
                    "summit_01": (40.0, -20.0),
                    "go2_01": (-40.0, 20.0),
                    "go2_02": (-40.0, -20.0),
                }
            else:  # long enough that all three are under way at once in a small campus
                goals = {
                    "summit_01": (-130.0 * SCALE, -10.0 * SCALE),
                    "go2_01": (65.0 * SCALE, 22.0 * SCALE),
                    "go2_02": (-40.0 * SCALE, -20.0 * SCALE),
                }
            runs = [
                asyncio.create_task(stack.robots[r].service.navigate_to(pose=Pose2D(x=x, y=y)))
                for r, (x, y) in goals.items()
            ]

            async def all_moving() -> bool:  # driving or turning in place
                p = (await stack.poses())["robots"]
                return all(
                    abs(p[r].get("vx", 0)) > 0.1 or abs(p[r].get("wz", 0)) > 0.05 for r in goals
                )

            await wait_for(all_moving, 300, 1, "all robots moving")
            t0 = (await stack.poses())["sim_time"]
            report = await _killswitch(stack.cfg, True, list(goals), "pytest", paths=paths)
            stopped_at = None
            for _ in range(400):
                snap = await stack.poses()
                p = snap["robots"]
                if all(abs(p[r]["vx"]) < 0.02 and abs(p[r]["wz"]) < 0.05 for r in goals):
                    stopped_at = snap["sim_time"]
                    break
                await asyncio.sleep(0.05)
            await asyncio.gather(*runs, return_exceptions=True)
            await _killswitch(stack.cfg, False, list(goals), "pytest", paths=("mqtt",))
    finally:
        await broker.shutdown()
    print(f"G7 {'+'.join(paths)}: {report}; stopped after {stopped_at and stopped_at - t0} s sim")
    assert all(all(not v.startswith("FAILED") for v in r.values()) for r in report.values())
    assert stopped_at is not None and stopped_at - t0 <= 1.0
