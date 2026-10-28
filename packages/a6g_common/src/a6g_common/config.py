"""Configuration models and loader.

All hosts, endpoints, budgets, thresholds and robot parameters live in YAML under
``config/``. Loading is strict: unknown keys, duplicate YAML keys, unresolved environment
variables, inconsistent cross-references and unsafe manifests raise :class:`ConfigError`
naming the file and the offending field.

String values may reference environment variables as ``${VAR}`` or ``${VAR:-default}``.
Secrets are never stored in YAML; reference them by environment-variable *name*
(e.g. ``api_key_env``).
"""

import math
import os
import re
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, TypeVar
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    Field,
    PrivateAttr,
    ValidationError,
    field_validator,
    model_validator,
)

from a6g_common.geometry import point_in_polygon
from a6g_common.netconfig import NetworkConfig
from a6g_common.routegraph import DEFAULT_PROFILES, RouteGraph, RoutingProfile
from a6g_common.schemas import (
    A6GModel,
    Identifier,
    Platform,
    Polygon,
    Pose2D,
    Posture,
    RobotId,
    SkillName,
    SkillOutcome,
    TaskKind,
    Versioned,
)

DEFAULT_CONFIG_DIR_ENV = "A6G_CONFIG_DIR"

#: Topics/services that no configuration may ever reference (no low-level motor control).
FORBIDDEN_ROS_NAMES = frozenset({"/lowcmd", "/api/bashrunner/request"})
#: The only Sport API call the Go2 adapter may make: StopMove.
GO2_SPORT_STOP_MOVE_API_ID = 1003
#: Hard cap on waypoints per follow_route call.
MAX_ROUTE_WAYPOINTS = 20


class ConfigError(Exception):
    """Raised when a configuration file is missing, malformed or inconsistent."""

    def __init__(self, source: str | Path, problems: list[str]) -> None:
        self.source = str(source)
        self.problems = problems
        lines = "\n".join(f"  - {p}" for p in problems)
        super().__init__(f"invalid configuration in {self.source}:\n{lines}")


# --------------------------------------------------------------------------- vocabularies


class Capability(StrEnum):
    """Capability vocabulary. Extend here (not in manifests) so typos fail loudly."""

    SURVEY = "survey"
    INSPECT = "inspect"
    HOLD_POST = "hold_post"
    ESCORT = "escort"
    VOICE = "voice"
    ROUGH_TERRAIN = "rough_terrain"
    PAYLOAD = "payload"
    RTK_GNSS = "rtk_gnss"


class Motion(StrEnum):
    REVERSE = "reverse"
    STRAFE = "strafe"
    TURN_IN_PLACE = "turn_in_place"


class NavMode(StrEnum):
    ACTION = "action"
    GOAL_POSE = "goal_pose"


class AgentHost(StrEnum):
    ONBOARD = "onboard"
    EDGE_PROXY = "edge_proxy"


class LinkLossPolicy(StrEnum):
    HOLD_THEN_RECOVER = "hold_then_recover"  # default
    FINISH_CURRENT_THEN_HOLD = "finish_current_then_hold"
    RETURN_HOME = "return_home"


class ModelRole(StrEnum):
    COORDINATOR_LLM = "coordinator_llm"
    EDGE_VLM = "edge_vlm"
    ONBOARD_VLM = "onboard_vlm"
    EMBEDDER = "embedder"  # indexing and search of the knowledge base


# --------------------------------------------------------------------------- robot manifest


class Footprint(A6GModel):
    length_m: float = Field(gt=0.0, le=5.0)
    width_m: float = Field(gt=0.0, le=5.0)


class StopMethod(StrEnum):
    SPORT_STOP_MOVE = "sport_stop_move"  # Go2: Sport API StopMove (api_id 1003)
    ZERO_TWIST = "zero_twist"  # Summit: zero Twist on the web_teleop twist_mux input


class StopConfig(A6GModel):
    method: StopMethod
    topic: str
    api_id: int | None = None
    duration_s: float | None = Field(None, gt=0.0)

    @model_validator(mode="after")
    def _check(self) -> "StopConfig":
        if self.method is StopMethod.SPORT_STOP_MOVE and self.api_id != GO2_SPORT_STOP_MOVE_API_ID:
            raise ValueError(
                f"sport_stop_move may only use api_id {GO2_SPORT_STOP_MOVE_API_ID} (StopMove)"
            )
        if self.method is StopMethod.ZERO_TWIST and self.duration_s is None:
            raise ValueError("zero_twist stop requires duration_s")
        return self


class GoalTolerance(A6GModel):
    xy_m: float = Field(gt=0.0)
    yaw_rad: float = Field(gt=0.0)


class ReverseLimits(A6GModel):
    """Short, slow reversing (Nav2 MPPI on the Summit: costmap-checked, forward preferred).

    The Summit's rear is a LiDAR blind spot (GPS mast): it may only back into space its
    costmap already knows, a few tens of cm at walking-aid speed.
    """

    max_speed_mps: float = Field(gt=0.0, le=0.2)
    max_distance_m: float = Field(gt=0.0, le=1.0, description="longest single reverse move")


class RosConfig(A6GModel):
    """Platform ROS names, consumed only by the on-robot ros_adapter."""

    distro: str = Field(pattern=r"^(foxy|humble|jazzy)$")
    namespace: str = ""
    map_frame: str
    base_frame: str
    pose_topic: str
    scan_topic: str
    cloud_topic: str | None = Field(None, description="read on-board only, never streamed")
    nav_action: str
    goal_pose_topic: str | None = None
    lifecycle_service: str | None = None
    stop: StopConfig

    @model_validator(mode="after")
    def _no_forbidden_names(self) -> "RosConfig":
        names = [
            self.pose_topic,
            self.scan_topic,
            self.cloud_topic,
            self.nav_action,
            self.goal_pose_topic,
            self.lifecycle_service,
            self.stop.topic,
        ]
        bad = sorted({n for n in names if n and n.rstrip("/") in FORBIDDEN_ROS_NAMES})
        if bad:
            raise ValueError(f"forbidden ROS names referenced: {bad}")
        return self


class RobotManifest(Versioned):
    """Capability manifest. The allocator and agents read only this."""

    robot_id: RobotId
    platform: Platform
    capabilities: list[Capability] = Field(min_length=1)
    max_speed_mps: float = Field(gt=0.0, le=3.0)
    footprint: Footprint
    turn_in_place_s_per_rad: float = Field(ge=0.0)
    nav_mode: NavMode = NavMode.ACTION
    clearance_m: float = Field(gt=0.0)
    localization_cov_max: float = Field(gt=0.0)
    battery_reliable: bool
    battery_drain_pct_per_m: float = Field(0.05, ge=0.0, description="planning/sim estimate")
    #: May it use stairs edges of the route graph? Assumed for the Go2, to be confirmed;
    #: the Summit never does.
    stairs: bool = False
    error_code_map: dict[int, SkillOutcome] = Field(default_factory=dict)
    forbidden_motions: list[Motion] = Field(default_factory=list)
    reverse_limits: ReverseLimits | None = None
    camera_topic: str
    skill_timeouts: dict[SkillName, float]
    agent_host: AgentHost
    goal_tolerance: GoalTolerance | None = None
    ros: RosConfig

    @model_validator(mode="after")
    def _check(self) -> "RobotManifest":
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("capabilities contain duplicates")
        if self.platform is Platform.SUMMIT and self.stairs:
            raise ValueError("the Summit never uses stairs (stairs: false)")
        missing = sorted(set(SkillName) - set(self.skill_timeouts))
        if missing:
            raise ValueError(f"skill_timeouts missing entries for: {missing}")
        bad = sorted(k for k, v in self.skill_timeouts.items() if v <= 0)
        if bad:
            raise ValueError(f"skill_timeouts must be > 0 s: {bad}")
        if (
            self.platform is Platform.SUMMIT
            and Motion.REVERSE not in self.forbidden_motions
            and self.reverse_limits is None
        ):
            raise ValueError(
                "Summit manifests must either forbid 'reverse' or set reverse_limits "
                "(rear blind spot: short, slow reverses only)"
            )
        if self.camera_topic.rstrip("/") in FORBIDDEN_ROS_NAMES:
            raise ValueError(f"forbidden ROS name as camera_topic: {self.camera_topic}")
        return self

    def has_capabilities(self, required: list[str]) -> bool:
        return set(required) <= {c.value for c in self.capabilities}


