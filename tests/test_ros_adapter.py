"""ros_adapter and the Go2/Summit backends, without ROS.

The adapter core runs behind its real Unix-socket server with a fake platform IO; the
skill service talks to it through ``AdapterClient`` + ``SummitBackend``/``Go2Backend``.
"""

import ast
import asyncio
import io
import math
import threading
import time
from pathlib import Path

import pytest
from a6g_common.config import PlanarTransform, adapter_config, load_config
from a6g_common.schemas import Pose2D, SkillOutcome
from a6g_ros_adapter.core import (
    AdapterCore,
    AdapterError,
    ProfileGuardError,
    check_profile,
)
from a6g_ros_adapter.image import encode_png
from a6g_ros_adapter.protocol import AdapterServer
from a6g_skill_mcp.adapter_client import AdapterClient, AdapterRequestError
from a6g_skill_mcp.frames import to_campus, to_map
from a6g_skill_mcp.ros_backend import Go2Backend, SummitBackend
from a6g_skill_mcp.service import SkillService

ADAPTER_SRC = Path(__file__).resolve().parents[1] / "packages" / "a6g_ros_adapter" / "src"


class FakeIO:
    """Platform IO stand-in: records goals; navigation ends on cancel or after ``nav_s``."""

    def __init__(self, result=None, nav_s: float = 0.05) -> None:
        self.goals: list[tuple[float, float, float, float]] = []
        self.result = result or {"outcome": "succeeded", "detail": "ok", "native_code": None}
        self.nav_s = nav_s
        self.cancelled = threading.Event()
        self.stops = 0
        self.png: str | None = None

    def pose(self):
        return {"x": 1.0, "y": 2.0, "yaw": 0.0, "cov_xy": 0.01, "frame": "robot_map"}

    def battery_pct(self):
        return 80.0

    def scan_min_range(self):
        return 3.0

    def navigate(self, x, y, yaw, timeout_s):
        self.goals.append((x, y, yaw, timeout_s))
        if self.cancelled.wait(self.nav_s):
            return {"outcome": "cancelled", "detail": "goal cancelled", "native_code": None}
        return self.result

    def cancel(self, wait: bool = True):
        self.cancelled.set()
        return True

    def stop(self):
        self.stops += 1

    def save_image(self):
        return self.png


@pytest.fixture
def summit_cfg():
    cfg = load_config("config", env={}, profile="gazebo")
    return cfg, adapter_config(cfg, "summit_01")


@pytest.fixture
def serve(tmp_path: Path):
    servers = []

    def start(config: dict, fake: FakeIO, dry_run: bool = False) -> str:
        path = str(tmp_path / f"{config['robot_id']}{len(servers)}.sock")
        server = AdapterServer(path, AdapterCore(config, fake, dry_run=dry_run))
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}).start()
        servers.append(server)
        return path

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def service(cfg, socket: str, backend=SummitBackend, robot="summit_01", t=None, tmp=None):
    b = backend(
        cfg.manifests[robot], AdapterClient(socket), map_to_campus=t, image_dir=tmp or "var/images"
    )
    svc = SkillService(b, cfg.campus)
    svc.arm("test")
    return svc


# --------------------------------------------------------------------------- core


def test_core_rejects_unknown_and_generic_ops(summit_cfg) -> None:
    core = AdapterCore(summit_cfg[1], FakeIO())
    for op in ("publish", "call_service", "set_velocity", None):
        with pytest.raises(AdapterError, match="unknown op"):
            core.handle(op, {})
    with pytest.raises(AdapterError, match="finite"):
        core.handle("navigate", {"x": math.nan, "y": 0, "timeout_s": 5})
    with pytest.raises(AdapterError, match="number"):
        core.handle("navigate", {"x": "1", "y": 0, "timeout_s": 5})


def test_dry_run_logs_goals_and_sends_nothing(summit_cfg, caplog) -> None:
    fake = FakeIO()
    core = AdapterCore(summit_cfg[1], fake, dry_run=True)
    with caplog.at_level("WARNING"):
        result = core.handle("navigate", {"x": 3.0, "y": -1.0, "yaw": 0.5, "timeout_s": 60})
        assert core.handle("stop", {}) is False
        assert core.handle("cancel", {}) is False
    assert result["outcome"] == "refused_precondition" and "dry-run" in result["detail"]
    assert fake.goals == [] and fake.stops == 0 and not fake.cancelled.is_set()
    assert "DRY-RUN summit_01: would send action goal x=3.000 y=-1.000" in caplog.text
    assert core.handle("get_state", {})["pose"]["x"] == 1.0  # reads still work


