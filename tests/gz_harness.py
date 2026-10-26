"""Run the Gazebo world and one robot stack from pytest (``--gazebo`` tests only).

Processes run in the ROS Jazzy environment (system Python), each in its own process group,
and are stopped in order (robot stack, then Gazebo) with SIGINT before anything is killed,
so Nav2 and the bridges shut down cleanly.
"""

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WORKSPACE = REPO / "sim_gazebo"
ROS_SETUP = (
    "source /opt/ros/jazzy/setup.bash && "
    "{ [ -f ~/robotnik_ws/install/setup.bash ] && "
    "source ~/robotnik_ws/install/setup.bash || true; } && "
    f"source {WORKSPACE}/install/setup.bash"
)


def available() -> str | None:
    """None if Gazebo tests can run here, else the reason."""
    if not Path("/opt/ros/jazzy/setup.bash").exists():
        return "ROS 2 Jazzy not installed"
    if not (WORKSPACE / "install" / "setup.bash").exists():
        return "sim_gazebo not built (colcon build --symlink-install in sim_gazebo/)"
    if not (WORKSPACE / "src" / "a6g_gz_worlds" / "maps").exists():
        return "campus not generated (uv run a6g campus build)"
    return None


def _env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV")}
    env.update(extra)
    return env


def _start(cmd: str, log: Path, env: dict[str, str]) -> subprocess.Popen:
    return subprocess.Popen(
        ["bash", "-c", f"{ROS_SETUP} && exec {cmd}"],
        stdout=log.open("w"),
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )


def _stop(proc: subprocess.Popen, grace_s: float) -> None:
    for sig, wait in ((signal.SIGINT, grace_s), (signal.SIGTERM, 5.0), (signal.SIGKILL, 5.0)):
        if proc.poll() is not None:
            break
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, sig)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(wait)
    with contextlib.suppress(ProcessLookupError):  # stragglers in the group
        os.killpg(proc.pid, signal.SIGKILL)


