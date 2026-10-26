"""Shared wire schemas (SPEC Section 5).

Every top-level message carries ``schema_version``. Times are timezone-aware UTC and
serialise as ISO-8601. Coordinates are in the shared ENU frame ``campus_map`` unless a
field says otherwise. Unknown fields are rejected so that partner mismatches fail loudly.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StringConstraints,
    field_validator,
    model_serializer,
    model_validator,
)

SCHEMA_VERSION = "1.0"
CAMPUS_FRAME = "campus_map"


# --------------------------------------------------------------------------- primitives


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware (UTC ISO-8601)")
    return value.astimezone(UTC)


UtcDatetime = Annotated[datetime, AfterValidator(_ensure_utc)]
"""A timezone-aware datetime, normalised to UTC."""

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,127}$")]
"""Generic opaque id (mission, task, envelope, ...)."""

RobotId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
"""Robot ids are lowercase snake case, e.g. ``go2_01``; they are also tie-breakers."""

Point2D = tuple[float, float]
Polygon = Annotated[list[Point2D], Field(min_length=3)]
"""Simple polygon in ``campus_map`` as a list of (x, y) vertices (not closed)."""

Percent = Annotated[float, Field(ge=0.0, le=100.0)]
Priority = Annotated[int, Field(ge=0, le=100)]


class A6GModel(BaseModel):
    """Base for every schema: strict about unknown fields and non-finite numbers."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, use_enum_values=False)


class Versioned(A6GModel):
    """Top-level message carrying ``schema_version`` (major version must match)."""

    schema_version: str = SCHEMA_VERSION

    @field_validator("schema_version")
    @classmethod
    def _same_major(cls, value: str) -> str:
        major = SCHEMA_VERSION.split(".")[0]
        if value.split(".")[0] != major:
            raise ValueError(f"unsupported schema_version {value!r}; expected {major}.x")
        return value


# --------------------------------------------------------------------------- enums


class Posture(StrEnum):
    ROUTINE = "routine"
    INTERACTIVE = "interactive"
    EMERGENCY = "emergency"


class Platform(StrEnum):
    GO2 = "go2"
    SUMMIT = "summit"