def test_profile_guard_rule_14() -> None:
    assert "OK" in check_profile("real", 0)
    assert "OK" in check_profile("gazebo", 1)
    with pytest.raises(ProfileGuardError, match="simulator is running"):
        check_profile("real", 1)
    with pytest.raises(ProfileGuardError, match="not a simulation"):
        check_profile("gazebo", 0)
    with pytest.raises(ProfileGuardError, match="unknown profile"):
        check_profile("sim", 1)


def test_adapter_is_python38_syntax() -> None:
    for path in sorted((ADAPTER_SRC / "a6g_ros_adapter").glob("*.py")):
        tree = ast.parse(path.read_text(), feature_version=(3, 8))
        for node in ast.walk(tree):
            assert not isinstance(node, ast.Match), path
            # no PEP 604 unions / builtin generics in evaluated annotations
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
                assert not any(
                    isinstance(n, ast.Name) and n.id in {"str", "int", "float", "None"}
                    for n in (node.left, node.right)
                ), f"{path}: X | Y type union"
        imports = {
            (n.module or "").split(".")[0]
            if isinstance(n, ast.ImportFrom)
            else a.name.split(".")[0]
            for n in ast.walk(tree)
            if isinstance(n, ast.Import | ast.ImportFrom)
            for a in (n.names if isinstance(n, ast.Import) else [n])
        }
        third_party = imports - {
            "argparse", "json", "logging", "math", "os", "signal", "socketserver", "stat",
            "struct", "sys", "threading", "time", "typing", "zlib", "a6g_ros_adapter",
            # ROS client library and message / tf packages (the only non-stdlib imports)
            "rclpy", "action_msgs", "geometry_msgs", "nav_msgs", "sensor_msgs", "tf2_ros",
            "nav2_msgs", "unitree_api",
        }  # fmt: skip
        assert not third_party, f"{path}: {third_party}"


def test_png_encoder_round_trips() -> None:
    from PIL import Image

    w, h = 4, 3
    rgb = bytes(range(w * h * 3))
    bgr = b"".join(rgb[i : i + 3][::-1] for i in range(0, len(rgb), 3))
    for enc, data in (("rgb8", rgb), ("bgr8", bgr)):
        img = Image.open(io.BytesIO(encode_png(w, h, enc, w * 3, data)))
        assert img.size == (w, h) and img.tobytes() == rgb


# --------------------------------------------------------------------------- frames


def test_map_to_campus_round_trip_with_non_trivial_transforms() -> None:
    for t in (
        PlanarTransform(x=12.5, y=-7.0, yaw=0.7),
        PlanarTransform(x=-150.0, y=40.0, yaw=-2.9),
        PlanarTransform(),
    ):
        for pose in (Pose2D(x=3.0, y=4.0, yaw=0.2), Pose2D(x=-20.0, y=0.5, yaw=-3.0)):
            mx, my, myaw = to_map(t, pose)
            cx, cy, cyaw = to_campus(t, mx, my, myaw)
            assert (cx, cy) == pytest.approx((pose.x, pose.y), abs=1e-9)
            assert math.cos(cyaw - pose.yaw) == pytest.approx(1.0)
    # The map origin sits at (t.x, t.y) in campus_map, rotated by t.yaw.
    t = PlanarTransform(x=10.0, y=5.0, yaw=math.pi / 2)
    assert to_campus(t, 1.0, 0.0, 0.0) == pytest.approx((10.0, 6.0, math.pi / 2))


# --------------------------------------------------------------------------- end to end


async def test_navigate_through_socket_converts_frames_and_maps_codes(
    summit_cfg, serve, tmp_path
) -> None:
    cfg, acfg = summit_cfg
    fake = FakeIO(result={"outcome": "unknown", "detail": "collision ahead", "native_code": 106})
    t = PlanarTransform(x=100.0, y=-50.0, yaw=math.pi / 2)
    svc = service(cfg, serve(acfg, fake), t=t, tmp=tmp_path)
    result = await svc.navigate_to(poi_id="poi_kiosk")
    assert result.outcome is SkillOutcome.BLOCKED and result.native_code == 106  # manifest table
    kiosk = cfg.campus.poi("poi_kiosk").pose
    x, y, _, timeout = fake.goals[0]
    assert (x, y) == pytest.approx(to_map(t, kiosk)[:2])  # sent in the robot's map frame
    assert timeout <= cfg.manifests["summit_01"].skill_timeouts["navigate_to"]
    state = await svc.get_state()  # adapter pose (1, 2) in robot_map -> campus_map
    assert (state.pose.x, state.pose.y) == pytest.approx(to_campus(t, 1.0, 2.0, 0.0)[:2])
    assert state.battery_pct is None  # Summit battery is unreliable
    await svc.backend.client.close()


