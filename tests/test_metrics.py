from datetime import UTC, datetime, timedelta
from pathlib import Path

from a6g_common.schemas import Decision, DecisionKind
from a6g_observability.metrics import mission_metrics, write_csv

T0 = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)


def d(kind, seconds, **kw):
    return Decision(
        decision_id=f"d{seconds}{kind}",
        actor="x",
        kind=kind,
        mission_id="m1",
        stamp=T0 + timedelta(seconds=seconds),
        **kw,
    )


def test_mission_metrics_and_csv(tmp_path: Path) -> None:
    decisions = [
        d(DecisionKind.DECOMPOSE, 0),
        d(DecisionKind.SKILL, 100, inputs_ref="skill:follow_route",
          output={"result": {"outcome": "succeeded"}}),
        d(DecisionKind.REALLOCATE, 130),
        d(DecisionKind.SKILL, 400, inputs_ref="skill:follow_route",
          output={"result": {"outcome": "succeeded"}}),
        d(DecisionKind.SKILL, 500, inputs_ref="skill:get_state",
          output={"result": {"outcome": "succeeded"}}),
    ]  # fmt: skip
    fail = {
        "kind": "robot_fail",
        "robot_id": "go2_01",
        "t": (T0 + timedelta(seconds=120)).timestamp(),
    }
    rows = mission_metrics("m1", decisions, [fail], {"go2_01": 1200, "summit_01": 900})
    by = {(r["metric"], r["robot_id"]): r["value"] for r in rows}
    assert by[("completion_time_s", "")] == 400.0
    assert by[("reallocation_latency_s", "go2_01")] == 10.0
    assert by[("uplink_bytes", "summit_01")] == 900
    path = write_csv(tmp_path / "m.csv", rows)
    write_csv(path, rows)
    lines = path.read_text().splitlines()
    assert lines[0] == "mission_id,metric,robot_id,value" and len(lines) == 1 + 2 * len(rows)