@dataclass
class GzRun:
    out_dir: Path
    env: dict[str, str]
    _procs: list = field(default_factory=list)

    def nav_check(self, *args: str, timeout_s: float = 900.0) -> dict:
        summary = self.out_dir / "nav_check.json"
        cmd = f"ros2 run a6g_gz_bringup nav_check {' '.join(args)} --json {summary}"
        subprocess.run(
            ["bash", "-c", f"{ROS_SETUP} && {cmd}"],
            env=self.env,
            stdout=(self.out_dir / "nav_check.log").open("w"),
            stderr=subprocess.STDOUT,
            timeout=timeout_s,
        )
        return json.loads(summary.read_text()) if summary.exists() else {"error": "no summary"}

    def ros(self, cmd: str, timeout_s: float = 30.0) -> str:
        """Run a ROS 2 command line in the robot's domain; stdout."""
        out = subprocess.run(
            ["bash", "-c", f"{ROS_SETUP} && {cmd}"],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        return out.stdout

    def echo(self, topic: str, timeout_s: float = 20.0) -> dict | None:
        """One message of ``topic`` as a dict (``ros2 topic echo --once``)."""
        import yaml

        text = self.ros(
            f"timeout {timeout_s} ros2 topic echo --once --full-length {topic}", timeout_s + 10
        )
        text = text.split("---")[0]
        # ros2 topic echo interleaves Fast DDS loss warnings ("A message was lost!!!",
        # "\ttotal count change:4"): not YAML.
        text = "\n".join(
            line for line in text.splitlines() if not line.startswith(("A message was lost", "\t"))
        )
        return yaml.safe_load(text) if text.strip() else None

    def wait_nav2_active(self, ns: str = "", timeout_s: float = 240.0) -> None:
        """Goals sent before bt_navigator is active are rejected (stalled bringups are
        finished once, see gz_procs.ensure_nav2_active)."""
        from a6g_devstack.gz_procs import ensure_nav2_active

        ensure_nav2_active(self.ros, ns, timeout_s)

    def ground_truth(self) -> tuple[float, float, float]:
        import math

        msg = self.echo("/a6g_sim/ground_truth")
        assert msg is not None, "no ground truth"
        p, q = msg["pose"]["pose"]["position"], msg["pose"]["pose"]["orientation"]
        yaw = math.atan2(
            2 * (q["w"] * q["z"] + q["x"] * q["y"]), 1 - 2 * (q["y"] ** 2 + q["z"] ** 2)
        )
        return p["x"], p["y"], yaw

    def velocity_stats(self) -> dict:
        msg = self.echo("/a6g_sim/velocity_monitor/stats")
        return json.loads(msg["data"]) if msg else {}

    def start_adapter(self, config: Path, *args: str, name: str = "adapter") -> subprocess.Popen:
        """``python3 -m a6g_ros_adapter`` under the ROS Python, in this robot's domain."""
        src = REPO / "packages" / "a6g_ros_adapter" / "src"
        proc = _start(
            f"env PYTHONPATH={src}:$PYTHONPATH /usr/bin/python3 -m a6g_ros_adapter "
            f"--config {config} " + " ".join(args),
            self.out_dir / f"{name}.log",
            self.env,
        )
        self._procs.append(proc)
        return proc

    def wait_socket(self, path: str, proc: subprocess.Popen, timeout_s: float = 60.0) -> None:
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            if Path(path).exists():
                return
            if proc.poll() is not None:
                raise RuntimeError(f"adapter exited with code {proc.returncode}")
            time.sleep(0.5)
        raise TimeoutError(f"adapter socket {path} did not appear")

    def stop_all(self) -> None:
        for proc in reversed(self._procs):
            _stop(proc, grace_s=10.0)
        self._procs.clear()


@contextlib.contextmanager
def gazebo_robot(robot: str, domain: int, out_dir: Path, *launch_args: str) -> Iterator[GzRun]:
    """World + one robot (``robot.launch.py``) in its ROS domain, on a private gz partition."""
    out_dir.mkdir(parents=True, exist_ok=True)
    env = _env(GZ_PARTITION=f"a6g_test_{uuid.uuid4().hex[:8]}")
    world = _start("ros2 launch a6g_gz_bringup world.launch.py", out_dir / "world.log", env)
    robot_proc = None
    try:
        time.sleep(8)
        robot_proc = _start(
            f"ros2 launch a6g_gz_bringup robot.launch.py robot:={robot} out_dir:={out_dir} "
            + " ".join(launch_args),
            out_dir / "robot.log",
            env,
        )
        run = GzRun(out_dir, {**env, "ROS_DOMAIN_ID": str(domain)})
        try:
            yield run
        finally:
            run.stop_all()
    finally:
        if robot_proc is not None:
            _stop(robot_proc, grace_s=30.0)
        _stop(world, grace_s=15.0)


# --------------------------------------------------------------------------- skills


def adapter_socket(robot_id: str) -> str:
    """Short path: AF_UNIX paths are limited to 108 bytes."""
    base = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "a6g"
    base.mkdir(parents=True, exist_ok=True)
    return str(base / f"{robot_id}_{uuid.uuid4().hex[:8]}.sock")


def adapter_json(
    cfg, robot_id: str, out: Path, localization: str | None = None, **override
) -> tuple[Path, dict]:
    """Write a ros_adapter config for this test run (unique short socket path)."""
    from a6g_common.config import adapter_config

    acfg = adapter_config(cfg, robot_id, str(out / "images"), localization=localization)
    acfg["socket"] = adapter_socket(robot_id)
    acfg.update(override)
    path = out / f"adapter_{uuid.uuid4().hex[:6]}.json"
    path.write_text(json.dumps(acfg))
    return path, acfg


def skill_service(
    run: "GzRun", cfg, robot_id: str, out: Path, *args: str, localization: str | None = None
):
    """Adapter (ROS Python) + armed SkillService (this Python) for one robot."""
    from a6g_skill_mcp.serve import ros_service

    path, acfg = adapter_json(cfg, robot_id, out, localization)
    proc = run.start_adapter(path, *args, name=f"adapter_{robot_id}")
    run.wait_socket(acfg["socket"], proc, 120)
    run.wait_nav2_active("/robot" if robot_id.startswith("summit") else "")
    svc = ros_service(cfg, robot_id, acfg["socket"], out / "images")
    svc.arm("pytest")
    return svc


async def localized(svc, timeout_s: float = 120.0) -> None:
    for _ in range(int(timeout_s / 2)):
        if (await svc.get_state()).pose.cov_xy is not None:
            return
        await asyncio.sleep(2)
    raise TimeoutError("no localization")
