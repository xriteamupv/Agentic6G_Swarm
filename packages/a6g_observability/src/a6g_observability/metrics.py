"""Run metrics for the simulated scenarios (SPEC A1 13, A5): written to CSV per run.

- ``completion_time_s``: first ``decompose`` of the mission -> last succeeded task skill;
- ``reallocation_latency_s``: each injected ``robot_fail`` -> the next ``reallocate``;
- ``uplink_bytes``: bytes each robot sent through its uplink budget.
"""

import csv
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from a6g_common.schemas import Decision, DecisionKind

TASK_SKILLS = {"navigate_to", "follow_route", "inspect", "hold_post", "return_home"}
FIELDS = ["mission_id", "metric", "robot_id", "value"]


def _ts(d: Decision) -> float:
    return d.stamp.timestamp()


def mission_metrics(
    mission_id: str,
    decisions: Iterable[Decision],
    injections: Iterable[Mapping[str, Any]] = (),
    uplink_bytes: Mapping[str, int] | None = None,
) -> list[dict[str, Any]]:
    decisions = sorted(decisions, key=_ts)
    rows: list[dict[str, Any]] = []
    starts = [d for d in decisions if d.kind is DecisionKind.DECOMPOSE]
    done = [
        d
        for d in decisions
        if d.kind is DecisionKind.SKILL
        and d.inputs_ref.removeprefix("skill:") in TASK_SKILLS
        and (d.output.get("result") or {}).get("outcome") == "succeeded"
    ]
    if starts and done:
        rows.append(
            {
                "mission_id": mission_id,
                "metric": "completion_time_s",
                "robot_id": "",
                "value": round(_ts(done[-1]) - _ts(starts[0]), 1),
            }
        )
    reallocations = [d for d in decisions if d.kind is DecisionKind.REALLOCATE]
    for inj in injections:
        if inj.get("kind") != "robot_fail":
            continue
        after = [d for d in reallocations if _ts(d) >= inj["t"]]
        if after:
            rows.append(
                {
                    "mission_id": mission_id,
                    "metric": "reallocation_latency_s",
                    "robot_id": inj["robot_id"],
                    "value": round(_ts(after[0]) - inj["t"], 2),
                }
            )
    for rid, n in sorted((uplink_bytes or {}).items()):
        rows.append(
            {"mission_id": mission_id, "metric": "uplink_bytes", "robot_id": rid, "value": n}
        )
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerows(rows)
    return path


def stamp() -> str:
    return datetime.now().strftime("%Y%m%dT%H%M%S")
