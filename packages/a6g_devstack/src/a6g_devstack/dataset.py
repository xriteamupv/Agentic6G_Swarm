"""Dataset DS09 (SPEC A3.8): ``a6g dataset export --run <dir> --out <dir>``.

JSON files with a schema, from a run directory (``a6g bench g10`` or a stack's workdir):

* ``telemetry.json``: pose, battery and link per robot over time;
* ``mission.json``: goals, allocations, flow records and grants;
* ``perception.json``: what the robots' perception escalated (label, confidence, evidence
  hash and model) and the statuses written from it; finding fusion is stage E4;
* ``failures.json``: link losses, failure detection and recovery;
* ``kpi.json``: the KPI table (``a6g kpi report``);
* ``metadata.json``: SNS JU Common Metadata Template fields (TODO(UPV): mapping with D6.1);
* ``schema.json``: what each file holds.

Interaction content never reaches the export (rule 28): decisions about an interaction
are left out, except the forget record's timings and booleans. Images are excluded by
default; ``with_images`` copies only those whose observation had no person in view.
"""

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from a6g_common.config import A6GConfig
from a6g_common.schemas import Decision, DecisionKind

from a6g_devstack import kpi_report

#: Never exported, whatever decision carries it (rule 28).
PRIVATE_KEYS = frozenset(
    {
        "utterance",
        "declared_constraint",
        "constraint",
        "profile",
        "content",
        "place_said",
        "destination",
        "meeting_pose",
        "text",
    }
)
FORGET_FIELDS = (
    "outcome",
    "deleted",
    "request_to_confirmation_s",
    "request_to_departure_s",
    "p7_round_trip_s",
    "route_compliant",
    "handovers",
    "repeated_requests",
)


def _decisions(run_dir: Path, mission_id: str | None) -> list[Decision]:
    path = run_dir / "decisions.jsonl"
    out = [Decision.model_validate_json(x) for x in path.read_text().splitlines() if x.strip()]
    return sorted(
        (d for d in out if mission_id is None or d.mission_id in (mission_id, None)),
        key=lambda d: d.stamp,
    )


def _public(d: Decision) -> dict[str, Any] | None:
    """The decision as exported, or None when it is about an interaction."""
    if d.kind is DecisionKind.FORGET:
        out = {k: d.output.get(k) for k in FORGET_FIELDS}
    elif d.output.get("interaction_id") or d.policy.get("rule") == "human_interaction":
        return None
    else:
        out = {k: v for k, v in d.output.items() if k not in PRIVATE_KEYS}
    return {
        "decision_id": d.decision_id,
        "stamp": d.stamp.isoformat(),
        "actor": d.actor,
        "kind": d.kind.value,
        "mission_id": d.mission_id,
        "output": out,
        "policy": {k: v for k, v in d.policy.items() if k not in PRIVATE_KEYS},
    }


SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Agentic6G UC2 DS09 swarm run",
    "type": "object",
    "files": {
        "telemetry.json": "array of {stamp, robot_id, x, y, battery_pct, link, failed} "
        "(campus_map frame, metres)",
        "mission.json": "{goals, allocations, flows, grants}: decisions (decision_id, "
        "stamp, actor, kind, mission_id, output, policy)",
        "perception.json": "{escalations, status_updates, findings}",
        "failures.json": "{link_events: [{robot, lost, recovered, recovery_s, detected, "
        "replanned}], failure_decisions}",
        "kpi.json": "the KPI table of a6g kpi report",
        "metadata.json": "SNS JU Common Metadata Template fields",
        "images/": "optional (with_images): images without a person in view",
    },
}


def export(
    run_dir: Path,
    out_dir: Path,
    cfg: A6GConfig,
    mission_id: str | None = None,
    with_images: bool = False,
    images_dir: Path | None = None,
) -> dict[str, Any]:
    run = json.loads((run_dir / "run.json").read_text()) if (run_dir / "run.json").exists() else {}
    mission_id = mission_id or run.get("mission_id")
    decisions = _decisions(run_dir, mission_id)
    public = [p for d in decisions if (p := _public(d)) is not None]

    def of(*kinds: DecisionKind) -> list[dict]:
        names = {k.value for k in kinds}
        return [p for p in public if p["kind"] in names]

    out_dir.mkdir(parents=True, exist_ok=True)
    telemetry = []
    if (run_dir / "telemetry.jsonl").exists():
        telemetry = [json.loads(x) for x in (run_dir / "telemetry.jsonl").read_text().splitlines()]
    escalations = of(DecisionKind.ESCALATE)
    files: dict[str, Any] = {
        "telemetry.json": telemetry,
        "mission.json": {
            "goals": of(DecisionKind.DECOMPOSE),
            "allocations": of(DecisionKind.ALLOCATE, DecisionKind.REALLOCATE, DecisionKind.AWARD),
            "flows": [p for p in of(DecisionKind.RECONCILE) if "kpi" not in p["output"]],
            "grants": {"network_branch": "stand-in, grants every request (until C5)"},
        },
        "perception.json": {
            "escalations": escalations,
            "status_updates": of(DecisionKind.STATUS_UPDATE),
            "findings": "n/a: finding fusion is stage E4",
        },
        "failures.json": {
            "link_events": [
                {
                    **e,
                    "lost": e["lost"].isoformat(),
                    "recovered": e["recovered"] and e["recovered"].isoformat(),
                }
                for e in kpi_report.link_events(decisions)
            ],
            "failure_decisions": [
                p for p in of(DecisionKind.FALLBACK) if p["policy"].get("rule") == "robot_failure"
            ],
        },
        "schema.json": SCHEMA,
    }
    if run:
        kpi = kpi_report.report(run_dir, cfg)
        files["kpi.json"] = kpi
    copied = []
    if with_images:
        source = images_dir or run_dir / "images"
        for e in escalations:
            uri = (e["output"].get("evidence") or {}).get("image_uri")
            if not uri or e["output"].get("person_present"):
                continue  # rule 28: never an image with a person in view
            name = Path(uri.split("://", 1)[-1]).name
            if (source / name).exists():
                (out_dir / "images").mkdir(exist_ok=True)
                shutil.copy2(source / name, out_dir / "images" / name)
                copied.append(name)
    files["metadata.json"] = {
        "template": "SNS JU Common Metadata Template (TODO(UPV): field mapping with D6.1)",
        "identifier": f"DS09-{run.get('scenario', 'run')}-{run.get('seed', '')}-{mission_id}",
        "title": "Agentic6G UC2 swarm robotics run (Agentic Campus Guardian)",
        "description": "Telemetry, mission record, perception, failures and KPIs of one run "
        "of the UC2 robot swarm. No interaction content (rule 28).",
        "creator": "UPV (Agentic6G UC2)",
        "project": "Agentic6G",
        "use_case": "UC2",
        "created": datetime.now(UTC).isoformat(),
        "fidelity": "functional (2D simulator)",
        "scenario": run.get("scenario"),
        "seed": run.get("seed"),
        "mission_id": mission_id,
        "records": {k: len(v) if isinstance(v, list) else None for k, v in files.items()},
        "images": copied,
        "format": "application/json",
        "license": "TODO(UPV)",
    }
    for name, content in files.items():
        (out_dir / name).write_text(json.dumps(content, indent=2, default=str))
    return {"out": str(out_dir), "files": sorted(files), "images": copied}