# --------------------------------------------------------------------------- fleet


class BandwidthBudget(A6GModel):
    sustained_mbps: float = Field(3.0, gt=0.0)  # assumed; to be set from measured 5G uplink
    burst_mbps: float = Field(8.0, gt=0.0)

    @model_validator(mode="after")
    def _check(self) -> "BandwidthBudget":
        if self.burst_mbps < self.sustained_mbps:
            raise ValueError("burst_mbps must be >= sustained_mbps")
        return self


class MqttConfig(A6GModel):
    host: str
    port: int = Field(1883, gt=0, lt=65536)
    keepalive_s: int = Field(30, gt=0)


class EdgeConfig(A6GModel):
    host: str  # edge hostname; its placement relative to the UPF is assumed
    mqtt: MqttConfig
    postgres_dsn_env: str = Field("A6G_PG_DSN", description="env var holding the DSN")
    coordinator_a2a_url: str
    otlp_endpoint: str | None = None
    decision_log_path: str = "var/decisions.jsonl"


class AgentConfig(A6GModel):
    """Robot-agent tunables."""

    #: Robots share out work by contract-net when the coordinator is unreachable. Off in
    #: the baselines B0-B2 (``FleetDefaults.baseline``).
    contract_net: bool = True
    #: An idle robot in a mission asks the coordinator for work this often (simulated s),
    #: and never more often than every ``work_request_min_wall_s`` real seconds (a fast
    #: simulator must not flood the coordinator).
    work_request_interval_s: float = Field(30.0, gt=0.0)
    #: Unanswered asks double the wait up to this factor (a small cap: a robot idle at the
    #: dock must not wait long to ask again).
    work_request_max_backoff: int = Field(4, ge=1)
    #: Recharging early, between tasks, so that robots do not all run low together:
    #: ``off``; ``rule`` (charge below ``early_recharge_pct`` when no peer is
    #: charging); ``model`` (the robot's model decides, within what code allows; the rule
    #: is its fallback).
    recharge_advisor: Literal["off", "rule", "model"] = "off"
    early_recharge_pct: float = Field(50.0, ge=0.0, le=100.0)
    work_request_min_wall_s: float = Field(10.0, ge=0.0)

    link_loss_hold_s: float = Field(
        30.0, ge=0.0, description="hold_then_recover: wait this long before seeking coverage"
    )
    #: Idle this long (simulated s) away from the staging area: drive back to it, and wait
    #: there for work. None: stay where the last task ended.
    idle_return_s: float | None = Field(30.0, ge=0.0)
    inspect_dwell_s: float = Field(10.0, ge=0.0)
    triage_confidence_threshold: float = Field(0.6, ge=0.0, le=1.0)
    state_poll_interval_s: float = Field(1.0, gt=0.0, description="get_state / MQTT state period")
    max_queued_reports: int = Field(1000, ge=1)
    llm_justifications: bool = True
    peer_timeout_s: float = Field(10.0, gt=0.0, description="peer declared dead after silence")
    #: The robot's model proposes (deliberation, replanning, negotiation, conversation,
    #: reporting) and code validates. Off: the deterministic logic only.
    cognition: bool = True
    model_timeouts: "ModelTimeouts" = Field(default_factory=lambda: ModelTimeouts())
    nl_max_tokens: int = Field(60, ge=1, description="cap of a report's nl.summary (words)")
    #: Report kinds that get a model-written nl.summary (others get a template one).
    nl_summary_kinds: list[str] = Field(default_factory=lambda: ["envelope_status", "observation"])
    max_hold_s: float = Field(300.0, gt=0.0, description="longest hold a replan may propose")


class ModelTimeouts(A6GModel):
    """Per-function model deadlines; past it the code decides."""

    triage: float = Field(10.0, gt=0.0)
    deliberation: float = Field(8.0, gt=0.0)
    replanning: float = Field(8.0, gt=0.0)
    conversation: float = Field(15.0, gt=0.0)
    negotiation: float = Field(1.0, gt=0.0, description="also capped by the CFP deadline")
    reporting: float = Field(5.0, gt=0.0)


class CoordinatorConfig(A6GModel):
    """Swarm coordinator tunables."""

    robot_timeout_s: float = Field(10.0, gt=0.0, description="robot failed after state silence")
    #: A robot silent longer than its running task's expected duration + the link-loss hold
    #: + this margin is presumed stuck: all its work, including the running task, goes to the
    #: other robots; it keeps seeking coverage on its own. None: it keeps its
    #: running task until it is back.
    lost_release_s: float | None = Field(120.0, gt=0.0)
    #: How long the coordinator waits for a robot's envelope reply: a robot model deliberates
    #: before answering (up to model_timeouts.deliberation).
    envelope_timeout_s: float = Field(15.0, gt=0.0)
    heartbeat_period_s: float = Field(1.0, gt=0.0)
    mission_horizon_s: float = Field(4 * 3600.0, gt=0.0, description="envelope validity")
    escalation_priority: int = Field(70, ge=0, le=100, description="unallocatable -> operator")
    max_allocation_rounds: int = Field(3, ge=1)
    #: How each posture's goals are decomposed (a fast path for the emergency reaction time):
    #: ``model``; ``template`` (code only); ``template_then_model``: the rule-based plan is
    #: allocated, reconciled and published at once, then the model's decomposition runs and
    #: can only add or adjust tasks through the normal path.
    decompose_mode: dict[str, Literal["model", "template", "template_then_model"]] = Field(
        default_factory=lambda: {
            "routine": "model",
            "interactive": "model",
            "emergency": "template_then_model",
        }
    )
    #: How orders are understood. ``rules_first``: rule-based readings (counted
    #: orders, keyword planners, place re-targeting) run before or over the model.
    #: ``model_first``: the model plans every order in a richer plan format (robots per
    #: task, groups, exclusions, order, ending and reassigning work); code validates it
    #: and the rules are only the fallback. Allocation always stays in code.
    understanding: Literal["rules_first", "model_first"] = "rules_first"
    #: model_first: after allocation the model checks who-does-what against the order and
    #: may propose reassignments, which code validates and the allocator applies.
    plan_check: bool = True
    #: Longest a careful plan (and, separately, its check) may think, wall clock: past it
    #: the quick reading stands and the operator is told.
    careful_deadline_s: float = Field(30.0, gt=0.0)
    #: When a robot behaves unexpectedly (refuses all its work twice in a
    #: row; a task every robot refuses), the model analyses the swarm and may propose a fix
    #: for the operator to approve. Once per robot / task within the cooldown (clock seconds).
    anomaly_check: bool = True
    anomaly_cooldown_s: float = Field(600.0, ge=0.0)
    #: Reconciliation of flows before publication: fix-up passes at most.
    reconcile_max_iter: int = Field(3, ge=1)
    assistant_id: str = Field("coordinator", description="graph id in langgraph.json")
    narration: str = Field(
        "template",
        pattern="^(template|llm)$",
        description="operator messages: deterministic templates, or LLM-rephrased (facts checked)",
    )
    missions: str = Field(
        "exclusive",
        pattern="^(exclusive|shared)$",
        description=(
            "exclusive: a conversation that starts (or revives) a mission ends the other "
            "active missions and takes over their robots; shared: they coexist (warning only)"
        ),
    )


