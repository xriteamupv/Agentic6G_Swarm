import math

import pytest
from a6g_gz_bringup.bridges import GZ_TO_ROS, ROS_TO_GZ, go2_bridge, namespaced, summit_bridge
from a6g_gz_bringup.frames import Pose2, map_to_odom
from a6g_gz_bringup.mux import PriorityMux


def test_map_to_odom_recovers_ground_truth():
    world_base = Pose2(45.0, -12.0, -math.pi / 2)
    odom_base = Pose2(3.0, 1.0, 0.4)  # drifted leg odometry
    m = map_to_odom(world_base, odom_base)
    est = m * odom_base
    assert (est.x, est.y) == pytest.approx((45.0, -12.0))
    assert est.yaw == pytest.approx(-math.pi / 2)


def test_priority_mux_stop_path_wins():
    mux = PriorityMux({"nav": 10, "stop": 40}, {"nav": 0.5, "stop": 1.0})
    assert mux.accept("nav", 0.0)
    assert mux.accept("stop", 0.1)
    assert not mux.accept("nav", 0.2)  # web_teleop zero twist overrides Nav2
    assert not mux.accept("nav", 1.0)
    assert mux.accept("nav", 1.2)  # stop input timed out


def _by_ros(entries):
    return {e["ros_topic_name"]: e for e in entries}


def test_go2_bridge_uses_dock_names_and_sim_namespace():
    b = _by_ros(go2_bridge("upv_vera_cpi", "go2_01"))
    assert {"/clock", "/utlidar/cloud", "/utlidar/imu", "/camera/image_raw"} <= b.keys()
    assert b["/a6g_sim/proxy_cmd_vel"]["direction"] == ROS_TO_GZ
    assert b["/a6g_sim/proxy_cmd_vel"]["gz_topic_name"] == "/model/go2_01/cmd_vel"
    assert all(e["direction"] == GZ_TO_ROS for t, e in b.items() if t != "/a6g_sim/proxy_cmd_vel")
    # Only this model's topics (plus /clock) are bridged.
    assert all("go2_01" in e["gz_topic_name"] for t, e in b.items() if t != "/clock")


def test_summit_bridge_matches_the_robot():
    b = _by_ros(summit_bridge("summit_01"))
    assert b["/robot/robotnik_base_control/cmd_vel_unstamped"]["direction"] == ROS_TO_GZ
    assert "/robot/top_3d_laser/points" in b
    assert "/robot/front_camera/color/image_raw" in b
    assert b["/tf"]["gz_topic_name"] == "/model/summit_01/tf_wheels"


def test_namespaced_params():
    assert namespaced({"amcl": {}}, "robot") == {"robot": {"amcl": {}}}
    assert namespaced({"amcl": {}}, "") == {"amcl": {}}


def _nav2():
    return {
        "controller_server": {
            "ros__parameters": {
                "FollowPath": {
                    "desired_linear_vel": 0.5,
                    "regulated_linear_scaling_min_speed": 0.1,
                    "min_approach_linear_velocity": 0.08,
                }
            }
        },
        "velocity_smoother": {
            "ros__parameters": {"max_velocity": [0.5, 0.0, 0.8], "min_velocity": [0.0, 0.0, -0.8]}
        },
    }


def test_max_speed_sets_nav2_and_caps_summit_reverse():
    from a6g_gz_bringup.speed import SUMMIT_REVERSE_MAX, apply_max_speed

    nav2 = _nav2()
    nav2["controller_server"]["ros__parameters"]["FollowPath"] = {
        "plugin": "nav2_mppi_controller::MPPIController",
        "vx_max": 0.5,
        "vx_min": -0.15,
    }
    p = apply_max_speed(nav2, "summit", 0.2)
    mppi = p["controller_server"]["ros__parameters"]["FollowPath"]
    assert mppi["vx_max"] == 0.2 and mppi["vx_min"] == -SUMMIT_REVERSE_MAX
    vs = p["velocity_smoother"]["ros__parameters"]
    assert vs["max_velocity"] == [0.2, 0.0, 0.8]
    assert vs["min_velocity"] == [-SUMMIT_REVERSE_MAX, 0.0, -0.8]
    slow = apply_max_speed(_nav2(), "summit", 0.1)  # RPP dict, reverse never above max
    assert slow["velocity_smoother"]["ros__parameters"]["min_velocity"][0] == -0.1
    g = apply_max_speed(_nav2(), "go2", 0.05)
    rpp = g["controller_server"]["ros__parameters"]["FollowPath"]
    assert rpp["regulated_linear_scaling_min_speed"] == 0.05  # never above the limit
    assert rpp["min_approach_linear_velocity"] == 0.05
    assert g["velocity_smoother"]["ros__parameters"]["min_velocity"][0] == -0.05


def test_max_speed_range():
    from a6g_gz_bringup.speed import check_speed

    for bad in (0.0, -0.1, 1.5):
        with pytest.raises(ValueError):
            check_speed("summit", bad)
    check_speed("go2", 1.0)


