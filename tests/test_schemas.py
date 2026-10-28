"""Schemas round-trip to JSON and JSON Schema."""

import json
from datetime import UTC, datetime, timedelta, timezone

import jsonschema
import pytest
from a6g_common.jsonschema_export import export_json_schemas, json_schema_for
from a6g_common.schemas import (
    EXPORTED_SCHEMAS,
    Award,
    Bid,
    CallForProposals,
    Decision,
    DecisionKind,
    Envelope,
    EnvelopeBudgets,
    EnvelopeReply,
    Evidence,
    Finding,
    FlowRecord,
    HealthReport,
    InteractionRef,
    LinkQuality,
    MissionGoal,
    Observation,
    Platform,
    PoiTarget,
    Pose2D,
    PoseTarget,
    PoseWithCovariance,
    Posture,
    RefusalReason,
    Report,
    ReportKind,
    RiskEntry,
    RiskPicture,
    RobotState,
    SectorTarget,
    SkillName,
    SkillOutcome,
    SkillResult,
    StatusReport,
    TaskKind,
    TaskSpec,
    TrafficClass,
)
from pydantic import BaseModel, ValidationError

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)

SURVEY = TaskSpec(
    task_id="t_survey_north",
    kind=TaskKind.SURVEY_SECTOR,
    target=SectorTarget(sector_id="sec_north"),
    required_capabilities=["survey"],
    est_duration_s=600,
    priority=40,
)
INSPECT = TaskSpec(
    task_id="t_inspect_door",
    kind=TaskKind.INSPECT_POI,
    target=PoiTarget(poi_id="poi_library_door"),
    required_capabilities=["inspect"],
    est_duration_s=120,
    sensing_mode="rgb",
    depends_on=["t_survey_north"],
)
HOLD = TaskSpec(
    task_id="t_hold",
    kind=TaskKind.HOLD_POST,
    target=PoseTarget(pose=Pose2D(x=1.0, y=2.0, yaw=0.5)),
    est_duration_s=60,
)