#: Comparison configurations: ``none`` is the proposed system (P).
Baseline = Literal["none", "independent", "central_memory", "central_twin"]


class FleetDefaults(A6GModel):
    #: B0 ``independent``: one-off allocation, no reallocation, no contract-net; B1
    #: ``central_memory``: coordinator state in memory (lost on a restart), no
    #: contract-net; B2 ``central_twin``: checkpointed coordinator, no contract-net.
    baseline: Baseline = "none"
    budget: BandwidthBudget = Field(default_factory=BandwidthBudget)
    coordinator_timeout_s: float = Field(10.0, gt=0.0)
    a2a_failures_for_loss: int = Field(2, ge=1)
    link_loss_policy: LinkLossPolicy = LinkLossPolicy.HOLD_THEN_RECOVER
    cfp_deadline_ms: int = Field(1500, gt=0)
    state_publish_hz: float = Field(1.0, gt=0.0)
    agent: AgentConfig = Field(default_factory=AgentConfig)

    @model_validator(mode="after")
    def _baseline_has_no_contract_net(self) -> "FleetDefaults":
        if self.baseline != "none" and self.agent.contract_net:
            self.agent = self.agent.model_copy(update={"contract_net": False})
        return self

    coordinator: CoordinatorConfig = Field(default_factory=CoordinatorConfig)


class FleetRobot(A6GModel):
    robot_id: RobotId
    manifest: str = Field(description="path relative to the config dir")
    a2a_url: str
    mcp_url: str
    adapter_socket: str | None = Field(None, description="default /run/a6g/<robot_id>.sock")
    peers: list[RobotId] = Field(default_factory=list)
    budget: BandwidthBudget | None = Field(None, description="overrides defaults.budget")
    #: The simulator models this robot's battery exactly: its level is trusted even when the
    #: manifest says the real robot's estimator is not (``battery_reliable: false``).
    simulated_battery: bool = False

    @property
    def socket_path(self) -> str:
        return self.adapter_socket or f"/run/a6g/{self.robot_id}.sock"

    @property
    def operator_url(self) -> str:
        """Base URL of the skill server's operator channel (same host/port as MCP)."""
        url = urlsplit(self.mcp_url)
        return f"{url.scheme}://{url.netloc}"


class FleetConfig(Versioned):
    fleet_id: Identifier
    campus: str = Field(description="campus file, relative to the config dir")
    edge: EdgeConfig
    defaults: FleetDefaults = Field(default_factory=FleetDefaults)
    robots: list[FleetRobot] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> "FleetConfig":
        ids = [r.robot_id for r in self.robots]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate robot_id(s): {dupes}")
        known = set(ids)
        for robot in self.robots:
            if robot.robot_id in robot.peers:
                raise ValueError(f"{robot.robot_id} lists itself as a peer")
            unknown = sorted(set(robot.peers) - known)
            if unknown:
                raise ValueError(f"{robot.robot_id} has unknown peers: {unknown}")
        return self

    def robot(self, robot_id: str) -> FleetRobot:
        for robot in self.robots:
            if robot.robot_id == robot_id:
                return robot
        raise KeyError(robot_id)

    def budget_for(self, robot_id: str) -> BandwidthBudget:
        """Per-robot budget; hot-added robots (not in fleet.yaml) get the fleet default."""
        try:
            return self.robot(robot_id).budget or self.defaults.budget
        except KeyError:
            return self.defaults.budget


# --------------------------------------------------------------------------- models


class ModelEndpoint(A6GModel):
    """A chat model, built with LangChain's ``init_chat_model`` (as in the LangChain notebooks).

    ``model`` is a LangChain model string: ``gpt-5-nano`` (provider inferred) or
    ``provider:model`` (e.g. ``anthropic:claude-...``). Keys come from the environment / ``.env``
    (``OPENAI_API_KEY``...). Set ``base_url`` only for a self-hosted OpenAI-compatible server
    (llama.cpp, Ollama, vLLM), e.g. the on-board VLM on the Go2 dock.
    """

    model: str = Field(min_length=1)
    provider: str | None = Field(None, description="override init_chat_model's inference")
    base_url: str | None = Field(None, description="self-hosted OpenAI-compatible server")
    api_key_env: str | None = Field(
        None, description="env var holding the key; default: the provider's own (OPENAI_API_KEY...)"
    )
    temperature: float | None = Field(
        None, ge=0.0, le=2.0, description="None = provider default (GPT-5 models accept only that)"
    )
    timeout_s: float = Field(30.0, gt=0.0)
    max_retries: int = Field(1, ge=0)
    supports_vision: bool = False
    reasoning_effort: str | None = Field(
        None, pattern="^(minimal|low|medium|high)$", description="reasoning models only"
    )
    reasoning_summary: bool = Field(
        False, description="stream the model's reasoning summary (OpenAI Responses API)"
    )

    @field_validator("base_url", "provider", "api_key_env", "reasoning_effort", mode="before")
    @classmethod
    def _empty_is_none(cls, value: Any) -> Any:
        return None if isinstance(value, str) and not value.strip() else value


class ModelsConfig(Versioned):
    roles: dict[ModelRole, ModelEndpoint]

    @model_validator(mode="after")
    def _check(self) -> "ModelsConfig":
        missing = sorted(set(ModelRole) - set(self.roles))
        if missing:
            raise ValueError(f"models.yaml missing roles: {missing}")
        for role in (ModelRole.EDGE_VLM, ModelRole.ONBOARD_VLM):
            if not self.roles[role].supports_vision:
                raise ValueError(f"role {role.value} must point at a vision model")
        return self


# --------------------------------------------------------------------------- allocator


class CostWeights(A6GModel):
    travel_time: float = Field(1.0, ge=0.0)
    battery: float = Field(0.5, ge=0.0)
    commitment: float = Field(0.3, ge=0.0)
    priority: float = Field(0.2, ge=0.0)


