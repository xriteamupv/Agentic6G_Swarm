"""Read the a6g configuration tree (the same files the uv services use).

The repository root is ``$A6G_ROOT`` or, with ``colcon build --symlink-install``, found by
walking up from this file to the directory holding ``config/fleet.yaml``.
"""

import math
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_DOMAINS = {"summit_01": 11, "go2_01": 21, "go2_02": 22, "go2_03": 23}


def a6g_root() -> Path:
    env = os.environ.get("A6G_ROOT")
    if env:
        return Path(env)
    for parent in Path(__file__).resolve().parents:
        if (parent / "config" / "fleet.yaml").exists():
            return parent
    raise RuntimeError(
        "a6g repository not found: set A6G_ROOT or build with colcon --symlink-install"
    )


def _load(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


@dataclass(frozen=True)
class RobotSetup:
    robot_id: str
    platform: str  # go2 | summit
    domain_id: int
    localization: str
    spawn: tuple[float, float, float]
    namespace: str  # "" (Go2) or "robot" (Summit)
    map_frame: str
    odom_frame: str
    base_frame: str
    manifest: dict
    world: str
    map_yaml: Path
    root: Path
    datum: tuple[float, float] = (0.0, 0.0)  # campus datum (lat, lon): map origin, ENU
    gnss_noise: str = "rtk"
    gnss_noise_params: dict | None = None
    gnss_dropout_in_shadows: bool = False
    shadows: tuple = ()  # coverage-shadow polygons in campus_map (approx. ENU)
    #: Driving speed limit (m/s): the profile's max_speed_mps, else the manifest's.
    max_speed: float = 0.3
    #: Go2 in-place turn rate (rad/s): the profile's turn_rate_radps; None = static walk.
    turn_rate: float | None = None


def robot_setup(robot_id: str, profile: str = "gazebo", root: Path | None = None) -> RobotSetup:
    root = root or a6g_root()
    cfg = root / "config"
    manifest = _load(cfg / "robots" / f"{robot_id}.yaml")
    profile_doc = _load(cfg / "profiles" / f"{profile}.yaml")
    prof = profile_doc.get("robots", {}).get(robot_id, {})
    fleet = _load(cfg / "fleet.yaml")
    campus = _load(cfg / fleet["campus"])
    campus_id = campus["campus_id"]
    scale = campus_scale(campus)
    world = campus_id if scale == 1.0 else f"{campus_id}_s{scale:g}".replace(".", "p")
    out_dir = (cfg / fleet["campus"]).parent / campus.get("world", {}).get(
        "output_dir", "../sim_gazebo/src/a6g_gz_worlds"
    )
    platform = manifest["platform"]
    ros = manifest.get("ros", {})
    spawn = prof.get("spawn") or {}
    f = spawn_factor(profile_doc.get("robots", {}), scale)
    namespace = ros.get("namespace", "").strip("/")
    odom = f"{namespace}_odom" if namespace else "odom"
    datum = campus.get("datum", {})
    lat0, lon0 = float(datum.get("lat_deg", 0.0)), float(datum.get("lon_deg", 0.0))
    gnss_noise = prof.get("gnss_noise", "rtk")
    return RobotSetup(
        robot_id=robot_id,
        platform=platform,
        domain_id=int(prof.get("ros_domain_id", DEFAULT_DOMAINS.get(robot_id, 30))),
        localization=prof.get("localization", "amcl_prior"),
        spawn=(
            round(float(spawn.get("x", 0.0)) * f, 3),
            round(float(spawn.get("y", 0.0)) * f, 3),
            float(spawn.get("yaw", 0.0)),
        ),
        namespace=namespace,
        map_frame=ros.get("map_frame", "map"),
        odom_frame=odom,
        base_frame=ros.get("base_frame", "base_link"),
        manifest=manifest,
        world=world,
        map_yaml=(out_dir / "maps" / f"{world}.yaml").resolve(),
        root=root,
        datum=(lat0, lon0),
        gnss_noise=gnss_noise,
        max_speed=float(prof.get("max_speed_mps") or manifest.get("max_speed_mps", 0.3)),
        turn_rate=float(prof["turn_rate_radps"]) if prof.get("turn_rate_radps") else None,
        gnss_noise_params=profile_doc.get("gnss_noise_profiles", {}).get(gnss_noise),
        gnss_dropout_in_shadows=bool(prof.get("gnss_dropout_in_shadows", False)),
        shadows=tuple(
            tuple(
                tuple(v * scale for v in _enu(lat0, lon0, lat, lon))
                for lat, lon in c["polygon_wgs84"]
            )
            for c in campus.get("coverage_shadows", [])
        ),
    )


def _enu(lat0: float, lon0: float, lat: float, lon: float) -> tuple[float, float]:
    """Local tangent-plane approximation (cm-level over the pilot sector)."""
    r = 6378137.0
    return (
        math.radians(lon - lon0) * r * math.cos(math.radians(lat0)),
        math.radians(lat - lat0) * r,
    )


def campus_scale(campus: dict) -> float:
    """``world.scale`` of the campus file, overridden by ``$A6G_WORLD_SCALE``."""
    return float(os.environ.get("A6G_WORLD_SCALE") or campus.get("world", {}).get("scale", 1.0))


#: Robots keep their real size: spawn spacing scales with the campus, but not below this.
MIN_SPAWN_SPACING_M = 1.5


def spawn_factor(robots: dict, scale: float) -> float:
    """Multiplier for the profile's spawn poses (designed at scale 1) in a scaled campus."""
    if scale == 1.0:
        return 1.0
    pts = [
        (float(r["spawn"]["x"]), float(r["spawn"]["y"])) for r in robots.values() if r.get("spawn")
    ]
    gaps = [math.dist(a, b) for i, a in enumerate(pts) for b in pts[i + 1 :] if math.dist(a, b) > 0]
    return max(scale, MIN_SPAWN_SPACING_M / min(gaps)) if gaps else scale