async def test_cancel_overtakes_a_running_navigate(summit_cfg, serve) -> None:
    cfg, acfg = summit_cfg
    fake = FakeIO(nav_s=30.0)
    svc = service(cfg, serve(acfg, fake))
    nav = asyncio.create_task(svc.navigate_to(pose=Pose2D(x=5.0, y=0.0)))
    while not fake.goals:
        await asyncio.sleep(0.01)
    started = time.monotonic()
    await svc.cancel()
    result = await nav
    assert result.outcome is SkillOutcome.CANCELLED and time.monotonic() - started < 5
    stop = await svc.stop()
    assert stop.outcome is SkillOutcome.SUCCEEDED and fake.stops == 1
    await svc.backend.client.close()


async def test_dry_run_backend_never_moves(summit_cfg, serve) -> None:
    cfg, acfg = summit_cfg
    fake = FakeIO()
    svc = service(cfg, serve(acfg, fake, dry_run=True))
    result = await svc.navigate_to(poi_id="poi_kiosk")
    assert result.outcome is SkillOutcome.REFUSED_PRECONDITION and "dry-run" in result.detail
    assert fake.goals == []
    await svc.backend.client.close()


async def test_capture_image_becomes_a_bounded_jpeg(summit_cfg, serve, tmp_path) -> None:
    cfg, acfg = summit_cfg
    fake = FakeIO()
    png = tmp_path / "frame.png"
    w, h = 64, 48
    png.write_bytes(encode_png(w, h, "rgb8", w * 3, bytes((i * 7) % 256 for i in range(w * h * 3))))
    fake.png = str(png)
    svc = service(cfg, serve(acfg, fake), tmp=tmp_path / "out")
    result = await svc.capture_event_image(max_kb=50)
    assert result.outcome is SkillOutcome.SUCCEEDED
    jpg = Path(result.artifact_uri.removeprefix("file://"))
    assert jpg.read_bytes()[:2] == b"\xff\xd8" and jpg.stat().st_size <= 50 * 1024
    await svc.backend.client.close()


async def test_adapter_errors_surface_and_platform_must_match(summit_cfg, serve) -> None:
    cfg, acfg = summit_cfg
    socket = serve(acfg, FakeIO())
    client = AdapterClient(socket)
    with pytest.raises(AdapterRequestError, match="unknown op"):
        await client.request("publish", {"topic": "/cmd_vel"})
    assert (await client.request("info"))["platform"] == "summit"
    await client.close()
    with pytest.raises(ValueError, match="go2 manifest"):
        Go2Backend(cfg.manifests["summit_01"], AdapterClient(socket))


def test_adapter_config_from_manifest_and_profile(summit_cfg) -> None:
    cfg, acfg = summit_cfg
    assert acfg["profile"] == "gazebo" and acfg["ros_domain_id"] == 11
    assert acfg["socket"].endswith("/a6g/summit_01.sock")
    assert acfg["ros"]["stop"] == {
        "method": "zero_twist",
        "topic": "/robot/web_teleop/cmd_vel",
        "api_id": None,
        "duration_s": 1.0,
    }
    go2 = adapter_config(cfg, "go2_01")
    assert (
        go2["ros"]["stop"]["method"] == "sport_stop_move" and go2["ros"]["stop"]["api_id"] == 1003
    )


def test_clearance_ignores_single_noisy_returns() -> None:
    from a6g_ros_adapter.core import robust_min_range

    wall = [0.60, 0.61, 0.43, 0.59, 0.60, 0.62, 0.60]  # one noisy short beam
    assert robust_min_range(wall, 0.45, 60.0) == pytest.approx(0.60)
    post = [5.0, 5.0, 0.48, 0.47, 0.49, 5.0, 5.0]  # a real obstacle spans several beams
    assert robust_min_range(post, 0.45, 60.0) == pytest.approx(0.49)  # within the obstacle
    assert robust_min_range([math.inf, math.nan, 0.1], 0.45, 60.0) == 60.0