class AllocatorConfig(Versioned):
    weights: CostWeights = Field(default_factory=CostWeights)
    #: Travel over the campus route graph when the campus has one;
    #: false, or no graph: straight lines.
    route_costs: bool = True
    battery_margin_pct: float = Field(5.0, ge=0.0, le=100.0)
    default_battery_floor_pct: float = Field(20.0, ge=0.0, le=100.0)
    unknown_battery_pct: float = Field(
        50.0, ge=0.0, le=100.0, description="assumed level when battery_reliable is false"
    )
    #: Low battery: at default_battery_floor_pct + battery_margin_pct a robot releases its
    #: tasks, goes to the staging area and charges; it is back at work at this level.
    recharge_resume_pct: float = Field(90.0, gt=0.0, le=100.0)
    #: An idle robot below this level also goes to charge: it is too low to be given a
    #: patrol lap (which must end above floor + margin), and idle robots barely drain, so
    #: it would never reach the low-battery trigger. Above it, an idle robot stays put.
    recharge_idle_pct: float = Field(40.0, ge=0.0, le=100.0)
    #: ...once it has waited this long (wall clock) for an order: a robot that has just
    #: finished a lap gets its next one from the coordinator instead of refusing it on its
    #: way to the charger.
    recharge_idle_grace_s: float = Field(10.0, ge=0.0)
    #: Released tasks of at least this priority (interactive, emergency) go to another robot;
    #: lower ones (routine patrols) wait and resume on the same robot once it is charged.
    recharge_reassign_min_priority: int = Field(60, ge=0, le=100)
    posture_priority: list[Posture] = Field(
        default_factory=lambda: [Posture.EMERGENCY, Posture.INTERACTIVE, Posture.ROUTINE]
    )
    non_preemptible_kinds: list[TaskKind] = Field(default_factory=lambda: [TaskKind.ESCORT])
    preempt_on: list[Posture] = Field(
        default_factory=lambda: [Posture.EMERGENCY],
        description="postures whose arrival preempts lower-posture work (others only outrank it)",
    )
    max_rounds: int = Field(10, ge=1)

    @model_validator(mode="after")
    def _check(self) -> "AllocatorConfig":
        if sorted(self.posture_priority) != sorted(Posture):
            raise ValueError("posture_priority must list each posture exactly once")
        return self


# --------------------------------------------------------------------------- campus


class MapRef(A6GModel):
    map_version: str
    tile_uri: str
    frame: str = "campus_map"


class Sector(A6GModel):
    sector_id: Identifier
    name: str = ""
    aliases: list[str] = Field(default_factory=list)  # campus v3
    polygon: Polygon
    #: Any length: the robot walks it in ``follow_route`` legs of MAX_ROUTE_WAYPOINTS.
    survey_waypoints: list[Pose2D] = Field(default_factory=list)
    #: Footpath zones (campus ``patrol_zones.mode: footpaths``): the path edges it covers.
    path_edges: list[str] = Field(default_factory=list)


class Poi(A6GModel):
    poi_id: Identifier
    name: str = ""
    sector_id: Identifier | None = None
    pose: Pose2D
    exposed: bool = Field(False, description="exposed to weather; target of weather_alert")
    tags: list[str] = Field(default_factory=list)
    # Campus v3
    aliases: list[str] = Field(default_factory=list)
    building: str | None = None
    node_id: str | None = Field(None, description="its node in the route graph")
    flood: dict[str, Any] | None = Field(None, description="flood twin parameters (A3.6.1)")


class Document(A6GModel):
    """A campus document for retrieval."""

    id: Identifier
    kind: str
    text: str
    lang: str = "en"
    applies_to: list[str] = Field(default_factory=list)
    valid_from: str | None = None
    valid_until: str | None = None
    source: str = "operator"


class LinkRecoveryPoint(A6GModel):
    """A reachable spot with known good 5G coverage."""

    point_id: Identifier
    pose: Pose2D
    note: str = ""
    name: str = ""
    aliases: list[str] = Field(default_factory=list)  # campus v3

    @property
    def label(self) -> str:
        """Its name on the map ("lrp_east_gate" -> "East gate")."""
        return self.name or self.point_id.removeprefix("lrp_").replace("_", " ").capitalize()


