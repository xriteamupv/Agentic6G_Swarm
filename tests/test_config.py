"""Config loads, and validation fails loudly on a bad manifest."""

import json
from pathlib import Path

import pytest
import yaml
from a6g_cli.main import app
from a6g_common.config import (
    ConfigError,
    LinkLossPolicy,
    Motion,
    load_config,
    load_manifest,
)
from a6g_common.schemas import Platform, SkillName
from typer.testing import CliRunner

runner = CliRunner()


def _edit_yaml(path: Path, mutate) -> None:
    data = yaml.safe_load(path.read_text())
    mutate(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def test_repository_config_loads(config_dir: Path) -> None:
    cfg = load_config(config_dir, env={})
    assert set(cfg.manifests) == {"go2_01", "summit_01"}
    go2, summit = cfg.manifests["go2_01"], cfg.manifests["summit_01"]
    assert go2.platform is Platform.GO2 and go2.agent_host == "onboard"
    assert summit.platform is Platform.SUMMIT and summit.agent_host == "edge_proxy"
    assert Motion.REVERSE not in summit.forbidden_motions  # short, slow reverses only
    assert summit.reverse_limits is not None and summit.reverse_limits.max_speed_mps <= 0.2
    assert summit.battery_reliable is False
    assert summit.error_code_map[208] == "no_path"
    assert set(go2.skill_timeouts) == set(SkillName)
    assert cfg.fleet.defaults.link_loss_policy is LinkLossPolicy.HOLD_THEN_RECOVER
    assert cfg.fleet.budget_for("go2_01").sustained_mbps == 3.0
    assert cfg.fleet.robot("go2_01").socket_path == "/run/a6g/go2_01.sock"
    assert cfg.campus.poi("poi_bike_rack").exposed


def test_env_interpolation(config_dir: Path) -> None:
    cfg = load_config(config_dir, env={"A6G_MQTT_HOST": "10.0.0.5"})
    assert cfg.fleet.edge.mqtt.host == "10.0.0.5"
    assert load_config(config_dir, env={}).fleet.edge.mqtt.host == "127.0.0.1"


# Each case mutates go2_01 or summit_01 and must raise ConfigError naming file + field.
BAD_MANIFESTS = {
    "unknown_field": ("go2_01", lambda d: d.update(max_sped_mps=1.0), "max_sped_mps"),
    "unknown_capability": (
        "go2_01",
        lambda d: d["capabilities"].append("flying"),
        "capabilities",
    ),
    "negative_speed": ("go2_01", lambda d: d.update(max_speed_mps=-0.3), "max_speed_mps"),
    "missing_skill_timeout": (
        "go2_01",
        lambda d: d["skill_timeouts"].pop("navigate_to"),
        "navigate_to",
    ),
    "unknown_skill_timeout": (
        "go2_01",
        lambda d: d["skill_timeouts"].update(publish_topic=5),
        "publish_topic",
    ),
    "summit_unlimited_reverse": (
        "summit_01",
        lambda d: d.pop("reverse_limits"),
        "reverse",
    ),
    "summit_fast_reverse": (
        "summit_01",
        lambda d: d["reverse_limits"].update(max_speed_mps=0.5),
        "max_speed_mps",
    ),
    "summit_long_reverse": (
        "summit_01",
        lambda d: d["reverse_limits"].update(max_distance_m=3.0),
        "max_distance_m",
    ),
    "forbidden_lowcmd_topic": (
        "go2_01",
        lambda d: d["ros"].update(pose_topic="/lowcmd"),
        "/lowcmd",
    ),
    "forbidden_bashrunner": (
        "go2_01",
        lambda d: d["ros"]["stop"].update(topic="/api/bashrunner/request"),
        "bashrunner",
    ),
    "sport_api_other_than_stopmove": (
        "go2_01",
        lambda d: d["ros"]["stop"].update(api_id=1008),
        "1003",
    ),
    "bad_error_code_outcome": (
        "summit_01",
        lambda d: d["error_code_map"].update({999: "exploded"}),
        "error_code_map",
    ),
    "bad_nav_mode": ("go2_01", lambda d: d.update(nav_mode="teleport"), "nav_mode"),
    "missing_platform": ("go2_01", lambda d: d.pop("platform"), "platform"),
}


@pytest.mark.parametrize("case", sorted(BAD_MANIFESTS))
def test_bad_manifest_fails_loudly(config_copy: Path, case: str) -> None:
    robot_id, mutate, needle = BAD_MANIFESTS[case]
    path = config_copy / "robots" / f"{robot_id}.yaml"
    _edit_yaml(path, mutate)
    with pytest.raises(ConfigError) as exc_info:
        load_manifest(path)
    message = str(exc_info.value)
    assert str(path) in message
    assert needle in message
    # The whole tree fails too, and so does the CLI (exit code 1, error on stderr).
    with pytest.raises(ConfigError):
        load_config(config_copy, env={})
    result = runner.invoke(app, ["config", "validate", "-c", str(config_copy)])
    assert result.exit_code == 1
    assert needle in result.stderr


def test_duplicate_yaml_key_rejected(config_copy: Path) -> None:
    path = config_copy / "robots" / "go2_01.yaml"
    path.write_text(path.read_text() + "\nclearance_m: 0.1\n")
    with pytest.raises(ConfigError, match="duplicate key 'clearance_m'"):
        load_manifest(path)


def test_unset_env_var_without_default_rejected(config_copy: Path) -> None:
    path = config_copy / "robots" / "go2_01.yaml"
    _edit_yaml(path, lambda d: d.update(camera_topic="${A6G_NOT_SET_ANYWHERE}"))
    with pytest.raises(ConfigError, match="A6G_NOT_SET_ANYWHERE"):
        load_manifest(path, env={})


def test_manifest_robot_id_must_match_fleet(config_copy: Path) -> None:
    _edit_yaml(config_copy / "robots" / "go2_01.yaml", lambda d: d.update(robot_id="go2_99"))
    with pytest.raises(ConfigError, match="go2_99"):
        load_config(config_copy, env={})


def test_fleet_unknown_peer_rejected(config_copy: Path) -> None:
    _edit_yaml(config_copy / "fleet.yaml", lambda d: d["robots"][0].update(peers=["ghost_01"]))
    with pytest.raises(ConfigError, match="ghost_01"):
        load_config(config_copy, env={})


CAMPUS = "upv_vera_cpi.yaml"


def test_campus_v2_derives_enu_geometry(config_dir: Path) -> None:
    campus = load_config(config_dir, env={}).campus
    # Staging sits on the footpath nearest the datum (footpaths only).
    assert (campus.staging.x, campus.staging.y) == pytest.approx((23.377, -36.583), abs=0.01)
    assert campus.datum and campus.datum["lat_deg"] == pytest.approx(39.4788828)
    assert len(campus.obstacles) > 20  # OSM building footprints (+ trees) for the 2D sim
    kiosk = campus.poi("poi_kiosk").pose
    assert (kiosk.x, kiosk.y) == pytest.approx((-154.166, -13.899), abs=0.01)
    assert len(campus.pilot_area) == 4  # the rotated pilot rectangle


def test_campus_poi_outside_pilot_sector_rejected(config_copy: Path) -> None:
    _edit_yaml(config_copy / "campus" / CAMPUS, lambda d: d["pois"][0].update(lat=40.0))
    with pytest.raises(ConfigError, match="outside the pilot sector"):
        load_config(config_copy, env={})


#: a legacy shadow polygon (the campus has none: coverage is emulated)
LEGACY_SHADOW = {
    "id": "shadow_east",
    "link_quality": "lost",
    "polygon_wgs84": [
        [39.4782885, -0.3341788],
        [39.4780638, -0.3334241],
        [39.4788163, -0.3330652],
        [39.4790278, -0.3338341],
    ],
}


def test_campus_self_intersecting_polygon_rejected(config_copy: Path) -> None:
    def bow_tie(d):
        d["coverage_shadows"] = [json.loads(json.dumps(LEGACY_SHADOW))]
        p = d["coverage_shadows"][0]["polygon_wgs84"]
        p[1], p[2] = p[2], p[1]

    _edit_yaml(config_copy / "campus" / CAMPUS, bow_tie)
    with pytest.raises(ConfigError, match="self-intersecting"):
        load_config(config_copy, env={})


def test_campus_point_inside_a_building_rejected(config_copy: Path) -> None:
    from a6g_common.geo import Datum

    lat, lon, _ = Datum(39.4788828, -0.3349505, 10.0).to_wgs84(-30, 72)  # inside building 9B
    _edit_yaml(config_copy / "campus" / CAMPUS, lambda d: d["pois"][0].update(lat=lat, lon=lon))
    with pytest.raises(ConfigError, match="inside a building"):
        load_config(config_copy, env={})


def test_shadow_needs_a_reachable_recovery_point(config_copy: Path) -> None:
    _edit_yaml(
        config_copy / "campus" / CAMPUS,
        lambda d: d.update(link_recovery_points=[], coverage_shadows=[LEGACY_SHADOW]),
    )
    with pytest.raises(ConfigError, match="no reachable link recovery point"):
        load_config(config_copy, env={})


def test_models_vlm_roles_must_support_vision(config_copy: Path) -> None:
    _edit_yaml(
        config_copy / "models.yaml",
        lambda d: d["roles"]["edge_vlm"].update(supports_vision=False),
    )
    with pytest.raises(ConfigError, match="edge_vlm"):
        load_config(config_copy, env={})


def test_cli_validate_ok(config_dir: Path) -> None:
    result = runner.invoke(app, ["config", "validate", "-c", str(config_dir)])
    assert result.exit_code == 0, result.output
    assert "OK fleet upv_pilot" in result.stdout


# --------------------------------------------------------------------------- model endpoints


def test_real_profile_rejects_cloud_vlm_endpoints(config_dir: Path) -> None:
    with pytest.raises(ConfigError) as exc:
        load_config(config_dir, env={}, profile="real")
    message = str(exc.value)
    assert "roles.edge_vlm" in message and "roles.onboard_vlm" in message
    assert "coordinator_llm" not in message  # text-only role: cloud allowed by the profile


def test_real_profile_accepts_on_premises_vlms_and_rejects_public_urls(config_dir: Path) -> None:
    local = {
        "A6G_EDGE_VLM_URL": "http://192.168.1.50:8081/v1",
        "A6G_ONBOARD_VLM_URL": "http://localhost:8082/v1",
        "A6G_EMBEDDER_URL": "http://localhost:8083/v1",
    }
    cfg = load_config(config_dir, env=local, profile="real")
    assert cfg.profile is not None and cfg.profile.profile == "real"
    public = {**local, "A6G_EDGE_VLM_URL": "https://8.8.8.8/v1"}
    with pytest.raises(ConfigError, match=r"roles\.edge_vlm"):
        load_config(config_dir, env=public, profile="real")


@pytest.mark.parametrize("profile", ["sim", "gazebo"])
def test_simulation_profiles_allow_cloud_models(config_dir: Path, profile: str) -> None:
    assert load_config(config_dir, env={}, profile=profile).profile.profile == profile


def test_cli_validate_with_profile(config_dir: Path) -> None:
    result = runner.invoke(app, ["config", "validate", "-c", str(config_dir), "--profile", "real"])
    assert result.exit_code == 1 and "on premises" in result.stderr


# --------------------------------------------------------------------------- deployment profiles


def _profile(profile: str, **robots: dict) -> dict:
    return {"schema_version": "1.0", "profile": profile, "robots": robots}


def test_gazebo_profile_robots(config_dir: Path) -> None:
    cfg = load_config(config_dir, env={}, profile="gazebo")
    robots = cfg.profile.robots
    assert {r: p.ros_domain_id for r, p in robots.items()} == {
        "summit_01": 11,
        "go2_01": 21,
        "go2_02": 22,
        "go2_03": 23,
    }
    assert all(p.use_sim_time and p.spawn is not None for p in robots.values())


def test_profile_rejects_shared_domains_and_wrong_sim_time() -> None:
    from a6g_common.config import ProfileConfig

    with pytest.raises(ValueError, match="already used by go2_01"):
        ProfileConfig.model_validate(
            _profile(
                "gazebo",
                go2_01={"ros_domain_id": 21, "use_sim_time": True},
                go2_02={"ros_domain_id": 21, "use_sim_time": True},
            )
        )
    with pytest.raises(ValueError, match="use_sim_time must be False"):
        ProfileConfig.model_validate(
            _profile("real", go2_01={"ros_domain_id": 21, "use_sim_time": True})
        )


def test_ground_truth_localization_is_simulation_only() -> None:
    from a6g_common.config import ProfileConfig

    gt = {"ros_domain_id": 11, "use_sim_time": False, "localization": "ground_truth"}
    with pytest.raises(ValueError, match="ground_truth is not allowed with profile real"):
        ProfileConfig.model_validate(_profile("real", summit_01=gt))
    ok = ProfileConfig.model_validate(_profile("gazebo", summit_01={**gt, "use_sim_time": True}))
    assert ok.robots["summit_01"].localization == "ground_truth"


def test_gnss_noise_profiles(config_dir: Path) -> None:
    from a6g_common.config import ProfileConfig

    cfg = load_config(config_dir, env={}, profile="gazebo")
    noise = cfg.profile.gnss_noise_profiles
    assert noise["rtk"].sigma_m == pytest.approx(0.02)
    std = noise["standard"]
    assert (std.sigma_m**2 + std.bias_sigma_m**2) ** 0.5 == pytest.approx(2.5)
    assert noise["degraded"].dropout_per_min > 0
    assert all(not r.gnss_dropout_in_shadows for r in cfg.profile.robots.values())
    bad = _profile("gazebo", go2_01={"ros_domain_id": 21, "use_sim_time": True, "gnss_noise": "x"})
    bad["gnss_noise_profiles"] = {"rtk": {"sigma_m": 0.02}}
    with pytest.raises(ValueError, match="gnss_noise 'x'"):
        ProfileConfig.model_validate(bad)


def test_adapter_reads_covariance_from_the_localization_in_use(config_dir: Path) -> None:
    from a6g_common.config import adapter_config

    cfg = load_config(config_dir, env={}, profile="gazebo")
    # Outdoors every robot localizes with GNSS (the gazebo profile default) ...
    assert adapter_config(cfg, "summit_01")["ros"]["pose_topic"] == "/robot/odometry/global"
    # ... AMCL on a prior map remains for indoor maps and single-robot tests.
    amcl = adapter_config(cfg, "summit_01", localization="amcl_prior")
    assert amcl["ros"]["pose_topic"] == "/robot/amcl_pose"
    assert adapter_config(cfg, "go2_01", localization="gnss_ekf_map")["ros"]["pose_topic"] == (
        "/odometry/global"
    )


def test_profile_max_speed_drives_and_plans(config_copy: Path) -> None:
    _edit_yaml(
        config_copy / "profiles" / "gazebo.yaml",
        lambda d: d["robots"]["summit_01"].update(max_speed_mps=0.8),
    )
    cfg = load_config(config_copy, env={}, profile="gazebo")
    assert cfg.manifests["summit_01"].max_speed_mps == 0.8  # the allocator plans with it
    assert cfg.manifests["go2_01"].max_speed_mps == 0.30  # unset: the manifest's value
    assert load_config(config_copy, env={}).manifests["summit_01"].max_speed_mps == 0.5


def test_real_robots_can_only_be_slowed_down(config_copy: Path) -> None:
    env = {
        "A6G_EDGE_VLM_URL": "http://localhost:1/v1",
        "A6G_ONBOARD_VLM_URL": "http://localhost:2/v1",
        "A6G_EMBEDDER_URL": "http://localhost:3/v1",
    }
    real = config_copy / "profiles" / "real.yaml"
    _edit_yaml(real, lambda d: d["robots"]["go2_01"].update(max_speed_mps=0.2))
    assert (
        load_config(config_copy, env=env, profile="real").manifests["go2_01"].max_speed_mps == 0.2
    )
    _edit_yaml(real, lambda d: d["robots"]["go2_01"].update(max_speed_mps=0.5))
    with pytest.raises(ConfigError, match="can only be slowed down"):
        load_config(config_copy, env=env, profile="real")
    _edit_yaml(real, lambda d: d["robots"]["go2_01"].update(max_speed_mps=1.5))
    with pytest.raises(ConfigError, match="max_speed_mps"):
        load_config(config_copy, env=env, profile="real")


def test_go2_turn_rate_is_a_gazebo_only_override(config_copy: Path) -> None:
    gz = load_config(config_copy, env={}, profile="gazebo")
    assert gz.manifests["go2_01"].turn_in_place_s_per_rad == 2.0  # 0.5 rad/s in Gazebo
    assert load_config(config_copy, env={}).manifests["go2_01"].turn_in_place_s_per_rad == 9.55
    env = {
        "A6G_EDGE_VLM_URL": "http://localhost:1/v1",
        "A6G_ONBOARD_VLM_URL": "http://localhost:2/v1",
        "A6G_EMBEDDER_URL": "http://localhost:3/v1",
    }
    real = config_copy / "profiles" / "real.yaml"
    _edit_yaml(real, lambda d: d["robots"]["go2_01"].update(turn_rate_radps=0.5))
    with pytest.raises(ConfigError, match="simulation-only"):
        load_config(config_copy, env=env, profile="real")
