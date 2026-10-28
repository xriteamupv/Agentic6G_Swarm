"""A whole simulated deployment in one process (scenario tests and demos).

    SimWorld --SimBackend--> SkillService --MCP--> RobotAgent  --uplink-->  Coordinator
                                                       ^  ^--- contract-net ---^ |
                                                       +------- envelopes -------+
                               InMemoryBus (MQTT stand-in): state, status/last-will,
                               coordinator heartbeat, kill-switch, shared envelopes

Each robot agent and the coordinator run in their own host task ("process"), so they can
be crashed and restarted independently; links behave like a network (a dead process
raises, a coordinator that is down rejects reports). Agents and coordinator compute in
frozen simulated time and sleep in simulated time, so heartbeats and latency KPIs are on
the robots' time base regardless of CPU speed.
"""

import asyncio
import contextlib
import json
import logging
import os
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Generic, TypeVar

from a6g_common.clock import ManualClock
from a6g_common.config import A6GConfig, FleetDefaults, RobotManifest, load_manifest
from a6g_common.ids import IdFactory, new_id
from a6g_common.messages import NL
from a6g_common.schemas import (
    Award,
    Bid,
    CallForProposals,
    Envelope,
    EnvelopeReply,
    LinkQuality,
    Pose2D,
    Report,
    RobotState,
)
from a6g_coordinator.graph import CoordinatorDeps
from a6g_coordinator.links import A2ARobotLink
from a6g_coordinator.network import CapacityNetwork, NetworkBranchClient
from a6g_coordinator.persistence import open_persistence
from a6g_coordinator.runtime import Coordinator
from a6g_interactions import InteractionClient, InteractionStore
from a6g_kb import KnowledgeClient, KnowledgeService, build_kb_server
from a6g_kb.index import HashingEmbedder
from a6g_observability.checkpoint_meter import MeteredSaver
from a6g_observability.comms import fresh_log, set_sim_clock
from a6g_observability.decision_log import DecisionLog
from a6g_robot_agent import RobotAgent, build_robot_agent
from a6g_robot_agent.skills import SkillClient
from a6g_robot_agent.uplink import A2AUplink
from a6g_sim import SimRunner, SimWorld
from a6g_skill_mcp import SimBackend, SkillService, build_mcp_server
from a6g_transport.messaging import (
    TOPIC_HEARTBEAT,
    TOPIC_STATUS,
    A6GMessage,
    MessageType,
    configure_messaging,
    memory_bus,
)
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.store.memory import InMemoryStore

from a6g_devstack.inproc import InProcessNet

log = logging.getLogger(__name__)
# Simulation-only robots beyond the 3 of config/sim/fleet.yaml, in the order fleets grow
# (to 8: Go2 and Summit-XL mixed); used by `a6g-sim-stack --robots`, the outage sweep and G13.
EXTRA_ROBOTS = ("go2_03", "go2_04", "go2_05", "summit_02", "summit_03")

T = TypeVar("T")


class Process(Generic[T]):
    """Run an object's async contexts inside one dedicated task, like an OS process."""

    def __init__(
        self, name: str, build: Callable[[contextlib.AsyncExitStack], Awaitable[T]]
    ) -> None:
        self.name = name
        self._build = build
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> T:
        ready: asyncio.Future[T] = asyncio.get_running_loop().create_future()
        self._stop = asyncio.Event()

        async def host() -> None:
            try:
                async with contextlib.AsyncExitStack() as stack:
                    ready.set_result(await self._build(stack))
                    await self._stop.wait()
            except BaseException as exc:
                if not ready.done():
                    ready.set_exception(exc)
                raise

        self._task = asyncio.create_task(host(), name=self.name)
        return await ready

    async def stop(self) -> None:
        if self._task is not None:
            self._stop.set()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None


@dataclass
class RobotProc:
    robot_id: str
    service: SkillService
    agent: RobotAgent | None = None
    process: Process[RobotAgent] | None = None

    @property
    def alive(self) -> bool:
        return self.agent is not None