class CampusConfig(Versioned):
    campus_id: Identifier
    frame: str = "campus_map"
    map: MapRef
    staging: Pose2D
    staging_area: Polygon | None = None
    route_graph_uri: str | None = None
    #: Robots move only on the footpaths (campus ``patrol_zones``).
    footpaths_only: bool = False
    #: Half-width of the walkable corridor around a footpath (simulator guard, Nav2 keepout).
    walk_corridor_m: float = Field(2.0, gt=0.0)
    #: Robots in transit prefer the patrol paths: other footways cost this many times their
    #: length (campus ``patrol_zones.transit_off_zone_factor``).
    transit_off_zone_factor: float = Field(3.0, ge=1.0)
    sectors: list[Sector] = Field(min_length=1)
    pois: list[Poi] = Field(default_factory=list)
    link_shadows: list[Polygon] = Field(
        default_factory=list, description="simulator: polygons where link_quality = lost"
    )
    link_recovery_points: list[LinkRecoveryPoint] = Field(default_factory=list)
    #: gNB sites (campus_map x, y; height above ground) and calibration loops
    network_sites: list[dict[str, Any]] = Field(default_factory=list)
    calibration_loops: list[dict[str, Any]] = Field(default_factory=list)
    #: trees that attenuate the radio: {area_id, name, polygon, canopy_height_m}
    vegetation_areas: list[dict[str, Any]] = Field(default_factory=list)
    #: heights (real m) aligned with ``obstacles``: buildings; None for trees
    obstacle_heights: list[float | None] = Field(default_factory=list)
    #: Campus v3 knowledge base: outdoor route graph, documents, profiles.
    route_graph: RouteGraph | None = None
    route_graph_notes: list[str] = Field(default_factory=list)
    documents: list[Document] = Field(default_factory=list)
    people_routing_profiles: dict[str, RoutingProfile] = Field(default_factory=dict)
    #: Campus v2: uniform geometry scale and the generated world's name.
    scale: float = 1.0
    world_name: str | None = None
    # Derived from campus v2 (WGS84 + OSM extract); empty for legacy v1 files.
    degraded_shadows: list[Polygon] = Field(default_factory=list)
    pilot_area: Polygon | None = None
    obstacles: list[Polygon] = Field(
        default_factory=list, description="building footprints / trees in campus_map (2D sim)"
    )
    datum: dict[str, float] | None = None

    @model_validator(mode="after")
    def _check(self) -> "CampusConfig":
        problems: list[str] = []
        sector_ids = [s.sector_id for s in self.sectors]
        poi_ids = [p.poi_id for p in self.pois]
        for label, ids in (("sector_id", sector_ids), ("poi_id", poi_ids)):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            if dupes:
                problems.append(f"duplicate {label}(s): {dupes}")
        sectors = {s.sector_id: s for s in self.sectors}
        for sector in self.sectors:
            for i, wp in enumerate(sector.survey_waypoints):
                if not point_in_polygon((wp.x, wp.y), sector.polygon):
                    problems.append(f"sector {sector.sector_id} waypoint {i} is outside it")
        for poi in self.pois:
            if poi.sector_id is None:
                continue
            sector = sectors.get(poi.sector_id)
            if sector is None:
                problems.append(f"poi {poi.poi_id} references unknown sector {poi.sector_id}")
            elif not point_in_polygon((poi.pose.x, poi.pose.y), sector.polygon):
                problems.append(f"poi {poi.poi_id} lies outside sector {poi.sector_id}")
        for point in self.link_recovery_points:
            if any(point_in_polygon((point.pose.x, point.pose.y), sh) for sh in self.link_shadows):
                problems.append(
                    f"link recovery point {point.point_id} lies inside a coverage shadow"
                )

        def blocked(x: float, y: float) -> bool:
            return any(point_in_polygon((x, y), o) for o in self.obstacles)

        named = [(f"poi {p.poi_id}", p.pose) for p in self.pois]
        named += [("staging", self.staging)]
        named += [(f"link recovery point {p.point_id}", p.pose) for p in self.link_recovery_points]
        named += [
            (f"sector {s.sector_id} waypoint {i}", w)
            for s in self.sectors
            for i, w in enumerate(s.survey_waypoints)
        ]
        for label, pose in named:
            if blocked(pose.x, pose.y):
                problems.append(f"{label} lies inside a building/obstacle (unreachable)")
        for i, shadow in enumerate(self.link_shadows):
            outside = [
                p
                for p in self.link_recovery_points
                if not point_in_polygon((p.pose.x, p.pose.y), shadow)
                and not blocked(p.pose.x, p.pose.y)
                and (
                    self.pilot_area is None
                    or point_in_polygon((p.pose.x, p.pose.y), self.pilot_area)
                )
            ]
            if not outside:
                problems.append(
                    f"coverage shadow {i} has no reachable link recovery point outside it"
                )
        if self.staging_area and not point_in_polygon(
            (self.staging.x, self.staging.y), self.staging_area
        ):
            problems.append("staging pose lies outside staging_area")
        if self.route_graph is not None:
            problems += self._knowledge_problems()
        if problems:
            raise ValueError("; ".join(problems))
        return self

    #: coverage level at a point, from the configured coverage map (set by ``load_config``)
    _level_at: Any = PrivateAttr(default=None)

    _robot_walker: Any = PrivateAttr(default=None)
    _edge_factors: Any = PrivateAttr(default=None)

    def robot_edge_factors(self) -> dict[str, float]:
        """Cost factors for the robots' walks: the patrol paths and the connectors to POIs at
        their length, every other footway ``transit_off_zone_factor`` times it (so recovery
        drives keep to the patrol corridors instead of façade footways)."""
        if self._edge_factors is None:
            graph = self.route_graph
            patrolled = {e for s in self.sectors for e in s.path_edges}
            self._edge_factors = (
                {}
                if graph is None or self.transit_off_zone_factor == 1.0
                else {
                    e.id: self.transit_off_zone_factor
                    for e in graph.edges
                    if e.id not in patrolled and e.source != "connector"
                }
            )
        return self._edge_factors

    def robot_walker(self) -> Any:
        """The robots' footpath walker (patrol paths preferred), built once."""
        if self._robot_walker is None and self.route_graph is not None:
            from a6g_common.footpaths import Walker

            self._robot_walker = Walker(self.route_graph, factors=self.robot_edge_factors())
        return self._robot_walker

    def link_level_at(self, x: float, y: float) -> str | None:
        """The coverage level (good | medium | low | lost) at a point; with no coverage map,
        ``lost`` inside a link shadow and otherwise unknown (None)."""
        if self._level_at is not None:
            return self._level_at(x, y)
        if any(point_in_polygon((x, y), sh) for sh in self.link_shadows):
            return "lost"
        return None

    def places(self) -> dict[str, tuple[str, Pose2D]]:
        """Named spots on the map other than sectors and POIs (link recovery points, e.g.
        the East gate): place_id -> (name, pose). Robots can be sent or escort there."""
        return {p.point_id: (p.label, p.pose) for p in self.link_recovery_points}

    def _knowledge_problems(self) -> list[str]:
        """Campus v3 checks (``a6g config validate``)."""
        problems: list[str] = []
        graph = self.route_graph
        assert graph is not None
        comps = graph.components()
        if len(comps) > 1:
            pieces = ", ".join(f"{len(c)} nodes from {c[0]}" for c in comps[1:4])
            problems.append(f"route graph is not connected: {len(comps)} pieces ({pieces} ...)")
        profiles = {**DEFAULT_PROFILES, **self.people_routing_profiles}
        step_free = profiles["step_free"]
        shelters = [p for p in self.pois if "shelter" in p.tags]
        node_ids = {n.id for n in graph.nodes}
        for poi in self.pois:
            needs = "escort_dest" in poi.tags or poi.exposed
            if not needs or "shelter" in poi.tags:
                continue
            if not shelters:
                problems.append("no shelter POI: escort destinations and exposed POIs need one")
                break
            reachable = any(
                poi.node_id in node_ids
                and s.node_id in node_ids
                and graph.shortest_path(poi.node_id, s.node_id, step_free.allows) is not None
                for s in shelters
            )
            if not reachable:
                problems.append(f"poi {poi.poi_id}: no shelter reachable on a step-free route")
        for poi in self.pois:
            if poi.exposed and not poi.flood:
                problems.append(f"exposed poi {poi.poi_id} has no flood parameters")
        if self.footpaths_only:
            for sector in self.sectors:
                if not sector.path_edges:
                    problems.append(f"zone {sector.sector_id} has no paths: move its seed")
        if self.footpaths_only:  # every fixed point on a footpath
            from a6g_common.footpaths import Walker

            walker = Walker(graph)
            points = [(f"poi {p.poi_id}", p.pose) for p in self.pois]
            points += [("staging", self.staging)]
            points += [(f"recovery point {r.point_id}", r.pose) for r in self.link_recovery_points]
            for label, pose in points:
                d = walker.distance((pose.x, pose.y))
                if d > self.walk_corridor_m:
                    problems.append(
                        f"{label} is {d:.1f} m from the nearest footpath (footpaths only: "
                        f"at most walk_corridor_m {self.walk_corridor_m:g} m)"
                    )
        known = graph.ids() | {p.poi_id for p in self.pois} | {s.sector_id for s in self.sectors}
        for doc in self.documents:
            unknown = sorted(set(doc.applies_to) - known)
            if unknown:
                problems.append(f"document {doc.id} applies to unknown id(s) {unknown}")
        return problems

    def sector(self, sector_id: str) -> Sector:
        return next(s for s in self.sectors if s.sector_id == sector_id)

    def poi(self, poi_id: str) -> Poi:
        return next(p for p in self.pois if p.poi_id == poi_id)

    def poi_slot(self, poi_id: str, slot: int, spacing_m: float = 1.5) -> Pose2D:
        """Where the ``slot``-th robot at a POI stands: slot 0 is the viewpoint; then spots
        ``spacing_m`` apart beside it (left, right, further left, ...), facing the same way,
        and behind it if those are blocked. Deterministic; never inside a building."""
        base = self.poi(poi_id).pose
        if slot == 0:
            return base
        if self.footpaths_only and self.route_graph is not None:
            # On the footpath through the POI: ahead, behind, further ahead, ...
            from a6g_common.footpaths import walker_for

            offsets = [sign * k * spacing_m for k in range(1, 9) for sign in (1, -1)]
            spots: list[tuple[float, float]] = []
            for q in walker_for(self.route_graph).along((base.x, base.y), offsets):
                if all(math.dist(q, o) > spacing_m / 2 for o in [(base.x, base.y), *spots]):
                    spots.append(q)
            x, y = spots[(slot - 1) % len(spots)] if spots else (base.x, base.y)
            return Pose2D(x=round(x, 3), y=round(y, 3), yaw=base.yaw, frame=base.frame)
        c, s = math.cos(base.yaw), math.sin(base.yaw)
        left, back = (-s, c), (-c, -s)
        candidates = []
        for k in range(1, 9):
            side = 1 if k % 2 else -1
            row = (k + 1) // 2
            candidates.append((side * row * spacing_m, 0.0))
        for k in range(1, 5):
            candidates.append((0.0, k * spacing_m))
        free = []
        for lat, bk in candidates:
            x = base.x + lat * left[0] + bk * back[0]
            y = base.y + lat * left[1] + bk * back[1]
            if not any(point_in_polygon((x, y), o) for o in self.obstacles) and all(
                _edge_distance((x, y), o) >= 0.6 for o in self.obstacles
            ):
                free.append(Pose2D(x=round(x, 3), y=round(y, 3), yaw=base.yaw, frame=base.frame))
        if not free:
            return base
        return free[(slot - 1) % len(free)]


