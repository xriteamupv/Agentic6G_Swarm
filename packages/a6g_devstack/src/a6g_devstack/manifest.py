"""Result manifests (SPEC A4 rule 31): every result file says what produced it.

The commit and dirty flag, the command line, hashes of the configuration files, seeds,
models with their fidelity (rule 25), the network mode, the coverage source (rule 32), the
simulator's time factor and the host. ``a6g paper export`` flags results whose commit is
not ``HEAD`` as possibly stale.
"""

import hashlib
import os
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from a6g_common.config import A6GConfig

REPO = Path(__file__).resolve().parents[4]


def _git(*args: str) -> str:
    try:
        out = subprocess.run(
            ["git", *args], cwd=REPO, capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def git_state() -> dict[str, Any]:
    commit = _git("rev-parse", "HEAD")
    # results and paper exports are outputs, not inputs: they do not make the tree dirty
    dirty = _git("status", "--porcelain", "--", ".", ":!bench/results", ":!paper")
    return {"commit": commit or None, "dirty": bool(dirty) if commit else None}


def config_hashes(cfg: A6GConfig) -> dict[str, str]:
    """SHA-256 (first 16 hex) of every configuration file, relative to the config dir."""
    root = Path(cfg.config_dir)
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in (".yaml", ".yml", ".json", ".csv"):
            out[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    return out


def network_info(cfg: A6GConfig) -> dict[str, Any]:
    """Network mode and coverage source (rule 32); always_grant / shadows before stage N3."""
    net = getattr(cfg, "network", None)
    if net is None:
        return {"network": "stand-in", "mode": "always_grant", "coverage_source": "shadows"}
    info = {
        "network": "emulated" if net.mode != "always_grant" else "stand-in",
        "mode": net.mode,
        "coverage_source": net.coverage.source,
        "coverage_calibration": net.coverage.calibration,
    }
    provenance = getattr(net, "provenance", None)
    if provenance:
        info["provenance"] = provenance
    return info


def build(
    cfg: A6GConfig,
    *,
    seeds: Any = None,
    models: dict[str, Any] | None = None,
    time_factor: Any = None,
    network: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """``models``: role -> {"model": name, "fidelity": "offline" | "functional" | ...}."""
    return {
        **git_state(),
        "command": " ".join([Path(sys.argv[0]).name, *sys.argv[1:]]),
        "config_dir": str(cfg.config_dir),
        "config_sha256": config_hashes(cfg),
        "seeds": seeds,
        "models": models or {"all": {"model": "fake", "fidelity": "offline"}},
        **(network if network is not None else network_info(cfg)),
        "time_factor": time_factor,
        "host": {
            "name": platform.node(),
            "os": platform.platform(),
            "python": platform.python_version(),
            "cpus": os.cpu_count(),
        },
        "written": datetime.now(UTC).isoformat(),
        **(extra or {}),
    }


def models_of(cfg: A6GConfig, **roles: Any) -> dict[str, Any]:
    """{role: {model, fidelity}} for the roles a run used (None: the fake model)."""
    out: dict[str, Any] = {}
    for role, used in roles.items():
        if not used:
            out[role] = {"model": "fake", "fidelity": "offline"}
            continue
        endpoint = cfg.models.roles.get(role)
        out[role] = {
            "model": getattr(endpoint, "model", str(used)),
            "fidelity": getattr(endpoint, "fidelity", None) or "functional",
            "endpoint": "self-hosted" if getattr(endpoint, "base_url", None) else "cloud",
        }
    return out
