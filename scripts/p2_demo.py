"""Narrated P2 demo: the real robot agent on the simulator, in one process.

Wires the same pieces a deployment uses: simulator -> SkillService -> MCP server ->
SkillClient -> LangGraph agent (local SQLite checkpoints) -> uplink. The coordinator does
not exist until P3, so upstream reports go to a printing in-memory uplink, and the
"link" of that uplink follows the simulated 5G link.

    uv run python scripts/p2_demo.py accept
    uv run python scripts/p2_demo.py battery
    uv run python scripts/p2_demo.py linkloss
    uv run python scripts/p2_demo.py killswitch
    uv run python scripts/p2_demo.py restart
    uv run python scripts/p2_demo.py all          # every scenario in turn

Options: --speed N (simulated seconds per wall second, default 60), --llm (use the
coordinator_llm endpoint from models.yaml for justifications).
"""

import argparse
import asyncio
import contextlib
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from a6g_common.clock import ManualClock
from a6g_common.config import ModelRole, load_config
from a6g_common.models import chat_model
from a6g_common.schemas import (
    Envelope,
    EnvelopeBudgets,
    LinkQuality,
    PoiTarget,
    Pose2D,
    PoseTarget,
    Report,
    SectorTarget,
    TaskKind,
    TaskSpec,
)
from a6g_observability.decision_log import DecisionLog, format_timeline
from a6g_robot_agent import build_robot_agent
from a6g_robot_agent.skills import SkillClient
from a6g_robot_agent.uplink import RecordingUplink
from a6g_sim import SimRunner, SimWorld
from a6g_skill_mcp import SimBackend, SkillService, build_mcp_server
from a6g_transport.telemetry import TOPIC_KILLSWITCH, InMemoryBus
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

ROBOT = "go2_01"
WORK = Path("var/p2_demo")
WHOLE_CAMPUS = [(-500.0, -500.0), (500.0, -500.0), (500.0, 500.0), (-500.0, 500.0)]
T0 = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)


def say(msg: str) -> None:
    print(f"\n\033[1m== {msg}\033[0m")


class PrintingUplink(RecordingUplink):
    def __init__(self, world: SimWorld) -> None:
        super().__init__(connected=lambda: world.link_quality(ROBOT) is not LinkQuality.LOST)
        self.world = world

    async def send(self, report: Report) -> bool:
        ok = await super().send(report)
        if ok:
            p = report.payload
            what = {
                "skill_result": lambda: f"{p['task_id']} -> {p['result']['outcome']}",
                "decision": lambda: f"{p['kind']}: {p['justification'][:70]}",
                "envelope_status": lambda: f"envelope {p['status']} {p.get('tasks')}",
                "observation": lambda: f"'{p['label']}' conf={p['confidence']}",
                "task_status": lambda: f"{p['task_id']} {p['status']}",
            }.get(report.kind.value, lambda: "")()
            print(f"   [uplink t={self.world.clock.now():%H:%M:%S}] {report.kind.value:<15} {what}")
        return ok


def task(kind: TaskKind, target, task_id: str, caps: list[str], **kw) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        kind=kind,
        target=target,
        required_capabilities=caps,
        est_duration_s=kw.pop("est", 30),
        **kw,
    )


def survey(sector: str) -> TaskSpec:
    return task(
        TaskKind.SURVEY_SECTOR, SectorTarget(sector_id=sector), f"survey_{sector}", ["survey"]
    )


def inspect(poi: str) -> TaskSpec:
    return task(TaskKind.INSPECT_POI, PoiTarget(poi_id=poi), f"inspect_{poi}", ["inspect"], est=10)


def home(depends_on: list[str]) -> TaskSpec:
    return task(
        TaskKind.RETURN_HOME,
        PoseTarget(pose=Pose2D(x=0, y=0)),
        "return_home",
        [],
        est=0,
        depends_on=depends_on,
    )


MISSION = {"id": "m_demo"}


def envelope(tasks: list[TaskSpec], envelope_id: str = "env_demo") -> Envelope:
    return Envelope(
        envelope_id=envelope_id,
        robot_id=ROBOT,
        mission_id=MISSION["id"],
        tasks=tasks,
        region=WHOLE_CAMPUS,
        valid_from=T0,
        valid_until=T0 + timedelta(hours=8),
        budgets=EnvelopeBudgets(battery_floor_pct=20, uplink_mbps=3.0),
        swappable=True,
    )