# --------------------------------------------------------------------------- profiles


class ProfileName(StrEnum):
    SIM = "sim"
    GAZEBO = "gazebo"
    REAL = "real"


class ModelPolicy(A6GModel):
    cloud_allowed: bool


#: Roles that may receive camera images, audio or interaction content (the embedder is
#: queried with what people say): kept on premises on real robots.
SENSITIVE_ROLES = frozenset({ModelRole.EDGE_VLM, ModelRole.ONBOARD_VLM, ModelRole.EMBEDDER})


class Localization(StrEnum):
    """Localization source. ``ground_truth`` is a simulation-only debugging aid."""

    GROUND_TRUTH = "ground_truth"
    AMCL_PRIOR = "amcl_prior"
    GNSS_EKF = "gnss_ekf"
    GNSS_EKF_MAP = "gnss_ekf_map"


class PlanarTransform(A6GModel):
    x: float = 0.0
    y: float = 0.0
    yaw: float = 0.0


class GnssNoise(A6GModel):
    """Simulated GNSS error: white noise plus a Gauss-Markov (slow random-walk)
    bias per axis, and random dropouts."""

    sigma_m: float = Field(ge=0.0, description="white noise, 1 sigma per horizontal axis")
    bias_sigma_m: float = Field(0.0, ge=0.0, description="steady-state bias, 1 sigma")
    bias_tau_s: float = Field(300.0, gt=0.0, description="bias correlation time")
    dropout_per_min: float = Field(0.0, ge=0.0)
    dropout_s: float = Field(0.0, ge=0.0)


class RobotProfile(A6GModel):
    """Per-robot deployment settings, read by uv services and ROS launch files."""

    ros_domain_id: int = Field(ge=0, le=232)
    use_sim_time: bool
    localization: Localization = Localization.AMCL_PRIOR
    #: Static transform from the robot's Nav2 map frame to ``campus_map``.
    map_to_campus: PlanarTransform = Field(default_factory=PlanarTransform)
    #: Gazebo only: spawn pose in ``campus_map``.
    spawn: PlanarTransform | None = None
    #: Unix socket between ``skill_mcp`` and ``ros_adapter``; default ``/run/a6g/<id>.sock``.
    adapter_socket: str | None = None
    #: Overrides the manifest's ``nav_mode`` (e.g. ``goal_pose`` if Foxy results misbehave).
    nav_mode: NavMode | None = None
    #: Driving speed (m/s): Nav2 cruise speed, the simulated platform cap and the speed the
    #: allocator plans with. Unset: the manifest's ``max_speed_mps``.
    max_speed_mps: float | None = Field(None, gt=0.0, le=1.0)
    #: Gazebo only: in-place turn rate (rad/s) of the simulated Go2 (its static-walk turn)
    #: and the allocator's turn estimate. A real robot turns at its own speed.
    turn_rate_radps: float | None = Field(None, gt=0.0, le=1.0)
    #: Gazebo: which ``gnss_noise_profiles`` entry the simulated receiver uses.
    gnss_noise: str = "rtk"
    #: Gazebo: coverage shadows also drop the GNSS fix (off by default).
    gnss_dropout_in_shadows: bool = False


class ProfileConfig(Versioned):
    """Deployment profile."""

    profile: ProfileName
    models: dict[ModelRole, ModelPolicy] = Field(default_factory=dict)
    robots: dict[str, RobotProfile] = Field(default_factory=dict)
    #: Multiplies every skill timeout (simulation may run slower than real time).
    timeout_scale: float = Field(1.0, gt=0.0, le=10.0)
    #: Gazebo: GNSS noise profiles (rtk, standard, degraded, ...).
    gnss_noise_profiles: dict[str, GnssNoise] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_robots(self) -> "ProfileConfig":
        problems = []
        domains: dict[int, str] = {}
        for robot_id, r in sorted(self.robots.items()):
            if r.ros_domain_id in domains:
                problems.append(
                    f"robots.{robot_id}.ros_domain_id {r.ros_domain_id} is already used by "
                    f"{domains[r.ros_domain_id]} (one ROS domain per robot, Rule 17)"
                )
            domains.setdefault(r.ros_domain_id, robot_id)
            if r.use_sim_time != (self.profile is ProfileName.GAZEBO):
                problems.append(
                    f"robots.{robot_id}.use_sim_time must be "
                    f"{self.profile is ProfileName.GAZEBO} under profile '{self.profile.value}'"
                )
            if self.profile is ProfileName.REAL and r.localization is Localization.GROUND_TRUTH:
                problems.append(
                    f"robots.{robot_id}.localization: ground_truth is not allowed with profile real"
                )
            if (
                self.profile is ProfileName.GAZEBO
                and self.gnss_noise_profiles
                and r.gnss_noise not in self.gnss_noise_profiles
            ):
                problems.append(
                    f"robots.{robot_id}.gnss_noise {r.gnss_noise!r} is not one of "
                    f"{sorted(self.gnss_noise_profiles)}"
                )
            if self.profile is not ProfileName.GAZEBO and r.spawn is not None:
                problems.append(f"robots.{robot_id}.spawn is only meaningful with profile gazebo")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def cloud_allowed(self, role: ModelRole) -> bool:
        if role in self.models:
            return self.models[role].cloud_allowed
        # Real robots: anything that may see camera images or hear audio stays local.
        return not (self.profile is ProfileName.REAL and role in SENSITIVE_ROLES)


def is_cloud_endpoint(endpoint: ModelEndpoint) -> bool:
    """A provider API (no base_url) or a base_url outside loopback / private networks."""
    import ipaddress
    import socket

    if not endpoint.base_url:
        return True
    host = urlsplit(endpoint.base_url).hostname or ""
    if host in ("localhost",) or host.endswith((".local", ".lan", ".internal")):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            address = ipaddress.ip_address(socket.gethostbyname(host))
        except OSError:
            return True  # unresolvable public-looking name: treat as cloud
    return not (address.is_private or address.is_loopback or address.is_link_local)


def adapter_socket(robot_id: str, profile: "ProfileConfig | None") -> str:
    r = profile.robots.get(robot_id) if profile else None
    return (r.adapter_socket if r and r.adapter_socket else None) or f"/run/a6g/{robot_id}.sock"


#: Where each localization profile publishes the pose covariance the adapter reports.
GNSS_LOCALIZATION = (Localization.GNSS_EKF, Localization.GNSS_EKF_MAP)


def adapter_config(
    cfg: "A6GConfig",
    robot_id: str,
    image_dir: str = "var/images",
    localization: "Localization | str | None" = None,
) -> dict:
    """The ``ros_adapter`` JSON config: manifest + profile.

    The adapter runs under the robot's ROS Python (3.8 on the Go2) with stdlib + rclpy only,
    so it reads this JSON instead of the YAML tree.
    """
    if cfg.profile is None or cfg.profile.profile is ProfileName.SIM:
        raise ConfigError(cfg.config_dir, ["adapter-config needs --profile gazebo or real"])
    m = cfg.manifests[robot_id]
    r = cfg.profile.robots.get(robot_id)
    loc = Localization(localization or (r.localization if r else Localization.AMCL_PRIOR))
    ros = m.ros.model_dump(mode="json")
    if loc in GNSS_LOCALIZATION:  # the global EKF's output carries the pose covariance
        ros["pose_topic"] = f"{m.ros.namespace.rstrip('/')}/odometry/global"
    return {
        "robot_id": robot_id,
        "platform": m.platform.value,
        "profile": cfg.profile.profile.value,
        "socket": adapter_socket(robot_id, cfg.profile),
        "ros_domain_id": r.ros_domain_id if r else None,
        "nav_mode": (r.nav_mode if r and r.nav_mode else m.nav_mode).value,
        "camera_topic": m.camera_topic,
        "forbidden_motions": [f.value for f in m.forbidden_motions],
        "reverse_limits": m.reverse_limits.model_dump() if m.reverse_limits else None,
        "goal_tolerance": m.goal_tolerance.model_dump() if m.goal_tolerance else None,
        "image_dir": str(Path(image_dir).resolve()),
        "clock_check_s": 3.0,
        "localization": loc.value,
        "ros": ros,
    }