class _RobotLink:
    """Coordinator -> robot over the 'network'."""

    def __init__(self, stack: "SimStack", robot_id: str) -> None:
        self.stack, self.robot_id = stack, robot_id

    def _agent(self) -> RobotAgent:
        proc = self.stack.robots.get(self.robot_id)
        if proc is None or proc.agent is None:
            raise ConnectionError(f"{self.robot_id} unreachable")
        if self.stack.world.link_quality(self.robot_id) is LinkQuality.LOST:
            raise ConnectionError(f"{self.robot_id} out of coverage")
        return proc.agent

    def _link(self) -> A2ARobotLink:
        self._agent()  # unreachable / out of coverage: nothing is sent
        return A2ARobotLink.with_client(self.stack.net.client("coordinator", self.robot_id))

    async def send_envelope(self, envelope: Envelope) -> EnvelopeReply:
        return await self._link().send_envelope(envelope)

    async def get_state(self) -> RobotState:
        return await self._link().get_state()

    async def get_queue(self) -> dict[str, Any]:
        return await self._link().get_queue()


class _Uplink:
    """Robot -> coordinator reports; fail while the coordinator is down or out of coverage."""

    def __init__(self, stack: "SimStack", robot_id: str) -> None:
        self.stack, self.robot_id = stack, robot_id

    async def send(self, report: Report, nl: dict[str, Any] | None = None) -> bool:
        coord = self.stack.coordinator
        if coord is None or not coord.running:
            return False
        if self.stack.world.link_quality(self.robot_id) is LinkQuality.LOST:
            return False
        self.stack.delivered.append(report)
        return await A2AUplink(self.stack.net.client(self.robot_id, "coordinator")).send(
            report, nl=nl
        )


class _Peer:
    def __init__(self, stack: "SimStack", robot_id: str, sender: str) -> None:
        self.stack, self.robot_id, self.sender = stack, robot_id, sender

    def _client(self):
        proc = self.stack.robots.get(self.robot_id)
        if proc is None or proc.agent is None:
            raise ConnectionError(f"{self.robot_id} unreachable")
        # Robot-to-robot traffic crosses the radio twice (UE to UE through the core): it
        # fails when either robot is out of coverage.
        for rid in (self.sender, self.robot_id):
            if rid in self.stack.robots and self.stack.world.link_quality(rid) is LinkQuality.LOST:
                raise ConnectionError(f"{rid} out of coverage")
        return self.stack.net.client(self.sender, self.robot_id)

    async def cfp(self, cfp: CallForProposals, nl: dict[str, Any] | None = None) -> Bid:
        reply = await self._client().send(
            A6GMessage(MessageType.CFP, cfp.model_dump(mode="json"), NL(**nl) if nl else None)
        )
        return Bid.model_validate(reply.payload)

    async def award(self, award: Award) -> dict[str, Any]:
        reply = await self._client().send(
            A6GMessage(MessageType.AWARD, award.model_dump(mode="json"))
        )
        return reply.payload


class _Peers:
    def __init__(self, stack: "SimStack", robot_id: str) -> None:
        self.stack, self.robot_id = stack, robot_id

    def peer_ids(self) -> list[str]:
        return sorted(r for r in self.stack.robots if r != self.robot_id)

    def link(self, robot_id: str) -> _Peer:
        return _Peer(self.stack, robot_id, self.robot_id)