class Demo:
    def __init__(self, speed: float, use_llm: bool) -> None:
        self.cfg = load_config("config", env={})
        self.world = SimWorld(self.cfg.campus, ManualClock(T0))
        self.world.add_robot(self.cfg.manifests[ROBOT])
        self.log = DecisionLog(WORK / "decisions.jsonl")
        self.service = SkillService(
            SimBackend(self.world, ROBOT, WORK / "images"),
            self.cfg.campus,
            self.log,
            battery_floor_pct=20,
            mission_id=MISSION["id"],
        )
        self.service.arm("demo-operator")
        self.uplink = PrintingUplink(self.world)
        self.bus = InMemoryBus()
        self.runner = SimRunner(self.world, dt=0.1, realtime_factor=speed)
        self.model = chat_model(ModelRole.COORDINATOR_LLM, self.cfg.models) if use_llm else None
        self.agent = None
        self._host: asyncio.Task | None = None
        self._stop = asyncio.Event()

    @property
    def robot(self):
        return self.world.robot(ROBOT)

    async def start_agent(self) -> None:
        ready: asyncio.Future = asyncio.get_running_loop().create_future()
        self._stop = asyncio.Event()

        async def process() -> None:
            async with contextlib.AsyncExitStack() as stack:
                cp = await stack.enter_async_context(
                    AsyncSqliteSaver.from_conn_string(str(WORK / f"{ROBOT}.sqlite"))
                )
                skills = await stack.enter_async_context(
                    SkillClient(build_mcp_server(self.service))
                )
                agent = build_robot_agent(
                    self.cfg,
                    ROBOT,
                    skills=skills,
                    checkpointer=cp,
                    uplink=self.uplink,
                    decisions=self.log,
                    bus=self.bus,
                    clock=self.world.clock,
                    poll_interval_s=0.05,
                    anomaly_source=self.world.anomalies.get,
                    model=self.model,
                )
                await stack.enter_async_context(agent)
                ready.set_result(agent)
                await self._stop.wait()

        self._host = asyncio.create_task(process())
        self.agent = await ready

    async def stop_agent(self) -> None:
        self._stop.set()
        await self._host

    async def watch_until(self, done, envelope_id: str = "env_demo", timeout: float = 120) -> dict:
        """Print task-state changes until ``done(state)``."""
        last = None
        async with asyncio.timeout(timeout):
            while True:
                state = await self.agent.state(envelope_id)
                tasks = {k: v["status"] for k, v in (state.get("tasks") or {}).items()}
                view = (
                    state.get("status"),
                    state.get("current_task_id"),
                    state.get("holding"),
                    json.dumps(tasks),
                )
                if view != last:
                    r = self.robot
                    print(
                        f"   [t={self.world.clock.now():%H:%M:%S} pos=({r.x:6.1f},{r.y:6.1f}) "
                        f"batt={r.battery_pct:5.1f}%] status={state.get('status')} "
                        f"running={state.get('current_task_id')} hold={state.get('holding')} "
                        f"queued={len(state.get('outbox') or [])}\n      tasks={tasks}"
                    )
                    last = view
                if done(state):
                    return state
                await asyncio.sleep(0.05)

    def print_reply(self, reply) -> None:
        print(f"   decision      : {reply.decision}")
        print(f"   accepted      : {reply.accepted_task_ids}")
        print(f"   refused       : { {k: v.value for k, v in reply.refusals.items()} }")
        print(f"   justification : {reply.justification}")

    # ------------------------------------------------------------------ scenarios

    async def accept(self) -> None:
        say("ACCEPTANCE 1: accept an envelope, execute every task, report upstream")
        env = envelope(
            [
                survey("sec_north"),
                inspect("poi_kiosk"),
                home(["survey_sec_north", "inspect_poi_kiosk"]),
            ]
        )
        self.print_reply(await self.agent.handle_envelope(env))
        await self.watch_until(lambda s: s.get("status") == "completed")
        print(
            f"   robot back at ({self.robot.x:.2f}, {self.robot.y:.2f}); "
            f"{len(self.uplink.reports)} reports delivered"
        )

    async def battery(self) -> None:
        say("ACCEPTANCE 2: battery-infeasible task refused with reason 'battery'")
        self.robot.battery_pct = 30.0
        print("   battery set to 30% (floor 20% + margin 5%)")
        print("\n   -- envelope with only the far east survey:")
        self.print_reply(
            await self.agent.handle_envelope(envelope([survey("sec_east")], "env_bat1"))
        )
        print("\n   -- envelope with the east survey AND a nearby inspection:")
        reply = await self.agent.handle_envelope(
            envelope([survey("sec_east"), inspect("poi_kiosk")], "env_bat2")
        )
        self.print_reply(reply)
        await self.watch_until(lambda s: s.get("status") == "completed", "env_bat2")

    async def linkloss(self) -> None:
        say("ACCEPTANCE 3: link loss -> finish current task, hold, queue, flush on reconnect")
        moving = self.world.pause_when(lambda w: w.robot(ROBOT).odometer_m > 3.0)
        env = envelope(
            [
                inspect("poi_kiosk"),
                inspect("poi_library_door"),
                home(["inspect_poi_kiosk", "inspect_poi_library_door"]),
            ],
            "env_link",
        )
        self.print_reply(await self.agent.handle_envelope(env))
        await moving.wait()
        print("\n   >>> 5G LINK LOST while walking to the first POI")
        self.world.inject_link_loss(ROBOT, 1e9)
        self.world.resume()
        state = await self.watch_until(
            lambda s: s.get("holding") and s.get("current_task_id") is None, "env_link"
        )
        print(f"\n   holding ({state['hold_reason']}); {len(state['outbox'])} reports queued:")
        for item in state["outbox"]:
            print(f"      - {item['kind']:<15} {item['report_id']}")
        late = await self.agent.handle_envelope(envelope([survey("sec_south")], "env_late"))
        print(f"   a new envelope while offline -> {late.decision} {dict(late.refusals)}")
        await asyncio.sleep(1.0)
        print(
            f"   still holding at ({self.robot.x:.1f}, {self.robot.y:.1f}), "
            f"moving={self.robot.job is not None}"
        )
        print("\n   >>> LINK RESTORED")
        self.robot.link_lost_until = None
        await self.watch_until(lambda s: s.get("status") == "completed", "env_link")

    async def killswitch(self) -> None:
        say("KILL-SWITCH: MQTT message stops the robot; the task waits for resume")
        moving = self.world.pause_when(lambda w: w.robot(ROBOT).odometer_m > 3.0)
        await self.agent.handle_envelope(envelope([inspect("poi_library_door")], "env_ks"))
        await moving.wait()
        print("   >>> a6g/killswitch {'state': 'on'}")
        await self.bus.publish(TOPIC_KILLSWITCH, json.dumps({"state": "on"}), qos=1, retain=True)
        await self.watch_until(
            lambda s: s.get("hold_reason") == "killswitch" and s.get("current_task_id") is None,
            "env_ks",
        )
        self.world.resume()
        await asyncio.sleep(1.0)
        print(
            f"   robot stopped at ({self.robot.x:.1f}, {self.robot.y:.1f}); "
            f"stop calls={self.robot.stop_count}"
        )
        print("   >>> operator: resume")
        await self.agent.handle_operator("resume")
        await self.watch_until(lambda s: s.get("status") == "completed", "env_ks")

    async def restart(self) -> None:
        say("RESTART: kill the agent process mid-mission; it resumes from local SQLite")
        reached = self.world.pause_when(lambda w: w.robot(ROBOT).odometer_m > 45.0)
        await self.agent.handle_envelope(
            envelope([inspect("poi_kiosk"), inspect("poi_library_door")], "env_rst")
        )
        await reached.wait()
        await self.watch_until(lambda s: True, "env_rst")
        print("   >>> agent process KILLED (edge not involved)")
        await self.stop_agent()
        print("   >>> agent process STARTED again on the same SQLite file")
        await self.start_agent()
        self.world.resume()
        await self.watch_until(lambda s: s.get("status") == "completed", "env_rst")


async def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "scenario", choices=["accept", "battery", "linkloss", "killswitch", "restart", "all"]
    )
    p.add_argument("--speed", type=float, default=60.0)
    p.add_argument("--llm", action="store_true")
    args = p.parse_args()
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)

    names = ["accept", "battery", "linkloss", "killswitch", "restart"]
    for name in names if args.scenario == "all" else [args.scenario]:
        MISSION["id"] = f"m_{name}"  # one mission per scenario keeps the log readable
        demo = Demo(args.speed, args.llm)  # fresh robot and sim clock per scenario
        async with demo.runner:
            await demo.start_agent()
            try:
                await getattr(demo, name)()
            finally:
                await demo.stop_agent()
    say(
        "Decision log of the last scenario: uv run a6g decisions "
        f"--log {WORK}/decisions.jsonl --mission {MISSION['id']}"
    )
    for line in format_timeline(
        DecisionLog(WORK / "decisions.jsonl").read(MISSION["id"])
    ).splitlines():
        print("  ", line[:160])


if __name__ == "__main__":
    asyncio.run(main())
