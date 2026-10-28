"""``GzStack``: the simulated deployment on Gazebo, counterpart of ``SimStack``.

    Gazebo world (one server) + gz_control (ground truth, freeze, spawn)
    per robot, in its own ROS domain:  robot.launch.py (spawn, bridge, shims, localization,
        Nav2)  ->  ros_adapter (ROS Python)  ->  SkillService (Go2/SummitBackend, served on
        the fleet's MCP URL)  ->  RobotAgent (A2A server, MQTT telemetry)
    LinkEmulator: injected link losses + coverage shadows from ground-truth poses

The robot side is the same code as on hardware; only the simulator, the link emulation and
the fault injection are simulation-specific. Time is the wall clock (Gazebo runs at ~1x).
"""

import asyncio
import contextlib
import json
import logging
import os
import signal
import subprocess
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from a6g_common.clock import SystemClock
from a6g_common.config import A6GConfig, RobotManifest, adapter_config, load_manifest
from a6g_common.geometry import point_in_polygon
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
from a6g_coordinator.runtime import Coordinator
from a6g_interactions import InteractionClient, InteractionStore
from a6g_kb import KnowledgeClient, KnowledgeService, build_kb_server
from a6g_kb.index import HashingEmbedder
from a6g_observability.comms import fresh_log
from a6g_observability.decision_log import DecisionLog
from a6g_robot_agent import RobotAgent, build_robot_agent
from a6g_robot_agent.skills import SkillClient
from a6g_robot_agent.uplink import A2AUplink
from a6g_skill_mcp import SkillService, build_mcp_server
from a6g_skill_mcp.backend import MotionResult, SkillBackend
from a6g_skill_mcp.serve import ros_service
from a6g_transport.messaging import A6GMessage, MessageType, configure_messaging, memory_bus
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.store.memory import InMemoryStore

from a6g_devstack import gz_procs
from a6g_devstack.inproc import InProcessNet
from a6g_devstack.stack import Process

log = logging.getLogger("a6g-gz-stack")

GZ_CONTROL_PORT = 8310
ANOMALY_MARKER_SDF = (
    '<?xml version="1.0"?><sdf version="1.9"><model name="anomaly"><static>true</static>'
    '<link name="l"><collision name="c"><geometry><box><size>0.4 0.4 0.6</size></box>'
    '</geometry></collision><visual name="v"><geometry><box><size>0.4 0.4 0.6</size></box>'
    "</geometry><material><ambient>1 0 0 1</ambient><diffuse>1 0 0 1</diffuse></material>"
    "</visual></link></model></sdf>"
)


# --------------------------------------------------------------------------- links


class LinkEmulator:
    """5G link quality per robot: injected losses + coverage shadows."""

    def __init__(self, link_shadows, degraded_shadows, coverage=None, net=None) -> None:
        self.link_shadows = [list(p) for p in link_shadows]
        self.degraded_shadows = [list(p) for p in degraded_shadows]
        #: Levels from the coverage map (the same as the 2D simulator)
        self.coverage, self.net = coverage, net
        self.trackers: dict[str, Any] = {}
        self.poses: dict[str, dict[str, float]] = {}
        self.lost_until: dict[str, float] = {}

    def inject_loss(self, robot_id: str, seconds: float) -> None:
        self.lost_until[robot_id] = time.monotonic() + seconds

    def quality(self, robot_id: str) -> LinkQuality:
        if time.monotonic() < self.lost_until.get(robot_id, 0.0):
            return LinkQuality.LOST
        pose = self.poses.get(robot_id)
        if pose and "x" in pose:
            xy = (pose["x"], pose["y"])
            if any(point_in_polygon(xy, s) for s in self.link_shadows):
                return LinkQuality.LOST
            if any(point_in_polygon(xy, s) for s in self.degraded_shadows):
                return LinkQuality.DEGRADED
            if self.coverage is not None:
                from datetime import UTC, datetime

                from a6g_common.coverage import LevelTracker

                sample = self.coverage.nearest(*xy)
                if sample is not None:
                    tracker = self.trackers.setdefault(robot_id, LevelTracker(self.net))
                    level = tracker.update(sample, datetime.now(UTC))
                    if level == "lost":
                        return LinkQuality.LOST
                    if level == "low":
                        return LinkQuality.DEGRADED
        return LinkQuality.OK