class LinkQuality(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    LOST = "lost"


class TaskKind(StrEnum):
    SURVEY_SECTOR = "survey_sector"
    INSPECT_POI = "inspect_poi"
    HOLD_POST = "hold_post"
    ESCORT = "escort"
    RETURN_HOME = "return_home"
    GO_TO = "go_to"  # drive to a POI (or pose) and stay there


class SkillName(StrEnum):
    """The complete MCP skill allow-list (SPEC 6.1). There are no other tools."""

    GET_STATE = "get_state"
    NAVIGATE_TO = "navigate_to"
    FOLLOW_ROUTE = "follow_route"
    INSPECT = "inspect"
    HOLD_POST = "hold_post"
    RETURN_HOME = "return_home"
    CANCEL = "cancel"
    STOP = "stop"
    CAPTURE_EVENT_IMAGE = "capture_event_image"


#: Skills that do not require the robot to be armed (SPEC 6.1 / Rule 7).
UNARMED_SKILLS = frozenset(
    {SkillName.GET_STATE, SkillName.STOP, SkillName.CANCEL, SkillName.CAPTURE_EVENT_IMAGE}
)
#: Skills that move the robot.
MOTION_SKILLS = frozenset(SkillName) - UNARMED_SKILLS


class SkillOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    NO_PATH = "no_path"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    REFUSED_DISARMED = "refused_disarmed"
    REFUSED_PRECONDITION = "refused_precondition"
    TF_ERROR = "tf_error"
    UNKNOWN = "unknown"


class RefusalReason(StrEnum):
    """Why a robot refuses a task (SPEC 8.2 evaluate_envelope)."""

    BATTERY = "battery"
    CAPABILITY = "capability"
    REGION = "region"
    CONFLICT = "conflict"
    LINK = "link"  # no new tasks while link_quality = lost


class TrafficClass(StrEnum):
    """Uplink priority classes, highest first (SPEC 11 bandwidth budget)."""

    CONTROL_MSGS = "control_msgs"
    VOICE = "voice"
    EVENT_IMAGE = "event_image"
    MAP_DELTA = "map_delta"
    BULK = "bulk"


class DecisionKind(StrEnum):
    DECOMPOSE = "decompose"  # goal -> tasks (LLM or template)
    VALIDATE = "validate"  # invalid tasks dropped
    ARBITRATE = "arbitrate"  # posture priority / preemption
    ALLOCATE = "allocate"
    REALLOCATE = "reallocate"
    ACCEPT = "accept"
    REFUSE = "refuse"
    RENEGOTIATE = "renegotiate"
    BID = "bid"
    AWARD = "award"
    ESCALATE = "escalate"
    FALLBACK = "fallback"
    SKILL = "skill"
    # Part A3 (SPEC A3.10)
    STATUS_UPDATE = "status_update"
    RECONCILE = "reconcile"
    RISK = "risk"
    ACCESS_DENIED = "access_denied"
    FORGET = "forget"


# --------------------------------------------------------------------------- geometry


class Pose2D(A6GModel):
    x: float
    y: float
    yaw: float = Field(0.0, description="radians, ENU (0 = east, CCW positive)")
    frame: str = CAMPUS_FRAME


class PoseWithCovariance(Pose2D):
    cov_xy: float | None = Field(
        None, ge=0.0, description="covariance summary: trace of the xy block, m^2"
    )
    cov_yaw: float | None = Field(None, ge=0.0, description="yaw variance, rad^2")


# --------------------------------------------------------------------------- mission & tasks


class MissionGoal(Versioned):
    mission_id: Identifier
    text: str = Field(min_length=1, description="natural-language goal")
    posture: Posture
    priority: Priority = 50
    deadline: UtcDatetime | None = None
    issued_by: str = Field(min_length=1)


class SectorTarget(A6GModel):
    type: Literal["sector"] = "sector"
    sector_id: Identifier
    #: A remainder survey (SPEC A3.4.4): the sector's survey route from this waypoint on
    #: (counted in the direction below).
    from_waypoint: int = Field(0, ge=0)
    #: The survey route run from its last waypoint. None (a whole patrol lap): from the end
    #: nearer the robot, so a robot that has just finished a lap goes straight back along
    #: the route instead of walking to its start (added 2026-10-19).
    reverse: bool | None = None
    #: The direction that starts at the end patrolled longest ago (where the sector's
    #: previous lap started): taken for a whole lap unless that end is more than half the
    #: route farther than the nearer one (a robot coming out of a coverage shadow does not
    #: turn back over what it has just patrolled; added 2026-10-21).
    prefer_reverse: bool | None = None


class PoiTarget(A6GModel):
    type: Literal["poi"] = "poi"
    poi_id: Identifier
    slot: int = Field(
        0,
        ge=0,
        le=7,
        description="0: the POI viewpoint; k > 0: the k-th spot beside it, so several robots "
        "sent to one POI do not converge on the same point",
    )
    viewpoint: Pose2D | None = Field(
        None,
        description="the post is held from here instead of the POI (SPEC A4.7 "
        "relocate_for_coverage: a nearby spot with better coverage, in sight of the POI)",
    )

    @model_serializer(mode="wrap")
    def _omit_unset_viewpoint(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """Left out when unset: messages and checkpoints stay as before the variant."""
        out = handler(self)
        if out.get("viewpoint") is None:
            out.pop("viewpoint", None)
        return out


class PoseTarget(A6GModel):
    type: Literal["pose"] = "pose"
    pose: Pose2D
    name: str | None = Field(None, description="the named map spot, for people (East gate)")


TaskTarget = Annotated[SectorTarget | PoiTarget | PoseTarget, Field(discriminator="type")]


class TaskSpec(Versioned):
    task_id: Identifier
    kind: TaskKind
    target: TaskTarget
    required_capabilities: list[str] = Field(default_factory=list)
    est_duration_s: float = Field(ge=0.0)
    priority: Priority = 50
    sensing_mode: str | None = None
    depends_on: list[Identifier] = Field(default_factory=list)
    #: Constraints declared by a person (SPEC A2.3.1, NFR-A6G-14), e.g. "uses a wheelchair:
    #: step-free route": carried from the robot's conversation to the task; never dropped.
    constraints: list[str] = Field(default_factory=list)
    #: Escorts: the interaction whose content lives only in the InteractionStore (rule 28).
    interaction_id: Identifier | None = None
    #: Filled by the coordinator's ``derive_flows`` (SPEC A3.7).
    flow_class: str | None = None
    #: An emergency post (``hold_post``): held until the operator's all clear, not for
    #: ``est_duration_s``; the robot renews the bounded hold skill period after period
    #: (added 2026-10-19). Low battery still releases it (another robot takes it over).
    until_cleared: bool = False

    @model_validator(mode="after")
    def _check_deps(self) -> "TaskSpec":
        if self.kind is TaskKind.HOLD_POST and self.sensing_mode not in (
            None,
            "video",
            "telemetry",
            "state_only",  # SPEC A4.7: the network can carry no sensing stream there
        ):
            raise ValueError("hold_post sensing_mode must be video, telemetry or state_only")
        if self.task_id in self.depends_on:
            raise ValueError(f"task {self.task_id} depends on itself")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("depends_on contains duplicates")
        return self


# --------------------------------------------------------------------------- robot state


class RobotState(Versioned):
    robot_id: RobotId
    platform: Platform
    pose: PoseWithCovariance
    battery_pct: Percent | None = Field(
        description="None when the platform's battery estimate is unreliable/unknown"
    )
    capabilities: list[str]
    current_task_id: Identifier | None = None
    commitment_until: UtcDatetime | None = None
    link_quality: LinkQuality = LinkQuality.OK
    #: SPEC A4.6: coverage level and serving cell; ``link_quality`` is derived from the
    #: level (lost -> lost, low -> degraded, otherwise ok). None: not known.
    link_level: Literal["good", "medium", "low", "lost"] | None = None
    cell_id: str | None = None
    #: SPEC A4.6: the radio as the robot's modem reports it (emulated from the coverage map)
    rsrp_dbm: float | None = None
    sinr_db: float | None = None
    armed: bool = False
    stamp: UtcDatetime


# --------------------------------------------------------------------------- envelopes


class EnvelopeBudgets(A6GModel):
    battery_floor_pct: Percent
    uplink_mbps: float = Field(gt=0.0)


class Envelope(Versioned):
    envelope_id: Identifier
    robot_id: RobotId
    mission_id: Identifier
    tasks: list[TaskSpec]
    region: Polygon
    valid_from: UtcDatetime
    valid_until: UtcDatetime
    priority: Priority = 50
    budgets: EnvelopeBudgets
    swappable: bool = False
    version: int = Field(1, ge=1)
    #: Tasks the operator gave to this robot by name: a robot model may not refuse them
    #: while they are feasible (SPEC A2 rule 20).
    pinned_task_ids: list[Identifier] = Field(default_factory=list)
    #: SPEC A4.7 (I3): the network's answer per task with a streaming flow: class, state
    #: (granted | degraded | rejected) and the granted uplink rate. A robot opens a class
    #: above telemetry only when it is granted, at the granted rate.
    flows: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> "Envelope":
        if self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be after valid_from")
        ids = [t.task_id for t in self.tasks]
        if len(set(ids)) != len(ids):
            raise ValueError("envelope contains duplicate task_ids")
        return self


# --------------------------------------------------------------------------- contract-net


class CallForProposals(Versioned):
    cfp_id: Identifier
    mission_id: Identifier
    envelope_id: Identifier = Field(description="envelope the task currently belongs to")
    initiator: RobotId
    task: TaskSpec
    deadline_ms: int = Field(1500, gt=0)
    issued_at: UtcDatetime


class Bid(Versioned):
    cfp_id: Identifier
    bidder: RobotId
    cost: float | None = Field(None, ge=0.0, description="None means the bidder refuses")
    refusal_reason: str | None = None
    stamp: UtcDatetime

    @model_validator(mode="after")
    def _cost_xor_refusal(self) -> "Bid":
        if (self.cost is None) == (self.refusal_reason is None):
            raise ValueError("a Bid carries exactly one of cost or refusal_reason")
        return self


class Award(Versioned):
    award_id: Identifier = Field(description="idempotency key for award acceptance")
    cfp_id: Identifier
    mission_id: Identifier
    envelope_id: Identifier
    task_id: Identifier
    awarded_by: RobotId
    awarded_to: RobotId
    cost: float = Field(ge=0.0)
    stamp: UtcDatetime


# --------------------------------------------------------------------------- perception


class Evidence(A6GModel):
    image_uri: str | None = None
    #: SHA-256 of the image: the audit trail survives the image's deletion (SPEC A3.5).
    image_sha256: str | None = None
    model_id: str | None = None
    prompt_hash: str | None = None


class Observation(Versioned):
    obs_id: Identifier
    robot_id: RobotId
    mission_id: Identifier | None = None
    pose: Pose2D
    label: str = Field(min_length=1, description="free-text open-vocabulary label")
    novel: bool
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: Evidence = Field(default_factory=Evidence)
    reasoning: str = ""
    requires_confirmation: bool = False
    stamp: UtcDatetime
    # Part A3 (SPEC A3.10)
    measurements: dict[str, float] = Field(default_factory=dict, description="e.g. water_level_m")
    person_present: bool = Field(False, description="a person is in view (never identified)")
    subject: str | None = Field(None, description="graph / POI id matched to a status")


# --------------------------------------------------------------------------- skills & audit


class SkillResult(Versioned):
    skill: SkillName
    outcome: SkillOutcome
    detail: str = ""
    native_code: int | None = None
    duration_s: float = Field(ge=0.0)
    artifact_uri: str | None = Field(None, description="e.g. event image URI from inspect")
    measurements: dict[str, float] = Field(default_factory=dict, description="e.g. water_level_m")


class Decision(Versioned):
    decision_id: Identifier
    actor: str = Field(min_length=1, description="agent id that took the decision")
    kind: DecisionKind
    mission_id: Identifier | None = Field(
        None, description="mission the decision belongs to (enables `a6g decisions --mission`)"
    )
    inputs_ref: str = Field("", description="reference(s) to the inputs, e.g. event or state ids")
    output: dict[str, Any] = Field(default_factory=dict)
    policy: dict[str, Any] = Field(
        default_factory=dict, description="rule name and/or cost weights applied"
    )
    justification: str = Field("", description="human-readable reason; may be LLM text")
    stamp: UtcDatetime
    trace_id: str | None = None
    # Part A3 (SPEC A3.9-A3.10): external validation and the tamper-evident chain.
    validated_by: str | None = Field(None, description="agent that re-validated it")
    prev_hash: str | None = Field(None, description="hash of the previous record")
    hash: str | None = Field(None, description="SHA-256 of this record's canonical JSON")


# --------------------------------------------------------------------------- robot -> edge


class EnvelopeReply(Versioned):
    """A robot's answer to an envelope: accept, refuse, or renegotiate (propose a subset)."""

    envelope_id: Identifier
    version: int = Field(ge=1)
    robot_id: RobotId
    mission_id: Identifier
    decision: Literal["accept", "refuse", "renegotiate"]
    accepted_task_ids: list[Identifier] = Field(default_factory=list)
    refusals: dict[Identifier, RefusalReason] = Field(default_factory=dict)
    #: Per refused task, why in figures ("battery would end at 13.3% ..." or "recharging at
    #: the staging area"), for the operator (added 2026-10-21).
    refusal_details: dict[Identifier, str] = Field(default_factory=dict)
    justification: str = ""
    stamp: UtcDatetime

    @model_validator(mode="after")
    def _consistent(self) -> "EnvelopeReply":
        overlap = set(self.accepted_task_ids) & set(self.refusals)
        if overlap:
            raise ValueError(f"tasks both accepted and refused: {sorted(overlap)}")
        expected = {
            "accept": not self.refusals,  # an empty envelope (all preempted) is accepted
            "refuse": not self.accepted_task_ids and bool(self.refusals),
            "renegotiate": bool(self.accepted_task_ids) and bool(self.refusals),
        }
        if not expected[self.decision]:
            raise ValueError(f"decision {self.decision!r} inconsistent with accepted/refused tasks")
        return self


class ReportKind(StrEnum):
    SKILL_RESULT = "skill_result"
    TASK_STATUS = "task_status"
    OBSERVATION = "observation"
    DECISION = "decision"
    ENVELOPE_STATUS = "envelope_status"
    RECONCILE = "reconcile"  # after coordinator loss: current envelopes + awards
    HUMAN_REQUEST = "human_request"  # a person's request to the robot, confirmed (A2.3.1)
    INTERACTION_END = "interaction_end"  # outcome and guidance metrics, no content (A3.4.7)


class Report(Versioned):
    """Upstream report from a robot agent; queued while offline, idempotent by report_id."""

    report_id: Identifier
    robot_id: RobotId
    mission_id: Identifier | None = None
    envelope_id: Identifier | None = None
    kind: ReportKind
    traffic_class: TrafficClass
    payload: dict[str, Any]
    stamp: UtcDatetime


#: Every schema exported to partners by ``a6g schemas export``.
# --------------------------------------------------------------------------- Part A3 (A3.10)

StatusValue = Literal["ok", "out_of_service", "closed", "obstructed", "flooded"]
RESTRICTIVE_STATUSES = frozenset({"out_of_service", "closed", "obstructed", "flooded"})


class StatusReport(Versioned):
    """Live status of a graph node, edge or POI (SPEC A3.3.2); rule 27 decides its effect."""

    status_id: Identifier
    subject: str = Field(min_length=1, description="node, edge or POI id")
    status: StatusValue
    source: str = Field(
        pattern=r"^(robot:[A-Za-z0-9_\-]+|operator|notice:[A-Za-z0-9_\-.]+|instrumentation:[A-Za-z0-9_\-.]+)$"
    )
    evidence: str | None = Field(None, description="observation or document id")
    confidence: float = Field(1.0, ge=0.0, le=1.0)
    stamp: UtcDatetime
    valid_until: UtcDatetime | None = None

    @property
    def restrictive(self) -> bool:
        return self.status in RESTRICTIVE_STATUSES


RiskLevel = Literal["low", "elevated", "high", "critical"]


class RiskEntry(A6GModel):
    poi_id: Identifier
    water_level_m: float = Field(0.0, ge=0.0)
    trend: float = Field(0.0, description="m/min")
    risk: RiskLevel = "low"
    covered_by: list[RobotId] = Field(default_factory=list)
    flow: Literal["none", "requested", "granted", "degraded"] = "none"
    sensing_mode: Literal["video", "telemetry"] = "telemetry"
    recommendation: Literal["none", "advise_move_away", "recommend_closure"] = "none"
    escalated: bool = False
    last_update: UtcDatetime


class RiskPicture(Versioned):
    """Risk per exposed location (SPEC A3.6.2), in the mission twin."""

    mission_id: Identifier
    entries: list[RiskEntry] = Field(default_factory=list)
    stamp: UtcDatetime


class Finding(Versioned):
    """Fused observations of one anomaly (SPEC A3.6.3)."""

    finding_id: Identifier
    pose: Pose2D
    label: str = Field(min_length=1)
    observations: list[Identifier] = Field(min_length=1)
    state: Literal["raised", "confirmed", "rejected", "actioned", "closed"] = "raised"
    raised_at: UtcDatetime
    updated_at: UtcDatetime


class FlowRecord(Versioned):
    """A network flow implied by a task (SPEC A3.7); ``flow_id`` is the idempotency key."""

    flow_id: Identifier
    task_id: Identifier
    robot_id: RobotId
    flow_class: Literal[
        "telemetry",
        "survey_uplink",
        "verification_burst",
        "conversational",
        "video_control",
        "telemetry_plus",
    ] = Field(description="the flow class (SPEC A3.7 `class`)")
    location: str = Field(description="POI id or region")
    requested_at: UtcDatetime
    state: Literal["requested", "granted", "degraded", "rejected", "released"] = "requested"
    granted: dict[str, float] = Field(default_factory=dict, description="achieved parameters")
    reason: str = ""


class HealthReport(Versioned):
    """An agent's self-report (SPEC A3.9, NFR-A6G-07), MQTT ``a6g/health/<agent>``."""

    agent_id: str = Field(min_length=1)
    metrics: dict[str, float] = Field(default_factory=dict)
    boundaries_crossed: list[str] = Field(default_factory=list)
    stamp: UtcDatetime


class InteractionRef(Versioned):
    """What the twin may hold about a human interaction (rule 28): no content."""

    interaction_id: Identifier
    participants: list[str] = Field(min_length=1)
    created: UtcDatetime
    expires: UtcDatetime


EXPORTED_SCHEMAS: dict[str, type[BaseModel]] = {
    "MissionGoal": MissionGoal,
    "TaskSpec": TaskSpec,
    "RobotState": RobotState,
    "Envelope": Envelope,
    "CallForProposals": CallForProposals,
    "Bid": Bid,
    "Award": Award,
    "Observation": Observation,
    "SkillResult": SkillResult,
    "Decision": Decision,
    "EnvelopeReply": EnvelopeReply,
    "Report": Report,
    "StatusReport": StatusReport,
    "RiskPicture": RiskPicture,
    "Finding": Finding,
    "FlowRecord": FlowRecord,
    "HealthReport": HealthReport,
    "InteractionRef": InteractionRef,
}
