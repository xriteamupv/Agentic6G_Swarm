import os

# Scenario stacks run the simulator faster than the services do (laps along the footpaths
# are long); timing assertions are on simulated time.
os.environ.setdefault("A6G_SIM_REALTIME_FACTOR", "200")

import asyncio
import contextlib
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from a6g_common.config import A6GConfig, load_config
from a6g_common.schemas import (
    Envelope,
    EnvelopeBudgets,
    LinkQuality,
    PoiTarget,
    Pose2D,
    PoseTarget,
    SectorTarget,
    TaskKind,
    TaskSpec,
)
from a6g_observability.decision_log import DecisionLog
from a6g_robot_agent import RobotAgent, build_robot_agent
from a6g_robot_agent.skills import SkillClient
from a6g_robot_agent.uplink import RecordingUplink
from a6g_sim.runner import SimRunner
from a6g_sim.world import SimWorld
from a6g_skill_mcp.server import build_mcp_server
from a6g_skill_mcp.service import SkillService
from a6g_skill_mcp.sim_backend import SimBackend
from a6g_transport.messaging import MessagingBus, memory_bus
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "config"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--model",
        action="store_true",
        help="run tests that call the real (cloud) models of models.yaml",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    gates = {
        "model": ("--model", "calls the cloud models: run with --model"),
    }
    for marker, (option, reason) in gates.items():
        if config.getoption(option):
            continue
        skip = pytest.mark.skip(reason=reason)
        for item in items:
            if item.get_closest_marker(marker) is not None:
                item.add_marker(skip)


@pytest.fixture
def config_dir() -> Path:
    return CONFIG_DIR


@pytest.fixture
def config_copy(tmp_path: Path) -> Path:
    """A writable copy of the repository config tree."""
    dst = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, dst)
    # The campus file references the cached OSM extract relative to itself.
    shutil.copytree(REPO_ROOT / "data" / "osm", tmp_path / "data" / "osm")
    return dst


# --------------------------------------------------------------------------- sim fleet

OPERATOR_TOKEN = "test-operator-token"


@dataclass
class SimFleet:
    cfg: A6GConfig
    world: SimWorld
    log: DecisionLog
    services: dict[str, SkillService]

    def server(self, robot_id: str):
        return build_mcp_server(self.services[robot_id], operator_token=OPERATOR_TOKEN)


@pytest.fixture
def cfg() -> A6GConfig:
    return load_config(CONFIG_DIR, env={})


@pytest.fixture
async def fleet(cfg: A6GConfig, tmp_path: Path):
    """go2_01 at the staging pose and summit_01 1.5 m north of it, both disarmed."""
    world = SimWorld(cfg.campus)
    for manifest in cfg.manifests.values():
        world.add_robot(manifest)
    log = DecisionLog(tmp_path / "decisions.jsonl")
    services = {
        rid: SkillService(
            SimBackend(world, rid, tmp_path / "images"),
            cfg.campus,
            log,
            battery_floor_pct=cfg.allocator.default_battery_floor_pct,
            mission_id="m_test",
        )
        for rid in cfg.manifests
    }
    async with SimRunner(world, dt=0.1):
        yield SimFleet(cfg, world, log, services)


# --------------------------------------------------------------------------- robot agent

WHOLE_CAMPUS = [(-500.0, -500.0), (500.0, -500.0), (500.0, 500.0), (-500.0, 500.0)]


@dataclass
class AgentEnv:
    fleet: SimFleet
    robot_id: str
    uplink: RecordingUplink
    bus: MessagingBus
    db_path: Path
    agent: RobotAgent | None = None
    _host: asyncio.Task | None = None
    _stop: asyncio.Event | None = None

    @property
    def world(self) -> SimWorld:
        return self.fleet.world

    @property
    def robot(self):
        return self.fleet.world.robot(self.robot_id)

    async def start_agent(self, **kwargs) -> RobotAgent:
        """(Re)start the agent "process" on the same local SQLite file.

        The agent's async contexts (MCP session, checkpointer) are entered and exited in
        one dedicated host task, like a real process; anyio cancel scopes require that.
        """
        if self._host is not None:
            await self.stop_agent()
        ready: asyncio.Future[RobotAgent] = asyncio.get_running_loop().create_future()
        stop = asyncio.Event()

        async def host() -> None:
            try:
                async with contextlib.AsyncExitStack() as stack:
                    checkpointer = await stack.enter_async_context(
                        AsyncSqliteSaver.from_conn_string(str(self.db_path))
                    )
                    skills = await stack.enter_async_context(
                        SkillClient(self.fleet.server(self.robot_id))
                    )
                    agent = build_robot_agent(
                        self.fleet.cfg,
                        self.robot_id,
                        skills=skills,
                        checkpointer=checkpointer,
                        uplink=self.uplink,
                        decisions=self.fleet.log,
                        bus=self.bus,
                        clock=self.world.clock,
                        poll_interval_s=0.01,
                        anomaly_source=self.world.anomalies.get,
                        **kwargs,
                    )
                    await stack.enter_async_context(agent)
                    ready.set_result(agent)
                    await stop.wait()
            except BaseException as exc:
                if not ready.done():
                    ready.set_exception(exc)
                raise

        self._stop = stop
        self._host = asyncio.create_task(host(), name=f"{self.robot_id}-process")
        self.agent = await ready
        return self.agent

    async def stop_agent(self) -> None:
        if self._host is not None:
            self._stop.set()
            await self._host
        self._host, self.agent = None, None

    async def state(self, envelope_id: str) -> dict:
        assert self.agent is not None
        return await self.agent.state(envelope_id)