def check_models_against_profile(models: ModelsConfig, profile: ProfileConfig) -> list[str]:
    problems = []
    for role, endpoint in sorted(models.roles.items()):
        if is_cloud_endpoint(endpoint) and not profile.cloud_allowed(role):
            where = endpoint.base_url or f"the {endpoint.provider or 'provider'} cloud API"
            problems.append(
                f"roles.{role.value}: points at a cloud endpoint ({where}) but profile "
                f"'{profile.profile.value}' does not allow cloud models for this role "
                "(camera images, audio and interaction content stay on premises); "
                "set a local base_url"
            )
    return problems


# --------------------------------------------------------------------------- aggregate


# --------------------------------------------------------------------------- comms


class BindingName(StrEnum):
    A2A_JSONRPC = "a2a_jsonrpc"
    MCP = "mcp"  # tools (knowledge server)
    A2A_GRPC = "a2a_grpc"
    MQTT = "mqtt"
    ZENOH = "zenoh"


class EncodingName(StrEnum):
    JSON = "json"
    MSGPACK = "msgpack"
    CBOR = "cbor"
    PROTOBUF = "protobuf"


class PatternSpec(A6GModel):
    """One interaction pattern of the catalogue: requirements, for reasoning
    per pattern in experiments."""

    name: str
    shape: str
    typical: str = ""
    requirement: str = ""
    priority_class: int = Field(0, ge=0, le=9)


class Route(A6GModel):
    """How one pattern travels: binding, encoding, compression and NL policy."""

    binding: BindingName
    encoding: EncodingName = EncodingName.JSON
    compression: str = Field("none", pattern=r"^(none|zstd)$")
    nl: str = Field("full", pattern=r"^(full|none|capped\(\d+\))$")


class CommsMetrics(A6GModel):
    #: Every message is appended here as one JSON line (measured per message); relative to
    #: the cwd.
    #: ``A6G_COMMS_LOG`` overrides it; empty disables the file (in-memory only).
    path: str = "var/comms/messages.jsonl"


_PATTERN_IDS = ("P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9", "P10", "P11")


class CommsConfig(Versioned):
    """``config/comms.yaml``: the pattern catalogue and the routing presets."""

    patterns: dict[str, PatternSpec]
    presets: dict[str, dict[str, Route]]
    preset: str = "hybrid"
    metrics: CommsMetrics = Field(default_factory=CommsMetrics)

    @model_validator(mode="after")
    def _check(self) -> "CommsConfig":
        problems = []
        if sorted(self.patterns, key=lambda p: int(p[1:])) != list(_PATTERN_IDS):
            problems.append(f"patterns must be exactly {list(_PATTERN_IDS)}")
        if self.preset not in self.presets:
            problems.append(f"preset {self.preset!r} not among {sorted(self.presets)}")
        for name, routes in self.presets.items():
            missing = sorted(set(_PATTERN_IDS) - set(routes))
            if missing:
                problems.append(f"preset {name!r} has no route for {missing}")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def route(self, pattern: str) -> Route:
        return self.presets[self.preset][str(pattern)]


class KnowledgeConfig(A6GModel):
    """Campus knowledge server, ``config/knowledge.yaml``."""

    #: MCP endpoint of the knowledge server; in-process stacks serve it themselves.
    url: str = "http://127.0.0.1:8400/mcp"
    resolve_min_score: float = Field(0.45, ge=0.0, le=1.0)
    status_ttl_min: float = Field(60.0, gt=0.0, description="robot status reports expire")
    status_match_radius_m: float = Field(3.0, gt=0.0, description="observation to subject")
    walking_speed_mps: float = Field(0.8, gt=0.0, description="escort route times")
    use_embeddings: bool = Field(True, description="hybrid search; off: lexical only")
    timeout_s: float = Field(3.0, gt=0.0, description="past it the caller falls back")
    #: Observations near a pose (``kb_observations_near``).
    observation_radius_m: float = Field(25.0, gt=0.0, description="default search radius")
    observation_ttl_min: float = Field(240.0, gt=0.0, description="older ones are not kept")


class InteractionsConfig(A6GModel):
    """Human interactions, ``config/interactions.yaml``."""

    #: HTTP endpoint of the InteractionStore; in-process stacks host it themselves.
    url: str = "http://127.0.0.1:8410"
    #: Entries expire after this (assumed; to be set with the DPO and D6.1).
    interaction_ttl_min: float = Field(60.0, gt=0.0)
    #: Deployment languages (assumed); the reply follows the language of the request.
    languages: list[Literal["es", "ca-valencia", "en"]] = Field(
        default_factory=lambda: ["es", "ca-valencia", "en"]
    )
    #: Development only: the operator's chat shows the declared
    #: constraint. None: on in the sim and gazebo profiles, off in real.
    show_constraints_to_operator: bool | None = None
    #: The person has this long to confirm the request or the arrival: wall-clock seconds
    #: (people answer in real time, even when the simulator runs faster).
    confirm_timeout_s: float = Field(120.0, gt=0.0)
    arrival_confirm_s: float = Field(30.0, gt=0.0)
    #: Event images are deleted after this (assumed; to be set with the data management plan).
    image_retention_h: float = Field(24.0, gt=0.0)
    timeout_s: float = Field(3.0, gt=0.0, description="store calls; past it the robot copy")


class _NetworkFile(BaseModel):
    network: NetworkConfig = Field(default_factory=NetworkConfig)


class A6GConfig(BaseModel):
    """Everything under ``config/``, cross-validated."""

    config_dir: Path
    fleet: FleetConfig
    manifests: dict[str, RobotManifest]
    models: ModelsConfig
    allocator: AllocatorConfig
    campus: CampusConfig
    profile: ProfileConfig | None = None
    comms: CommsConfig | None = None
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    interactions: InteractionsConfig = Field(default_factory=InteractionsConfig)
    #: The campus cell, coverage map, link profiles and capacity
    network: NetworkConfig = Field(default_factory=NetworkConfig)

    @property
    def show_constraints_to_operator(self) -> bool:
        """Development profiles show the declared constraint; real does not."""
        if self.interactions.show_constraints_to_operator is not None:
            return self.interactions.show_constraints_to_operator
        return self.profile is None or self.profile.profile is not ProfileName.REAL