EXAMPLES: dict[str, BaseModel] = {
    "MissionGoal": MissionGoal(
        mission_id="m_001",
        text="Survey all sectors of the pilot area",
        posture=Posture.ROUTINE,
        deadline=T0 + timedelta(hours=1),
        issued_by="operator",
    ),
    "TaskSpec": INSPECT,
    "RobotState": RobotState(
        robot_id="go2_01",
        platform=Platform.GO2,
        pose=PoseWithCovariance(x=1, y=2, yaw=0.1, cov_xy=0.02, cov_yaw=0.01),
        battery_pct=87.5,
        capabilities=["survey", "inspect"],
        current_task_id="t_survey_north",
        commitment_until=T0 + timedelta(minutes=10),
        link_quality=LinkQuality.DEGRADED,
        armed=True,
        stamp=T0,
    ),
    "Envelope": Envelope(
        envelope_id="env_001",
        robot_id="go2_01",
        mission_id="m_001",
        tasks=[SURVEY, INSPECT, HOLD],
        region=[(0, 0), (100, 0), (100, 100), (0, 100)],
        valid_from=T0,
        valid_until=T0 + timedelta(hours=1),
        budgets=EnvelopeBudgets(battery_floor_pct=20, uplink_mbps=3.0),
        swappable=True,
        version=2,
    ),
    "CallForProposals": CallForProposals(
        cfp_id="cfp_001",
        mission_id="m_001",
        envelope_id="env_001",
        initiator="go2_01",
        task=SURVEY,
        issued_at=T0,
    ),
    "Bid": Bid(cfp_id="cfp_001", bidder="summit_01", cost=42.5, stamp=T0),
    "Award": Award(
        award_id="aw_001",
        cfp_id="cfp_001",
        mission_id="m_001",
        envelope_id="env_001",
        task_id="t_survey_north",
        awarded_by="go2_01",
        awarded_to="summit_01",
        cost=42.5,
        stamp=T0,
    ),
    "Observation": Observation(
        obs_id="obs_001",
        robot_id="go2_01",
        mission_id="m_001",
        pose=Pose2D(x=35, y=58),
        label="unattended backpack near the library door",
        novel=True,
        confidence=0.62,
        evidence=Evidence(
            image_uri="file:///var/a6g/img/obs_001.jpg", model_id="stub", prompt_hash="ab12"
        ),
        reasoning="object not present in the prior map annotations",
        requires_confirmation=True,
        stamp=T0,
    ),
    "SkillResult": SkillResult(
        skill=SkillName.NAVIGATE_TO,
        outcome=SkillOutcome.NO_PATH,
        detail="ComputePathToPose failed",
        native_code=208,
        duration_s=3.2,
    ),
    "Decision": Decision(
        decision_id="dec_001",
        actor="coordinator",
        kind=DecisionKind.ALLOCATE,
        mission_id="m_001",
        inputs_ref="event:evt_001",
        output={"task_id": "t_survey_north", "robot_id": "go2_01", "cost": 12.3},
        policy={"rule": "hungarian", "weights": {"travel_time": 1.0}},
        justification="closest available robot with survey capability",
        stamp=T0,
        trace_id="0" * 32,
    ),
    "EnvelopeReply": EnvelopeReply(
        envelope_id="env_001",
        version=2,
        robot_id="go2_01",
        mission_id="m_001",
        decision="renegotiate",
        accepted_task_ids=["t_survey_north"],
        refusals={"t_hold": RefusalReason.BATTERY},
        justification="enough battery for the survey only",
        stamp=T0,
    ),
    "StatusReport": StatusReport(
        status_id="st_001",
        subject="poi_bus_stop",
        status="flooded",
        source="robot:go2_01",
        evidence="obs_001",
        confidence=0.8,
        stamp=T0,
        valid_until=T0 + timedelta(hours=1),
    ),
    "RiskPicture": RiskPicture(
        mission_id="m_001",
        entries=[
            RiskEntry(
                poi_id="poi_bus_stop",
                water_level_m=0.12,
                trend=0.01,
                risk="elevated",
                covered_by=["go2_01"],
                flow="granted",
                sensing_mode="video",
                last_update=T0,
            )
        ],
        stamp=T0,
    ),
    "Finding": Finding(
        finding_id="fnd_001",
        pose=Pose2D(x=1.0, y=2.0),
        label="unattended backpack",
        observations=["obs_001", "obs_002"],
        raised_at=T0,
        updated_at=T0,
    ),
    "FlowRecord": FlowRecord(
        flow_id="flow_001",
        task_id="t_hold",
        robot_id="go2_01",
        flow_class="video_control",
        location="poi_bus_stop",
        requested_at=T0,
        state="granted",
        granted={"uplink_mbps": 4.0},
    ),
    "HealthReport": HealthReport(
        agent_id="go2_01",
        metrics={"model_fallback_rate": 0.1, "queue_depth": 3.0},
        boundaries_crossed=["model_fallback_rate"],
        stamp=T0,
    ),
    "InteractionRef": InteractionRef(
        interaction_id="int_001",
        participants=["coordinator", "go2_01"],
        created=T0,
        expires=T0 + timedelta(hours=1),
    ),
    "Report": Report(
        report_id="rep_001",
        robot_id="go2_01",
        mission_id="m_001",
        envelope_id="env_001",
        kind=ReportKind.SKILL_RESULT,
        traffic_class=TrafficClass.CONTROL_MSGS,
        payload={"task_id": "t_survey_north", "outcome": "succeeded"},
        stamp=T0,
    ),
}


def test_every_exported_schema_has_an_example() -> None:
    assert set(EXAMPLES) == set(EXPORTED_SCHEMAS)


@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_json_round_trip(name: str) -> None:
    model = EXPORTED_SCHEMAS[name]
    original = EXAMPLES[name]
    as_json = original.model_dump_json()
    restored = model.model_validate_json(as_json)
    assert restored == original
    assert restored.model_dump_json() == as_json
    assert json.loads(as_json)["schema_version"] == "1.0"