def make_envelope(
    tasks: list[TaskSpec],
    *,
    envelope_id: str = "env_001",
    robot_id: str = "go2_01",
    mission_id: str = "m_test",
    floor: float = 20.0,
    region=None,
    version: int = 1,
    priority: int = 50,
    swappable: bool = True,
    now: datetime | None = None,
) -> Envelope:
    now = now or datetime(2026, 1, 1, tzinfo=UTC)
    return Envelope(
        envelope_id=envelope_id,
        robot_id=robot_id,
        mission_id=mission_id,
        tasks=tasks,
        region=region or WHOLE_CAMPUS,
        valid_from=now,
        valid_until=now + timedelta(hours=8),
        priority=priority,
        budgets=EnvelopeBudgets(battery_floor_pct=floor, uplink_mbps=3.0),
        swappable=swappable,
        version=version,
    )


def survey(sector: str, task_id: str | None = None, **kw) -> TaskSpec:
    return TaskSpec(
        task_id=task_id or f"t_survey_{sector}",
        kind=TaskKind.SURVEY_SECTOR,
        target=SectorTarget(sector_id=sector),
        required_capabilities=["survey"],
        est_duration_s=60,
        **kw,
    )


def inspect_poi(poi: str, task_id: str | None = None, **kw) -> TaskSpec:
    return TaskSpec(
        task_id=task_id or f"t_inspect_{poi}",
        kind=TaskKind.INSPECT_POI,
        target=PoiTarget(poi_id=poi),
        required_capabilities=["inspect"],
        est_duration_s=10,
        **kw,
    )


def return_home(task_id: str = "t_home", **kw) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        kind=TaskKind.RETURN_HOME,
        target=PoseTarget(pose=Pose2D(x=0, y=0)),
        est_duration_s=0,
        **kw,
    )


async def wait_for(predicate, timeout: float = 60.0, what: str = "condition") -> None:
    """Await an (a)sync predicate; the simulator keeps running meanwhile."""
    async with asyncio.timeout(timeout):
        while True:
            value = predicate()
            if asyncio.iscoroutine(value):
                value = await value
            if value:
                return
            await asyncio.sleep(0.005)


@pytest.fixture
async def agent_env(fleet: SimFleet, tmp_path: Path):
    """go2_01 armed, with an agent on a local SQLite checkpointer."""
    fleet.services["go2_01"].arm("tester")
    uplink = RecordingUplink(
        connected=lambda: fleet.world.link_quality("go2_01") is not LinkQuality.LOST
    )
    env = AgentEnv(fleet, "go2_01", uplink, memory_bus(), tmp_path / "go2_01.sqlite")
    await env.start_agent()
    try:
        yield env
    finally:
        await env.stop_agent()


# --------------------------------------------------------------------------- Postgres


@pytest.fixture(scope="session")
def pg_server(tmp_path_factory):
    """A user-space PostgreSQL (pgserver: bundled binaries, no root, no Docker)."""
    pgserver = pytest.importorskip("pgserver")
    server = pgserver.get_server(tmp_path_factory.mktemp("pg"), cleanup_mode="stop")
    yield server
    server.cleanup()


@pytest.fixture
def pg_dsn(pg_server, request) -> str:
    """A fresh database per test."""
    import re
    import uuid

    import psycopg

    name = "t_" + re.sub(r"\W", "_", request.node.name)[:40].lower() + "_" + uuid.uuid4().hex[:6]
    with psycopg.connect(pg_server.get_uri(), autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    return pg_server.get_uri(name)