@dataclass
class SimStack:
    cfg: A6GConfig
    workdir: Path
    model: BaseChatModel | None = None
    #: The robots' cognitive core (role onboard_vlm); None: ``model``.
    robot_model: BaseChatModel | None = None
    #: Knowledge-base embedder; None: the deterministic hashing embedder.
    kb_embedder: Any = None
    #: Perception triage for every robot (None: stub driven by injected anomalies).
    triage: Any = None
    edge: Any = None
    auto_arm: bool = True
    dt: float = 0.1
    # Simulated time runs at most this many times faster than wall time, so heartbeats and
    # timeouts stay meaningful while processes do wall-clock work (sqlite, MCP, LLM calls).
    realtime_factor: float | None = field(
        default_factory=lambda: float(os.environ.get("A6G_SIM_REALTIME_FACTOR", "50"))
    )
    #: While a model (or a remote service) answers: "full" (default) the world runs on at
    #: full speed, so the wait counts as wall latency x the speed factor in simulated time;
    #: "freeze" it waits, so model calls take no simulated time.
    model_time: str = "full"
    #: Record checkpoint bytes and write latency (``checkpoints.jsonl``)
    checkpoint_meter: bool = False
    #: Coordinator persistence: "sqlite" (default) or "postgres" (needs pg_dsn).
    coordinator_persistence: str = "sqlite"
    #: Baseline comparison: none (P), independent (B0), central_memory (B1: the
    #: coordinator's state in memory, lost on a restart), central_twin (B2).
    baseline: str = "none"
    pg_dsn: str | None = None
    start_time: datetime = field(default_factory=lambda: datetime(2026, 1, 1, 8, 0, tzinfo=UTC))
    ids: IdFactory = new_id
    #: Telemetry plane (default: in-memory; the sim-stack service passes an MQTT bus).
    bus: Any = None
    #: Robot -> coordinator uplink per robot (default: in-process coordinator).
    uplink_factory: Callable[[str], Any] | None = None
    #: A2A URL each robot announces in its status (lets a remote coordinator reach it).
    a2a_urls: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.baseline != "none":
            defaults = FleetDefaults.model_validate(
                {**self.cfg.fleet.defaults.model_dump(), "baseline": self.baseline}
            )
            fleet = self.cfg.fleet.model_copy(update={"defaults": defaults})
            self.cfg = self.cfg.model_copy(update={"fleet": fleet})
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.world = SimWorld(
            self.cfg.campus,
            ManualClock(self.start_time),
            charge_below_pct=self.cfg.allocator.recharge_idle_pct,  # no passive top-up
        )
        if self.cfg.network.coverage.source != "shadows":  # levels from the twin
            from a6g_common.coverage import load_or_build

            self.world.coverage = load_or_build(self.cfg)
            self.world.coverage_net = self.cfg.network
        # The network stand-in, kept across coordinator restarts (it is the
        # network branch, not the coordinator's state)
        self.network = NetworkBranchClient()
        if self.cfg.network.mode == "capacity":
            self.network = CapacityNetwork(
                self.cfg.network,
                self.world.coverage,
                where=lambda f: (f["x"], f["y"]) if "x" in f and "y" in f else None,
                accounting_only=self.cfg.network.reconcile.mode == "off",
            )
        self.world.models_freeze_time = self.model_time == "freeze"
        self.bus = self.bus if self.bus is not None else memory_bus("devstack")
        self.log = DecisionLog(self.workdir / "decisions.jsonl")
        # Every inter-agent message is measured, here per stack workdir.
        configure_messaging(self.cfg.comms, log_path=fresh_log(self.workdir / "comms.jsonl"))
        set_sim_clock(self.world.clock.now)
        self.net = InProcessNet(self._robot_agent, lambda: self.coordinator)
        # Campus knowledge server, in-process; served over HTTP by the service.
        self.kb_service = KnowledgeService(
            self.cfg.campus,
            self.cfg.knowledge,
            embedder=self.kb_embedder or HashingEmbedder(),
            clock=self.world.clock,
        )
        self.kb_server = build_kb_server(self.kb_service)
        # Accepted statuses reach escorting robots at once (a6g/kb/status).
        self.kb_service.on_status.append(self._publish_status)
        self._pending: set[asyncio.Task[Any]] = set()
        # The InteractionStore: in memory, its own service; it survives
        # coordinator restarts and is served over HTTP by the stack service.
        self.interactions = InteractionStore(
            self.cfg.interactions.interaction_ttl_min, clock=self.world.clock
        )
        #: What each robot said recently (its speech output; never logged).
        self.speech: dict[str, deque[str]] = {}
        self.echo_speech = False  # the stack service prints speech to its console
        # Stands in for the Agent Server's Postgres-backed store: survives coordinator restarts.
        self.store = InMemoryStore()
        self.runner = SimRunner(self.world, dt=self.dt, realtime_factor=self.realtime_factor)
        self.robots: dict[str, RobotProc] = {}
        self.coordinator: Coordinator | None = None
        self._coord_proc: Process[Coordinator] | None = None
        self.delivered: list[Report] = []

    def _robot_agent(self, robot_id: str) -> RobotAgent:
        proc = self.robots.get(robot_id)
        if proc is None or proc.agent is None:
            raise ConnectionError(f"{robot_id} unreachable")
        return proc.agent

    # ------------------------------------------------------------------ lifecycle

    async def start(self, *, coordinator: bool = True) -> "SimStack":
        # Bring every process up first, then let simulated time flow.
        if coordinator:
            await self.start_coordinator()
        for robot in self.cfg.fleet.robots:
            await self.start_robot(robot.robot_id)
        self.runner.start()
        return self

    async def stop(self) -> None:
        if self._coord_proc is not None:
            await self._coord_proc.stop()
            self._coord_proc = self.coordinator = None
        for proc in self.robots.values():
            if proc.process is not None:
                await proc.process.stop()
                proc.agent = proc.process = None
        await self.runner.stop()
        await self.net.aclose()

    async def __aenter__(self) -> "SimStack":
        return await self.start()

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # ------------------------------------------------------------------ robots

    async def start_robot(
        self, robot_id: str, manifest: RobotManifest | None = None, pose: Pose2D | None = None
    ) -> RobotAgent:
        manifest = manifest or self.cfg.manifests[robot_id]
        if not manifest.battery_reliable:
            # The 2D simulator models every battery exactly (the fleet file's
            # simulated_battery). Robots added at the staging area are told too: otherwise
            # a robot sees its battery as unknown, never charges and runs flat.
            manifest = manifest.model_copy(update={"battery_reliable": True})
        self.cfg.manifests[robot_id] = manifest  # hot-added robots join the config view
        if robot_id not in self.world.robots:
            self.world.add_robot(manifest, pose)
        proc = self.robots.get(robot_id)
        resume = proc is not None  # a restart inside this stack; a fresh stack starts clean
        if proc is None:
            backend = SimBackend(self.world, robot_id, self.workdir / "images")
            backend.image_retention_h = self.cfg.interactions.image_retention_h
            service = SkillService(
                backend,
                self.cfg.campus,
                self.log,
                battery_floor_pct=self.cfg.allocator.default_battery_floor_pct,
            )
            if self.auto_arm:
                service.arm("devstack")
            proc = self.robots[robot_id] = RobotProc(robot_id, service)
        service = proc.service

        builds = 0

        async def build(stack: contextlib.AsyncExitStack) -> RobotAgent:
            nonlocal builds
            checkpointer = await stack.enter_async_context(
                AsyncSqliteSaver.from_conn_string(str(self.workdir / f"{robot_id}.sqlite"))
            )
            if self.checkpoint_meter:
                checkpointer = MeteredSaver(
                    checkpointer, robot_id, self.workdir / "checkpoints.jsonl"
                )
            skills = await stack.enter_async_context(SkillClient(build_mcp_server(service)))
            agent = build_robot_agent(
                self.cfg,
                robot_id,
                skills=skills,
                checkpointer=checkpointer,
                uplink=self.uplink_factory(robot_id)
                if self.uplink_factory
                else _Uplink(self, robot_id),
                a2a_url=self.a2a_urls.get(robot_id),
                decisions=self.log,
                bus=self.bus,
                clock=self.world.clock,
                ids=self.ids,
                model=self.robot_model or self.model,
                triage=self.triage,
                edge=self.edge,
                anomaly_source=self.world.anomalies.get,
                peers=_Peers(self, robot_id),
                sleep=self.world.sleep,
                gate=self.world.frozen,
                kb=KnowledgeClient(
                    self.kb_server, agent_id=robot_id, timeout_s=self.cfg.knowledge.timeout_s
                ),
                interactions=InteractionClient(
                    self.interactions,
                    agent_id=robot_id,
                    timeout_s=self.cfg.interactions.timeout_s,
                ),
            )
            agent.resume = resume or builds > 0  # rebuilt after a crash: resume
            agent.on_speech = self._on_speech
            builds += 1
            await stack.enter_async_context(agent)
            return agent

        proc.process = Process(f"robot-{robot_id}", build)
        proc.agent = await proc.process.start()
        return proc.agent

    async def fail_robot(self, robot_id: str) -> None:
        """``a6g sim inject robot_fail``: the robot dies; its agent goes silent."""
        self.world.fail_robot(robot_id)
        proc = self.robots[robot_id]
        if proc.process is not None:
            await proc.process.stop()
        proc.agent = proc.process = None

    async def add_robot(
        self, manifest_path: Path | str | None = None, robot_id: str | None = None
    ) -> RobotAgent:
        """``a6g sim add_robot``: hot fleet change from the staging area."""
        if manifest_path is None:
            if robot_id is None:
                raise ValueError("give a manifest path or a robot_id")
            manifest_path = self.cfg.config_dir / "robots" / f"{robot_id}.yaml"
        manifest = load_manifest(Path(manifest_path))
        return await self.start_robot(manifest.robot_id, manifest)

    def outage_zone(self, polygon: list[tuple[float, float]]) -> None:
        """An injected outage zone (scenarios, tests): see ``SimWorld.inject_outage_zone``."""
        self.world.inject_outage_zone(polygon)

    def link_loss(self, robot_id: str, seconds: float) -> None:
        self.world.inject_link_loss(robot_id, seconds)

    def anomaly(self, poi_id: str, label: str) -> None:
        self.world.inject_anomaly(poi_id, label)

    # ------------------------------------------------------------------ coordinator

    async def start_coordinator(self) -> Coordinator:
        async def build(stack: contextlib.AsyncExitStack) -> Coordinator:
            if self.coordinator_persistence == "postgres":
                checkpointer, self.store = await stack.enter_async_context(
                    open_persistence("postgres", dsn=self.pg_dsn)
                )
            elif self.baseline == "central_memory":  # B1: nothing survives a restart
                checkpointer = MemorySaver()
            else:
                checkpointer = await stack.enter_async_context(
                    AsyncSqliteSaver.from_conn_string(str(self.workdir / "coordinator.sqlite"))
                )
            if self.checkpoint_meter:
                checkpointer = MeteredSaver(
                    checkpointer, "coordinator", self.workdir / "checkpoints.jsonl"
                )
            deps = CoordinatorDeps(
                cfg=self.cfg,
                links=lambda rid, entry: _RobotLink(self, rid),
                decisions=self.log,
                clock=self.world.clock,
                ids=self.ids,
                model=self.model,
                network=self.network,
                kb=KnowledgeClient(
                    self.kb_server,
                    agent_id="coordinator",
                    timeout_s=self.cfg.knowledge.timeout_s,
                ),
                interactions=InteractionClient(
                    self.interactions,
                    agent_id="coordinator",
                    timeout_s=self.cfg.interactions.timeout_s,
                ),
            )
            coord = Coordinator(
                deps,
                checkpointer,
                self.store,
                bus=_as_agent(self.bus, "coordinator"),
                sleep=self.world.sleep,
                gate=self.world.frozen,
            )
            await stack.enter_async_context(coord)
            return coord

        self._coord_proc = Process("coordinator", build)
        self.coordinator = await self._coord_proc.start()
        return self.coordinator

    async def kill_coordinator(self) -> None:
        """``a6g sim inject coordinator_kill``: crash; the broker publishes the last-will."""
        if self.coordinator is not None:
            await self.coordinator.stop(graceful=False)
        if self._coord_proc is not None:
            await self._coord_proc.stop()
        self._coord_proc = self.coordinator = None
        # The broker publishes the coordinator's last-will.
        await _as_agent(self.bus, "coordinator").publish(
            TOPIC_HEARTBEAT, "offline", qos=1, retain=True
        )

    # ------------------------------------------------------------------ missions & events

    async def mission(
        self, text: str, mission_id: str = "m1", posture: str = "routine"
    ) -> dict[str, Any]:
        assert self.coordinator is not None
        return await self.coordinator.goal(mission_id, text, posture)

    async def weather_alert(self) -> None:
        """``a6g sim inject weather_alert``: emulated rain -> emergency at exposed POIs."""
        assert self.coordinator is not None
        exposed = [p.poi_id for p in self.cfg.campus.pois if p.exposed]
        await self.coordinator.broadcast(
            {
                "type": "posture",
                "posture": "emergency",
                "text": "weather alert: heavy rain",
                "reason": "weather_alert",
                "exposed_pois": exposed,
            }
        )

    async def say(
        self, robot_id: str, text: str, lang: str | None = None, end: bool = False
    ) -> str:
        """``a6g sim say [--lang] [--end]``: a person speaks to the robot (text only)."""
        return await self._robot_agent(robot_id).say(text, lang=lang, end=end)

    def _on_speech(self, robot_id: str, text: str) -> None:
        self.speech.setdefault(robot_id, deque(maxlen=20)).append(text)
        if self.echo_speech:
            print(f"🔊 {robot_id}: {text}", flush=True)

    def _publish_status(self, report: Any) -> None:
        payload = json.dumps(
            {
                "subject": report.subject,
                "status": report.status,
                "restrictive": report.restrictive,
                "status_id": report.status_id,
            }
        )
        try:
            task = asyncio.get_running_loop().create_task(
                _as_agent(self.bus, "kb").publish("a6g/kb/status", payload, qos=1)
            )
        except RuntimeError:  # no loop (a synchronous caller): robots check at each leg
            return
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def announce_offline(self, robot_id: str) -> None:
        await _as_agent(self.bus, robot_id).publish(
            TOPIC_STATUS.format(robot_id=robot_id), '{"state": "offline"}', qos=1, retain=True
        )


def _as_agent(bus: Any, agent_id: str) -> Any:
    """``bus`` publishing as ``agent_id`` (one shared bus in single-process stacks)."""
    return bus.as_agent(agent_id) if hasattr(bus, "as_agent") else bus