@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_json_validates_against_exported_json_schema(name: str) -> None:
    schema = json_schema_for(name)
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(json.loads(EXAMPLES[name].model_dump_json()), schema)


@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_json_schema_rejects_unknown_fields(name: str) -> None:
    payload = json.loads(EXAMPLES[name].model_dump_json())
    payload["unexpected"] = 1
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, json_schema_for(name))
    with pytest.raises(ValidationError):
        EXPORTED_SCHEMAS[name].model_validate(payload)


def test_export_writes_one_file_per_schema(tmp_path) -> None:
    paths = export_json_schemas(tmp_path)
    assert {p.name for p in paths} == {f"{n}.schema.json" for n in EXPORTED_SCHEMAS}
    for path in paths:
        jsonschema.Draft202012Validator.check_schema(json.loads(path.read_text()))


def test_times_are_normalised_to_utc_and_naive_rejected() -> None:
    cest = timezone(timedelta(hours=2))
    bid = Bid(cfp_id="c", bidder="go2_01", cost=1.0, stamp=datetime(2026, 10, 5, 14, tzinfo=cest))
    assert bid.stamp == T0 and bid.stamp.tzinfo is UTC
    assert bid.model_dump_json().count("2026-10-05T12:00:00Z") == 1
    with pytest.raises(ValidationError, match="timezone-aware"):
        Bid(cfp_id="c", bidder="go2_01", cost=1.0, stamp=datetime(2026, 10, 5, 12))


def test_schema_version_major_mismatch_rejected() -> None:
    payload = json.loads(EXAMPLES["Bid"].model_dump_json())
    payload["schema_version"] = "2.0"
    with pytest.raises(ValidationError, match="schema_version"):
        Bid.model_validate(payload)


def test_bid_carries_exactly_one_of_cost_or_refusal() -> None:
    Bid(cfp_id="c", bidder="go2_01", refusal_reason="battery", stamp=T0)
    with pytest.raises(ValidationError):
        Bid(cfp_id="c", bidder="go2_01", stamp=T0)
    with pytest.raises(ValidationError):
        Bid(cfp_id="c", bidder="go2_01", cost=1.0, refusal_reason="battery", stamp=T0)


def test_non_finite_numbers_rejected() -> None:
    with pytest.raises(ValidationError):
        Bid(cfp_id="c", bidder="go2_01", cost=float("inf"), stamp=T0)


def test_envelope_validity_window_and_unique_tasks() -> None:
    env = EXAMPLES["Envelope"]
    data = env.model_dump()
    data["valid_until"] = data["valid_from"]
    with pytest.raises(ValidationError, match="valid_until"):
        Envelope.model_validate(data)
    data = env.model_dump()
    data["tasks"] = [data["tasks"][0], data["tasks"][0]]
    with pytest.raises(ValidationError, match="duplicate"):
        Envelope.model_validate(data)


def test_task_cannot_depend_on_itself() -> None:
    with pytest.raises(ValidationError, match="itself"):
        TaskSpec(
            task_id="t1",
            kind=TaskKind.RETURN_HOME,
            target=SectorTarget(sector_id="s"),
            est_duration_s=1,
            depends_on=["t1"],
        )


def test_task_target_is_discriminated() -> None:
    restored = TaskSpec.model_validate_json(HOLD.model_dump_json())
    assert isinstance(restored.target, PoseTarget)
    bad = json.loads(HOLD.model_dump_json())
    bad["target"]["type"] = "building"
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(bad)


def test_envelope_reply_decision_must_match_tasks() -> None:
    base = EXAMPLES["EnvelopeReply"].model_dump()
    with pytest.raises(ValidationError, match="inconsistent"):
        EnvelopeReply.model_validate({**base, "decision": "accept"})
    with pytest.raises(ValidationError, match="both accepted and refused"):
        EnvelopeReply.model_validate({**base, "refusals": {"t_survey_north": "battery"}})