# --------------------------------------------------------------------------- YAML loading

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys instead of silently overriding."""


def _construct_mapping(loader: _StrictLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark
            )
        mapping[key] = loader.construct_object(value_node)
    return mapping


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def _interpolate(value: Any, path: Path, env: dict[str, str], where: str = "") -> Any:
    if isinstance(value, dict):
        return {
            k: _interpolate(v, path, env, f"{where}.{k}" if where else str(k))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_interpolate(v, path, env, f"{where}[{i}]") for i, v in enumerate(value)]
    if isinstance(value, str):

        def repl(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            if name in env:
                return env[name]
            if default is not None:
                return default
            raise ConfigError(path, [f"{where}: environment variable ${{{name}}} is not set"])

        return _ENV_PATTERN.sub(repl, value)
    return value


def load_yaml(path: Path, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Load a YAML mapping strictly, with ``${VAR}`` / ``${VAR:-default}`` interpolation."""
    if not path.is_file():
        raise ConfigError(path, ["file not found"])
    try:
        data = yaml.load(path.read_text(), Loader=_StrictLoader)
    except yaml.YAMLError as exc:
        raise ConfigError(path, [f"YAML error: {exc}"]) from exc
    if not isinstance(data, dict):
        raise ConfigError(path, ["top level must be a mapping"])
    return _interpolate(data, path, dict(os.environ) if env is None else env)


M = TypeVar("M", bound=BaseModel)


def _format_errors(exc: ValidationError) -> list[str]:
    problems = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        problems.append(f"{loc}: {err['msg']}")
    return problems


def load_model(model: type[M], path: Path, env: dict[str, str] | None = None) -> M:
    """Load ``path`` and validate it as ``model``; raise :class:`ConfigError` on any problem."""
    data = load_yaml(path, env)
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(path, _format_errors(exc)) from exc


def load_campus(path: Path, env: dict[str, str] | None = None) -> CampusConfig:
    """Campus file v1 (ENU, legacy), v2 (WGS84) or v3 (v2 + knowledge base) -> runtime ENU
    config."""
    from a6g_common.campus import CampusV2

    data = load_yaml(path, env)
    if str(data.get("schema_version", "")).split(".")[0] not in ("2", "3"):
        return load_model(CampusConfig, path, env)
    try:
        v2 = CampusV2.model_validate(data).with_scale_override(env)
        return CampusConfig.model_validate(v2.to_enu(base_dir=path.parent))
    except ValueError as exc:  # pydantic ValidationError is a ValueError too
        errors = _format_errors(exc) if isinstance(exc, ValidationError) else [str(exc)]
        raise ConfigError(path, errors) from exc


def load_manifest(path: Path, env: dict[str, str] | None = None) -> RobotManifest:
    return load_model(RobotManifest, path, env)


def resolve_config_dir(config_dir: Path | str | None = None) -> Path:
    if config_dir is not None:
        return Path(config_dir)
    return Path(os.environ.get(DEFAULT_CONFIG_DIR_ENV, "config"))


def load_config(
    config_dir: Path | str | None = None,
    env: dict[str, str] | None = None,
    fleet_file: str | None = None,
    profile: str | None = None,
) -> A6GConfig:
    """Load and cross-validate the whole configuration tree.

    ``fleet_file`` (relative to the config dir) selects another fleet, e.g. ``sim/fleet.yaml``;
    default ``$A6G_FLEET_FILE`` or ``fleet.yaml``. All other paths stay relative to the dir.
    """
    root = resolve_config_dir(config_dir)
    environ = os.environ if env is None else env
    fleet_path = root / (fleet_file or environ.get("A6G_FLEET_FILE") or "fleet.yaml")
    fleet = load_model(FleetConfig, fleet_path, env)

    manifests: dict[str, RobotManifest] = {}
    for robot in fleet.robots:
        manifest_path = root / robot.manifest
        manifest = load_manifest(manifest_path, env)
        if manifest.robot_id != robot.robot_id:
            raise ConfigError(
                manifest_path,
                [
                    f"robot_id: manifest declares {manifest.robot_id!r} but fleet.yaml "
                    f"lists it for {robot.robot_id!r}"
                ],
            )
        if robot.simulated_battery and not manifest.battery_reliable:
            manifest = manifest.model_copy(update={"battery_reliable": True})
        manifests[robot.robot_id] = manifest

    models = load_model(ModelsConfig, root / "models.yaml", env)
    profile_name = profile or environ.get("A6G_PROFILE")
    profile_cfg = None
    if profile_name:
        profile_path = root / "profiles" / f"{profile_name}.yaml"
        profile_cfg = load_model(ProfileConfig, profile_path, env)
        problems = check_models_against_profile(models, profile_cfg)
        if problems:
            raise ConfigError(root / "models.yaml", problems)
    if profile_cfg is not None:  # per-robot speed overrides feed the allocator's estimates
        problems = []
        for rid, rp in profile_cfg.robots.items():
            if rp.turn_rate_radps is not None and rid in manifests:
                if profile_cfg.profile is not ProfileName.GAZEBO:
                    problems.append(
                        f"robots.{rid}.turn_rate_radps is simulation-only: a real robot "
                        "turns at its own speed"
                    )
                else:
                    manifests[rid] = manifests[rid].model_copy(
                        update={"turn_in_place_s_per_rad": round(1.0 / rp.turn_rate_radps, 3)}
                    )
            if rp.max_speed_mps is None or rid not in manifests:
                continue
            limit = manifests[rid].max_speed_mps
            if profile_cfg.profile is ProfileName.REAL and rp.max_speed_mps > limit:
                problems.append(
                    f"robots.{rid}.max_speed_mps {rp.max_speed_mps} exceeds the manifest's "
                    f"{limit} m/s: a real robot can only be slowed down"
                )
                continue
            manifests[rid] = manifests[rid].model_copy(update={"max_speed_mps": rp.max_speed_mps})
        if problems:
            raise ConfigError(root / "profiles" / f"{profile_name}.yaml", problems)
    cfg = A6GConfig(
        config_dir=root,
        fleet=fleet,
        manifests=manifests,
        models=models,
        allocator=load_model(AllocatorConfig, root / "allocator.yaml", env),
        campus=load_campus(root / fleet.campus, env),
        profile=profile_cfg,
        comms=load_model(CommsConfig, root / "comms.yaml", env)
        if (root / "comms.yaml").exists()
        else None,
        knowledge=load_model(KnowledgeConfig, root / "knowledge.yaml", env)
        if (root / "knowledge.yaml").exists()
        else KnowledgeConfig(),
        interactions=load_model(InteractionsConfig, root / "interactions.yaml", env)
        if (root / "interactions.yaml").exists()
        else InteractionsConfig(),
        network=load_model(_NetworkFile, root / "network.yaml", env).network
        if (root / "network.yaml").exists()
        else NetworkConfig(),
    )
    if cfg.network.coverage.source != "shadows":
        cfg.campus._level_at = _coverage_lookup(cfg)
    return cfg


def _coverage_lookup(cfg: A6GConfig) -> Callable[[float, float], str | None]:
    """``CampusConfig.link_level_at`` from the coverage map, loaded now: on first use it
    would be file I/O inside the Agent Server's event loop (BlockingError)."""
    from a6g_common.coverage import load_or_build

    try:
        cmap = load_or_build(cfg)
    except (FileNotFoundError, OSError, ValueError):
        cmap = None

    def level_at(x: float, y: float) -> str | None:
        sample = cmap.nearest(x, y) if cmap is not None else None
        return sample["level"] if sample else None

    return level_at


def _edge_distance(p: tuple[float, float], polygon: list[tuple[float, float]]) -> float:
    best = math.inf
    for (ax, ay), (bx, by) in zip(polygon, polygon[1:] + polygon[:1], strict=True):
        dx, dy = bx - ax, by - ay
        t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / (dx * dx + dy * dy or 1e-12)))
        best = min(best, math.hypot(p[0] - ax - t * dx, p[1] - ay - t * dy))
    return best