def test_reverse_governor_caps_speed_and_distance():
    from a6g_gz_bringup.reverse import ReverseGovernor

    gov = ReverseGovernor(max_speed_mps=0.15, max_distance_m=0.5, rearm_forward_m=0.2)
    assert gov.filter(0.4) == 0.4  # forward untouched
    assert gov.filter(-0.5) == -0.15  # backward speed capped
    for _ in range(30):  # 3 s backwards at 0.15 m/s = 0.45 m: still allowed
        gov.on_odom(-0.15, 0.1)
    assert not gov.locked and gov.filter(-0.1) == -0.1
    for _ in range(5):  # past 0.5 m: locked
        gov.on_odom(-0.15, 0.1)
    assert gov.locked and gov.filter(-0.1) == 0.0 and gov.filter(0.2) == 0.2
    gov.on_odom(0.0, 1.0)  # turning in place does not re-arm
    assert gov.locked
    for _ in range(5):  # 0.1 m forward: not enough
        gov.on_odom(0.2, 0.1)
    assert gov.locked
    for _ in range(6):  # 0.22 m forward: re-armed, a fresh 0.5 m allowed
        gov.on_odom(0.2, 0.1)
    assert not gov.locked and gov.reversed_m == 0.0


def test_reverse_governor_wiggles_do_not_add_up_to_a_long_reverse():
    from a6g_gz_bringup.reverse import ReverseGovernor

    gov = ReverseGovernor(max_speed_mps=0.15, max_distance_m=0.5, rearm_forward_m=0.2)
    for _ in range(10):  # 0.15 m back, 0.05 m forward, repeated
        for _ in range(10):
            gov.on_odom(-0.15, 0.1)
        for _ in range(5):
            gov.on_odom(0.1, 0.1)
    assert gov.locked


def test_reverse_forbidden_means_no_reverse_at_all():
    from a6g_gz_bringup.reverse import ReverseGovernor

    gov = ReverseGovernor(max_speed_mps=0.0, max_distance_m=0.0)
    assert gov.filter(-0.3) == 0.0


def test_campus_overlay_matches_the_2d_view():
    import json
    import os

    from a6g_gz_bringup.overlay import EXPOSED, POI, overlay_specs

    here = os.path.dirname(__file__)
    path = os.path.join(here, "..", "..", "a6g_gz_worlds", "worlds", "upv_vera_cpi.markers.json")
    with open(path) as f:
        campus = json.load(f)
    specs = overlay_specs(campus)
    for p in campus["pois"]:  # "Building 9B door", "Bus stop", "Bike rack", ...
        post = next(s for s in specs if s.ns == "posts" and s.pose[:2] == (p["x"], p["y"]))
        assert post.rgba == (EXPOSED if p["exposed"] else POI)
    names = [p["name"] for p in campus["pois"]] + [s["name"] for s in campus["sectors"]]
    labels = [s for s in specs if s.ns == "labels"]
    assert len(labels) == len(names) + len(campus["recovery"]) + 1  # + staging
    assert all(s.type == "TRIANGLE_LIST" and len(s.points) % 3 == 0 for s in labels)
    routes = [s for s in specs if s.ns == "routes"]
    assert len(routes) == len(campus["sectors"])
    assert sum(s.ns == "shadows" for s in specs) == len(campus["shadows"])
    assert sum(s.ns == "recovery" for s in specs) == len(campus["recovery"])
    keys = [(s.ns, s.id) for s in specs]
    assert len(keys) == len(set(keys))  # unique marker ids per namespace


def test_stroke_font_covers_the_campus_names():
    from a6g_gz_bringup.overlay import FONT, text_triangles

    names = ("Building 9B door", "Bus stop", "Bike rack", "North-west walk", "Building 8K door")
    for name in names:
        assert all(c in FONT or c == " " for c in name.upper()), name
    tris = text_triangles("BUS STOP", 10.0, 5.0, 0.1, 1.2)
    xs = [p[0] for p in tris]
    ys = [p[1] for p in tris]
    assert abs((min(xs) + max(xs)) / 2 - 10.0) < 0.3  # centred
    assert max(ys) - min(ys) <= 1.2 * 1.2 and all(p[2] == 0.1 for p in tris)


def test_trails_sample_like_the_2d_view():
    from a6g_gz_bringup.overlay import ROBOT_COLORS, Trails

    t = Trails(scale=0.25, cap=5)
    for i in range(20):
        t.add("go2_01", i * 0.01, 0.0)  # 1 cm steps: a point every 2.5 cm at scale 0.25
    assert len(t.paths["go2_01"]) == 5  # capped
    t.add("summit_01", 0.0, 0.0)
    t.add("summit_01", 1.0, 0.0)
    specs = t.specs()
    assert [s.id for s in specs] == [0, 1] and not t.changed
    assert specs[0].rgba == ROBOT_COLORS[0] and specs[1].rgba == ROBOT_COLORS[1]
    assert len(specs[1].points) == 6  # one segment = 2 triangles
