"""Flows and the two-branch consistency check (SPEC A3.7, decision D-02).

Each task implies a network flow of a class (``derive``); the coordinator keeps a
``FlowRecord`` per flow in the mission twin and, before envelopes are published, checks:

* **I1**: every live task whose class is not ``telemetry`` has a flow that is requested,
  granted or degraded;
* **I2**: every flow that is not released belongs to a live task.

(I3, the robot's budget opening a class only after its grant, is enforced robot-side with
C5.) Until C5 the network branch is a stand-in that grants every request at once.
Pure functions over plain dicts: the graph stores them as they are.
"""

from datetime import datetime
from typing import Any

from a6g_common.schemas import FlowRecord, PoiTarget, SectorTarget, TaskKind, TaskSpec

#: Tasks in an envelope (they need their flow before publication).
LIVE = ("assigned", "in_progress")


def flow_class(spec: TaskSpec) -> str:
    """The flow a task implies (SPEC A3.7 table)."""
    if spec.kind is TaskKind.SURVEY_SECTOR:
        return "survey_uplink"
    if spec.kind is TaskKind.ESCORT:
        return "conversational"
    if spec.kind is TaskKind.HOLD_POST:
        if spec.sensing_mode == "state_only":  # no sensing stream the network can carry
            return "telemetry"
        return "video_control" if spec.sensing_mode == "video" else "telemetry_plus"
    return "telemetry"  # inspect, go_to, return_home: state and A2A only


def location(spec: TaskSpec) -> str:
    t = spec.target
    if isinstance(t, PoiTarget):
        return t.poi_id
    if isinstance(t, SectorTarget):
        return t.sector_id
    return "pose"


def derive(
    tasks: dict[str, dict[str, Any]], flows: dict[str, dict[str, Any]], now: datetime
) -> tuple[list[dict[str, Any]], list[str]]:
    """Bring the flow records in line with the live tasks: (new requests, released ids).
    Mutates ``tasks`` (``spec.flow_class``) and ``flows``."""
    requests: list[dict[str, Any]] = []
    wanted: set[str] = set()
    for tid, t in sorted(tasks.items()):
        spec = TaskSpec.model_validate(t["spec"])
        cls = flow_class(spec)
        t["spec"]["flow_class"] = cls
        if cls == "telemetry" or t["status"] not in LIVE or not t.get("robot_id"):
            continue
        fid = f"flow-{tid}-{t['robot_id']}"[:120]  # the idempotency key (NFR-A6G-12)
        wanted.add(fid)
        if fid in flows and flows[fid]["state"] != "released":
            continue
        record = FlowRecord(
            flow_id=fid,
            task_id=tid,
            robot_id=t["robot_id"],
            flow_class=cls,  # type: ignore[arg-type]
            location=location(spec),
            requested_at=now,
        ).model_dump(mode="json")
        flows[fid] = record
        requests.append(record)
    released = []
    for fid, f in sorted(flows.items()):
        if f["state"] != "released" and fid not in wanted:
            f.update(state="released", reason="its task is no longer live")
            released.append(fid)
    return requests, released


def check(tasks: dict[str, dict[str, Any]], flows: dict[str, dict[str, Any]]) -> dict[str, list]:
    """The invariants: {"i1": tasks without a flow, "i2": flows without a task}."""
    held = {
        f["task_id"] for f in flows.values() if f["state"] in ("requested", "granted", "degraded")
    }
    i1 = sorted(
        tid
        for tid, t in tasks.items()
        if t["status"] in LIVE
        and t.get("robot_id")
        and flow_class(TaskSpec.model_validate(t["spec"])) != "telemetry"
        and tid not in held
    )
    i2 = sorted(
        fid
        for fid, f in flows.items()
        if f["state"] != "released"
        and (
            f["task_id"] not in tasks
            or tasks[f["task_id"]]["status"] not in LIVE
            or tasks[f["task_id"]].get("robot_id") != f["robot_id"]
        )
    )
    return {"i1": i1, "i2": i2}
