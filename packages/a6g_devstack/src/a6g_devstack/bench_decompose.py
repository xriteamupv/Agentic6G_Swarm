"""Decomposition accuracy bench: share of the goals in
``bench/decompose/goals.yaml`` decomposed exactly, order ignored (expected task kinds,
targets and flow classes), with the knowledge server on, as the coordinator runs.

Without ``--model`` the rule-based planner decomposes (fake models, offline); with it the
coordinator model does. Exact = the plan equals ``expect`` (or one of ``expect_any``, the
defensible readings of an ambiguous goal). Kinds, targets and flow classes are also scored
on their own, to show where a plan goes wrong. KPI targets are orientative.
"""

import json
import statistics
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from a6g_common.config import A6GConfig
from a6g_common.schemas import PoseTarget, Posture, TaskSpec
from a6g_coordinator.decompose import _target_key, decompose_goal
from a6g_coordinator.flows import flow_class

from a6g_devstack.bench_kb import _KB, service_for

GOALS = Path(__file__).resolve().parents[4] / "bench" / "decompose" / "goals.yaml"
ROBOTS = {"go2_01": "go2", "go2_02": "go2", "summit_01": "summit-xl"}


def describe(task: TaskSpec, cfg: A6GConfig) -> list[str]:
    """[kind, target id, flow class]; the staging area is ``staging``."""
    key = _target_key(task.target, cfg.campus)
    if key is not None:
        target = key[1]
    elif isinstance(task.target, PoseTarget):
        s = cfg.campus.staging
        near = abs(task.target.pose.x - s.x) + abs(task.target.pose.y - s.y) < 1.0
        target = "staging" if near else "pose"
    else:
        target = "?"
    return [task.kind.value, target, flow_class(task)]


def _bag(plan: list[list[str]], fields: slice = slice(None)) -> Counter:
    return Counter(tuple(t[fields]) for t in plan)


async def run(
    cfg: A6GConfig,
    model: Any = None,
    embedder: Any = None,
    label: str = "fake",
    goals_file: Path = GOALS,
) -> dict[str, Any]:
    goals = yaml.safe_load(goals_file.read_text())
    kb = _KB(service_for(cfg.campus, cfg, embedder))
    rows = []
    for g in goals:
        tasks, *_ = await decompose_goal(
            g["text"], Posture(g["posture"]), cfg.campus, model, (), ROBOTS, (), kb=kb
        )
        plan = [describe(t, cfg) for t in tasks]
        readings = [g["expect"]] if "expect" in g else g["expect_any"]

        def match(fields: slice, plan: list[list[str]] = plan, readings=readings) -> bool:
            return any(_bag(plan, fields) == _bag(r, fields) for r in readings)

        rows.append(
            {
                **{k: g[k] for k in ("text", "posture", "group")},
                "expect": readings,
                "plan": plan,
                "exact": match(slice(None)),
                "kinds": match(slice(0, 1)),
                "targets": match(slice(1, 2)),
                "flows": match(slice(2, 3)),
            }
        )

    def acc(subset: list[dict], key: str = "exact") -> float | None:
        return round(statistics.fmean(r[key] for r in subset), 4) if subset else None

    return {
        "kpi": "UC2-KPI-9",
        "definition": "share of goals decomposed exactly (task kinds, targets and flow "
        "classes; order ignored; ambiguous goals: any defensible reading)",
        "model": label,
        "planner": "model" if model is not None else "rule-based (fake models)",
        "fidelity": "functional" if model is not None or embedder is not None else "offline",
        "goals": len(rows),
        "exact": acc(rows),
        "kinds": acc(rows, "kinds"),
        "targets": acc(rows, "targets"),
        "flows": acc(rows, "flows"),
        "by_posture": {
            p: acc([r for r in rows if r["posture"] == p])
            for p in ("routine", "interactive", "emergency")
        },
        "by_group": {
            g: acc([r for r in rows if r["group"] == g])
            for g in ("plain", "paraphrase", "ambiguous", "out_of_map", "injected")
        },
        "errors": [r for r in rows if not r["exact"]],
        "rows": rows,
        "stamp": datetime.now(UTC).isoformat(),
    }


def write(result: dict[str, Any], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"e6_kpi9_{result['model']}_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(result, indent=2))
    return path


def summary(result: dict[str, Any]) -> str:
    p, g = result["by_posture"], result["by_group"]
    return "\n".join(
        [
            f"UC2-KPI-9 ({result['model']}, {result['planner']}, {result['fidelity']}): "
            f"{result['exact']} of {result['goals']} goals exact (kinds {result['kinds']}, "
            f"targets {result['targets']}, flows {result['flows']})",
            "  by posture: " + ", ".join(f"{k} {v}" for k, v in p.items()),
            "  by group: " + ", ".join(f"{k} {v}" for k, v in g.items()),
        ]
    )