class LinkAwareBackend(SkillBackend):
    """Adds the emulated link quality to the robot's state (what the modem would report)."""

    def __init__(self, inner: SkillBackend, link: Callable[[], LinkQuality]) -> None:
        super().__init__(inner.manifest)
        self.inner, self.link = inner, link

    def now(self) -> datetime:
        return self.inner.now()

    async def get_state(self) -> RobotState:
        state = await self.inner.get_state()
        return state.model_copy(update={"link_quality": self.link()})

    async def scan_min_range(self) -> float:
        return await self.inner.scan_min_range()

    async def navigate(self, goal: Pose2D, timeout_s: float) -> MotionResult:
        return await self.inner.navigate(goal, timeout_s)

    async def dwell(self, seconds: float, timeout_s: float) -> MotionResult:
        return await self.inner.dwell(seconds, timeout_s)

    async def cancel(self) -> bool:
        return await self.inner.cancel()

    async def stop(self) -> None:
        await self.inner.stop()

    async def capture_image(self, max_kb: int) -> str:
        return await self.inner.capture_image(max_kb)


# --------------------------------------------------------------------------- robots


@dataclass
class GzRobot:
    robot_id: str
    domain: int
    localization: str
    out: Path
    launch: subprocess.Popen | None = None
    adapter: subprocess.Popen | None = None
    socket: str = ""
    service: SkillService | None = None
    agent: RobotAgent | None = None
    process: Process[RobotAgent] | None = None
    mcp_server: uvicorn.Server | None = None
    failed: bool = False
    tasks: list[asyncio.Task] = field(default_factory=list)

    @property
    def alive(self) -> bool:
        return self.agent is not None

    @property
    def env(self) -> dict[str, str]:
        return gz_procs.ros_env(ROS_DOMAIN_ID=str(self.domain))


class _QuietServer(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self):
        yield


class _Uplink:
    """Robot -> in-process coordinator (``coordinator=True``), honouring the emulated link."""

    def __init__(self, stack: "GzStack", robot_id: str) -> None:
        self.stack, self.robot_id = stack, robot_id

    async def send(self, report: Report, nl: dict[str, Any] | None = None) -> bool:
        coord = self.stack.coordinator
        if coord is None or not coord.running:
            return False
        if self.stack.links.quality(self.robot_id) is LinkQuality.LOST:
            return False
        return await A2AUplink(self.stack.net.client(self.robot_id, "coordinator")).send(
            report, nl=nl
        )


class _LinkCheckedUplink:
    """Wraps a network uplink (A2A to ``langgraph dev``): nothing gets through a lost link."""

    def __init__(self, stack: "GzStack", robot_id: str, inner: Any) -> None:
        self.stack, self.robot_id, self.inner = stack, robot_id, inner

    async def send(self, report: Report, nl: dict[str, Any] | None = None) -> bool:
        if self.stack.links.quality(self.robot_id) is LinkQuality.LOST:
            return False
        try:
            return await self.inner.send(report, nl=nl)
        except TypeError:
            return await self.inner.send(report)


class _RobotLink:
    """In-process coordinator -> robot."""

    def __init__(self, stack: "GzStack", robot_id: str) -> None:
        self.stack, self.robot_id = stack, robot_id

    def _link(self) -> A2ARobotLink:
        self.stack.reachable_agent(self.robot_id)  # unreachable / no coverage: nothing sent
        return A2ARobotLink.with_client(self.stack.net.client("coordinator", self.robot_id))

    async def send_envelope(self, envelope: Envelope) -> EnvelopeReply:
        return await self._link().send_envelope(envelope)

    async def get_state(self) -> RobotState:
        return await self._link().get_state()

    async def get_queue(self) -> dict[str, Any]:
        return await self._link().get_queue()


class _Peer:
    def __init__(self, stack: "GzStack", robot_id: str, sender: str) -> None:
        self.stack, self.robot_id, self.sender = stack, robot_id, sender

    def _client(self):
        self.stack.reachable_agent(self.robot_id)
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
    def __init__(self, stack: "GzStack", robot_id: str) -> None:
        self.stack, self.robot_id = stack, robot_id

    def peer_ids(self) -> list[str]:
        return sorted(r for r in self.stack.robots if r != self.robot_id)

    def link(self, robot_id: str) -> _Peer:
        return _Peer(self.stack, robot_id, self.robot_id)


# --------------------------------------------------------------------------- the stack


@dataclass
class GzStack:
    cfg: A6GConfig  # loaded with the Gazebo fleet and profile
    workdir: Path
    robot_ids: list[str] | None = None
    headless: bool = True
    gui: bool = False
    auto_arm: bool = True
    model: BaseChatModel | None = None
    kb_embedder: Any = None  # knowledge-base embedder; None: hashing
    #: Perception triage for every robot (None: stub driven by injected anomalies).
    triage: Any = None
    edge: Any = None
    ids: IdFactory = new_id
    bus: Any = None
    #: Robot -> coordinator uplink per robot (default: in-process coordinator).
    uplink_factory: Callable[[str], Any] | None = None
    a2a_urls: dict[str, str] = field(default_factory=dict)
    serve_mcp: bool = True
    operator_token: str | None = None
    localization: dict[str, str] = field(default_factory=dict)  # per-robot override
    gz_control_port: int = GZ_CONTROL_PORT

    def __post_init__(self) -> None:
        assert self.cfg.profile is not None, "load the config with profile='gazebo'"
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.partition = f"a6g_{uuid.uuid4().hex[:8]}"
        self.env = gz_procs.ros_env(GZ_PARTITION=self.partition)
        self.bus = self.bus if self.bus is not None else memory_bus("devstack")
        self.log = DecisionLog(self.cfg.fleet.edge.decision_log_path)
        configure_messaging(self.cfg.comms, log_path=fresh_log(self.workdir / "comms.jsonl"))
        self.net = InProcessNet(self._robot_agent, lambda: self.coordinator)
        self.kb_service = KnowledgeService(
            self.cfg.campus,
            self.cfg.knowledge,
            embedder=self.kb_embedder or HashingEmbedder(),
        )
        self.kb_server = build_kb_server(self.kb_service)
        self.kb_service.on_status.append(self._publish_status)
        self._pending: set[asyncio.Task[Any]] = set()
        # The InteractionStore, in memory; served over HTTP by the service.
        self.interactions = InteractionStore(self.cfg.interactions.interaction_ttl_min)
        #: The robots' speech output, printed on the console (no on-screen label in Gazebo).
        #: Never logged.
        self.speech: dict[str, Any] = {}
        self.echo_speech = False
        self.store = InMemoryStore()
        campus = self.cfg.campus
        coverage = None
        if self.cfg.network.coverage.source != "shadows":
            from a6g_common.coverage import load_or_build

            coverage = load_or_build(self.cfg)
        self.links = LinkEmulator(
            campus.link_shadows, campus.degraded_shadows, coverage, self.cfg.network
        )
        self.robots: dict[str, GzRobot] = {}
        self.anomalies: dict[str, str] = {}
        self.injections: list[dict[str, Any]] = []
        self.coordinator: Coordinator | None = None
        self._coord_proc: Process[Coordinator] | None = None
        self._world: subprocess.Popen | None = None
        self._gz_control: subprocess.Popen | None = None
        self._poll: asyncio.Task | None = None
        self._http = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{self.gz_control_port}", timeout=10.0
        )

    # ------------------------------------------------------------------ lifecycle

    async def start(self, *, coordinator: bool = True) -> "GzStack":
        world_args = (
            f"headless:={'true' if self.headless else 'false'} gui:={str(self.gui).lower()}"
        )
        self._world = gz_procs.start(
            f"ros2 launch a6g_gz_bringup world.launch.py {world_args}",
            self.workdir / "world.log",
            self.env,
        )
        ids = self.robot_ids or [r.robot_id for r in self.cfg.fleet.robots]
        world = self.cfg.campus.world_name or self.cfg.campus.campus_id
        self._gz_control = gz_procs.start(
            f"ros2 run a6g_gz_bringup gz_control --world {world} "
            f"--port {self.gz_control_port} --track {','.join(ids)}",
            self.workdir / "gz_control.log",
            self.env,
        )
        await asyncio.sleep(8)  # world up before spawning
        self._poll = asyncio.create_task(self._poll_poses(), name="gz-poses")
        if coordinator:
            await self.start_coordinator()
        await asyncio.gather(*(self.start_robot(rid) for rid in ids))
        return self

    def kill_now(self) -> None:
        """Forced exit: SIGKILL every ROS / Gazebo process group right away (no orphans)."""
        procs = [self._gz_control, self._world]
        for r in self.robots.values():
            procs += [r.adapter, r.launch]
        for proc in procs:
            if proc is not None and proc.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)

    async def stop(self) -> None:
        if self._coord_proc is not None:
            await self._coord_proc.stop()
            self._coord_proc = self.coordinator = None
        for robot in self.robots.values():
            await self._stop_robot(robot)
        if self._poll is not None:
            self._poll.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll
        await asyncio.to_thread(gz_procs.stop, self._gz_control, 5.0)
        await asyncio.to_thread(gz_procs.stop, self._world, 15.0)
        await self._http.aclose()
        await self.net.aclose()

    async def __aenter__(self) -> "GzStack":
        return await self.start()

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def _poll_poses(self) -> None:
        while True:
            with contextlib.suppress(httpx.HTTPError, ValueError):
                self.links.poses = (await self._http.get("/poses")).json()["robots"]
            await asyncio.sleep(0.5)

    async def poses(self) -> dict[str, Any]:
        return (await self._http.get("/poses")).json()

    # ------------------------------------------------------------------ robots

    def _robot_agent(self, robot_id: str) -> RobotAgent:
        robot = self.robots.get(robot_id)
        if robot is None or robot.agent is None:
            raise ConnectionError(f"{robot_id} unreachable")
        return robot.agent

    def reachable_agent(self, robot_id: str) -> RobotAgent:
        robot = self.robots.get(robot_id)
        if robot is None or robot.agent is None:
            raise ConnectionError(f"{robot_id} unreachable")
        if self.links.quality(robot_id) is LinkQuality.LOST:
            raise ConnectionError(f"{robot_id} out of coverage")
        return robot.agent

    async def start_robot(
        self, robot_id: str, manifest: RobotManifest | None = None, spawn: Pose2D | None = None
    ) -> RobotAgent:
        cfg = self.cfg
        if manifest is not None:
            cfg.manifests[robot_id] = manifest  # hot-added robots join the config view
        profile = cfg.profile.robots[robot_id]
        loc = self.localization.get(robot_id) or profile.localization.value
        # The gazebo profile uses gnss_ekf for every robot; a profile that asks for AMCL in a
        # scaled world still gets GNSS.
        if loc == "amcl_prior" and cfg.campus.scale != 1.0 and robot_id not in self.localization:
            # Real-size scans against a shrunk map: AMCL's estimate jumps by decimetres and
            # Nav2 dithers at goals. GNSS (the campus datum is unchanged) is unaffected.
            log.warning(
                "%s: campus scale %s: using gnss_ekf instead of amcl_prior",
                robot_id,
                cfg.campus.scale,
            )
            loc = "gnss_ekf"
        out = self.workdir / robot_id
        resume = robot_id in self.robots  # a restart inside this stack; a fresh stack starts clean
        robot = GzRobot(robot_id, profile.ros_domain_id, loc, out)
        self.robots[robot_id] = robot
        args = [f"robot:={robot_id}", f"localization:={loc}", f"out_dir:={out}"]
        if spawn is not None:
            args += [f"x:={spawn.x}", f"y:={spawn.y}", f"yaw:={spawn.yaw}"]
        env = {**robot.env, "GZ_PARTITION": self.partition}
        robot.launch = gz_procs.start(
            "ros2 launch a6g_gz_bringup robot.launch.py " + " ".join(args), out / "robot.log", env
        )
        # The adapter's profile guard needs /clock (the robot's bridge): wait for Nav2 first.
        await gz_procs.wait_nav2_active(env, "/robot" if robot_id.startswith("summit") else "")
        acfg = adapter_config(cfg, robot_id, str(out / "images"), localization=loc)
        base = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "a6g"
        base.mkdir(parents=True, exist_ok=True)
        robot.socket = acfg["socket"] = str(base / f"{robot_id}_{self.partition}.sock")
        (out / "adapter.json").write_text(json.dumps(acfg))
        robot.adapter = gz_procs.start(
            gz_procs.adapter_cmd(out / "adapter.json"), out / "adapter.log", env
        )
        await self._wait_socket(robot)
        service = ros_service(cfg, robot_id, robot.socket, out / "images", self.log)
        service.backend = LinkAwareBackend(service.backend, lambda: self.links.quality(robot_id))
        if self.auto_arm:
            service.arm("gz-stack")
        robot.service = service
        for _ in range(90):  # localized before the agent starts polling its state
            if (await service.get_state()).pose.cov_xy is not None:
                break
            await asyncio.sleep(2)
        if self.serve_mcp:
            self._serve_mcp(robot)
        await self._start_agent(robot, resume)
        log.warning("%s up (domain %s, %s)", robot_id, robot.domain, loc)
        return robot.agent  # type: ignore[return-value]

    async def _wait_socket(self, robot: GzRobot, timeout_s: float = 180.0) -> None:
        end = time.monotonic() + timeout_s
        while not Path(robot.socket).exists():
            if robot.adapter is not None and robot.adapter.poll() is not None:
                raise RuntimeError(f"{robot.robot_id} adapter exited ({robot.adapter.returncode})")
            if time.monotonic() > end:
                raise TimeoutError(f"{robot.robot_id} adapter socket did not appear")
            await asyncio.sleep(0.5)

    def _serve_mcp(self, robot: GzRobot) -> None:
        from urllib.parse import urlsplit

        url = urlsplit(self.cfg.fleet.robot(robot.robot_id).mcp_url)
        app = build_mcp_server(robot.service, self.operator_token).streamable_http_app(
            streamable_http_path=url.path or "/mcp", host=url.hostname
        )
        server = _QuietServer(
            uvicorn.Config(app, host=url.hostname, port=url.port, log_level="warning")
        )
        robot.mcp_server = server
        robot.tasks.append(asyncio.create_task(server.serve(), name=f"mcp-{robot.robot_id}"))

    async def _start_agent(self, robot: GzRobot, resume: bool = True) -> None:
        rid = robot.robot_id

        builds = 0

        async def build(stack: contextlib.AsyncExitStack) -> RobotAgent:
            nonlocal builds
            checkpointer = await stack.enter_async_context(
                AsyncSqliteSaver.from_conn_string(str(robot.out / "agent.sqlite"))
            )
            skills = await stack.enter_async_context(SkillClient(build_mcp_server(robot.service)))
            uplink = (
                _LinkCheckedUplink(self, rid, self.uplink_factory(rid))
                if self.uplink_factory
                else _Uplink(self, rid)
            )
            agent = build_robot_agent(
                self.cfg,
                rid,
                skills=skills,
                checkpointer=checkpointer,
                uplink=uplink,
                a2a_url=self.a2a_urls.get(rid),
                decisions=self.log,
                bus=self.bus,
                clock=SystemClock(),
                ids=self.ids,
                model=self.model,
                triage=self.triage,
                edge=self.edge,
                anomaly_source=self.anomalies.get,
                peers=_Peers(self, rid),
                kb=KnowledgeClient(
                    self.kb_server, agent_id=rid, timeout_s=self.cfg.knowledge.timeout_s
                ),
                interactions=InteractionClient(
                    self.interactions, agent_id=rid, timeout_s=self.cfg.interactions.timeout_s
                ),
            )
            agent.resume = resume or builds > 0  # rebuilt after a crash: resume
            agent.on_speech = self._on_speech
            builds += 1
            await stack.enter_async_context(agent)
            return agent

        robot.process = Process(f"robot-{rid}", build)
        robot.agent = await robot.process.start()

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
        bus = self.bus.as_agent("kb") if hasattr(self.bus, "as_agent") else self.bus
        try:
            task = asyncio.get_running_loop().create_task(
                bus.publish("a6g/kb/status", payload, qos=1)
            )
        except RuntimeError:
            return
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _stop_robot(self, robot: GzRobot) -> None:
        if robot.process is not None:
            await robot.process.stop()
            robot.agent = robot.process = None
        if robot.mcp_server is not None:
            robot.mcp_server.should_exit = True
        await asyncio.gather(*robot.tasks, return_exceptions=True)
        robot.tasks.clear()
        if robot.service is not None:
            with contextlib.suppress(Exception):
                await robot.service.backend.inner.client.close()  # type: ignore[attr-defined]
        await asyncio.to_thread(gz_procs.stop, robot.adapter, 10.0)
        await asyncio.to_thread(gz_procs.stop, robot.launch, 30.0)
        robot.adapter = robot.launch = None

    # ------------------------------------------------------------------ fault injection

    async def fail_robot(self, robot_id: str) -> None:
        """``robot_fail``: the robot dies. Its agent, adapter and Nav2 stop; the model is
        frozen (Gazebo's velocity systems would otherwise keep the last command)."""
        robot = self.robots[robot_id]
        self.injections.append({"kind": "robot_fail", "robot_id": robot_id, "t": time.time()})
        robot.failed = True
        await self._http.post("/freeze", json={"robot_id": robot_id})
        await self._stop_robot(robot)

    def link_loss(self, robot_id: str, seconds: float) -> None:
        self.injections.append(
            {"kind": "link_loss", "robot_id": robot_id, "t": time.time(), "seconds": seconds}
        )
        self.links.inject_loss(robot_id, seconds)

    async def anomaly(self, poi_id: str, label: str) -> None:
        """A visible marker 1.5 m in front of the POI's viewpoint; StubTriage reports the label."""
        import math

        pose = self.cfg.campus.poi(poi_id).pose
        x, y = pose.x + 1.5 * math.cos(pose.yaw), pose.y + 1.5 * math.sin(pose.yaw)
        await self._http.post(
            "/spawn",
            json={"name": f"anomaly_{poi_id}", "sdf": ANOMALY_MARKER_SDF, "x": x, "y": y, "z": 0.3},
        )
        self.anomalies[poi_id] = label
        self.injections.append({"kind": "anomaly", "poi_id": poi_id, "t": time.time()})

    async def add_robot(
        self, manifest_path: Path | str | None = None, robot_id: str | None = None
    ) -> RobotAgent:
        """``add_robot``: spawn at the profile's pose (staging) and start its stack."""
        if manifest_path is None:
            if robot_id is None:
                raise ValueError("give a manifest path or a robot_id")
            manifest_path = self.cfg.config_dir / "robots" / f"{robot_id}.yaml"
        manifest = load_manifest(Path(manifest_path))
        await self._http.post("/track", json={"robot_id": manifest.robot_id})
        return await self.start_robot(manifest.robot_id, manifest)

    # ------------------------------------------------------------------ coordinator

    async def start_coordinator(self) -> Coordinator:
        async def build(stack: contextlib.AsyncExitStack) -> Coordinator:
            checkpointer = await stack.enter_async_context(
                AsyncSqliteSaver.from_conn_string(str(self.workdir / "coordinator.sqlite"))
            )
            deps = CoordinatorDeps(
                cfg=self.cfg,
                links=lambda rid, entry: _RobotLink(self, rid),
                decisions=self.log,
                clock=SystemClock(),
                ids=self.ids,
                model=self.model,
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
            bus = self.bus.as_agent("coordinator") if hasattr(self.bus, "as_agent") else self.bus
            coord = Coordinator(deps, checkpointer, self.store, bus=bus)
            await stack.enter_async_context(coord)
            return coord

        self._coord_proc = Process("coordinator", build)
        self.coordinator = await self._coord_proc.start()
        return self.coordinator

    async def mission(
        self, text: str, mission_id: str = "m1", posture: str = "routine"
    ) -> dict[str, Any]:
        assert self.coordinator is not None
        return await self.coordinator.goal(mission_id, text, posture)

    def weather_event(self) -> dict[str, Any]:
        return {
            "type": "posture",
            "posture": "emergency",
            "text": "weather alert: heavy rain",
            "reason": "weather_alert",
            "exposed_pois": [p.poi_id for p in self.cfg.campus.pois if p.exposed],
        }

    async def weather_alert(self) -> None:
        assert self.coordinator is not None
        self.injections.append({"kind": "weather_alert", "t": time.time()})
        await self.coordinator.broadcast(self.weather_event())

    # ------------------------------------------------------------------ status & metrics

    async def status(self) -> dict[str, Any]:
        snap = await self.poses()
        return {
            "sim_time": snap.get("sim_time"),
            "robots": {
                rid: {
                    **{k: round(v, 3) for k, v in snap["robots"].get(rid, {}).items()},
                    "failed": r.failed,
                    "link": self.links.quality(rid).value,
                    "agent": r.alive,
                    "localization": r.localization,
                }
                for rid, r in self.robots.items()
            },
        }

    def bytes_per_robot(self) -> dict[str, int]:
        out = {}
        for rid, r in self.robots.items():
            if r.agent is not None:
                out[rid] = int(sum(r.agent.deps.budget.sent_bytes.values()))
        return out
