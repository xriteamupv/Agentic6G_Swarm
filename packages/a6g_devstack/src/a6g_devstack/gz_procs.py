"""ROS / Gazebo child processes for ``a6g-gz-stack`` (and the ``--gazebo`` tests).

Every process runs in the ROS Jazzy environment (system Python) in its own process group,
so it can be stopped as a unit: SIGINT first (Nav2 and the bridges shut down cleanly),
then SIGTERM, then SIGKILL.
"""

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
WORKSPACE = REPO / "sim_gazebo"
ADAPTER_SRC = REPO / "packages" / "a6g_ros_adapter" / "src"
ROS_SETUP = (
    "source /opt/ros/jazzy/setup.bash && "
    "{ [ -f ~/robotnik_ws/install/setup.bash ] && "
    "source ~/robotnik_ws/install/setup.bash || true; } && "
    f"source {WORKSPACE}/install/setup.bash"
)


def ros_env(**extra: str) -> dict[str, str]:
    """This environment minus the uv venv, so ROS tools run under the system Python."""
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV",)}
    env["PATH"] = "/usr/bin:" + env.get("PATH", "")
    env.pop("PYTHONPATH", None)
    env.update(extra)
    return env


def start(cmd: str, log: Path, env: dict[str, str]) -> subprocess.Popen:
    log.parent.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        ["bash", "-c", f"{ROS_SETUP} && exec {cmd}"],
        stdout=log.open("w"),
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )


def stop(proc: subprocess.Popen | None, grace_s: float = 15.0) -> None:
    if proc is None:
        return
    for sig, wait in ((signal.SIGINT, grace_s), (signal.SIGTERM, 5.0), (signal.SIGKILL, 5.0)):
        if proc.poll() is not None:
            break
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, sig)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(wait)
    with contextlib.suppress(ProcessLookupError):  # stragglers in the group
        os.killpg(proc.pid, signal.SIGKILL)


def run(cmd: str, env: dict[str, str], timeout_s: float = 30.0) -> str:
    """stdout of a ROS command line; "" if it hangs past ``timeout_s`` (callers poll)."""
    try:
        out = subprocess.run(
            ["bash", "-c", f"{ROS_SETUP} && {cmd}"],
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return ""
    return out.stdout


def adapter_cmd(config: Path, *args: str) -> str:
    return (
        f"env PYTHONPATH={ADAPTER_SRC}:$PYTHONPATH /usr/bin/python3 -m a6g_ros_adapter "
        f"--config {config} " + " ".join(args)
    )


#: The lifecycle manager's order (a6g_gz_bringup robot_actions.NAV2_NODES).
NAV2_NODES = (
    "controller_server",
    "smoother_server",
    "planner_server",
    "behavior_server",
    "velocity_smoother",
    "bt_navigator",
)
#: Seconds before a Nav2 bringup counts as stalled (normal: 10-60 s on a loaded host).
NAV2_STALL_S = 90.0

log = logging.getLogger(__name__)


def finish_bringup_plan(states: dict[str, str]) -> list[tuple[str, str]]:
    """Transitions that complete a Nav2 bringup the lifecycle manager stalled on.

    Jazzy's lifecycle manager waits for each ``change_state`` reply with no timeout; under
    load Fast DDS can lose one, and the manager then hangs silently with the node already
    transitioned. Configure every unconfigured node, then activate every inactive one, in
    the manager's order. Any other state (unknown, finalized, error) -> no plan.
    """
    plan: list[tuple[str, str]] = []
    for node in NAV2_NODES:
        state = states.get(node)
        if state not in ("unconfigured", "inactive", "active"):
            return []
        if state == "unconfigured":
            plan.append((node, "configure"))
    plan += [(n, "activate") for n in NAV2_NODES if states[n] != "active"]
    return plan


def ensure_nav2_active(
    ros, ns: str = "", timeout_s: float = 300.0, stall_s: float = NAV2_STALL_S
) -> None:
    """Wait for bt_navigator; finish a stalled bringup once. ``ros(cmd, timeout) -> stdout``."""
    prefix = ns.rstrip("/") if ns else ""
    target = f"{prefix}/bt_navigator"
    start = time.monotonic()
    rescued = False
    while time.monotonic() - start < timeout_s:
        if "active [3]" in ros(f"ros2 lifecycle get {target}", 20):
            return
        if not rescued and time.monotonic() - start > stall_s:
            rescued = True
            states = {}
            for node in NAV2_NODES:
                out = ros(f"ros2 lifecycle get {prefix}/{node}", 20)
                states[node] = out.split(" [")[0].strip() if out.strip() else "unknown"
            plan = finish_bringup_plan(states)
            log.warning(
                "Nav2 bringup under %s stalled %s; finishing: %s", prefix or "/", states, plan
            )
            for node, transition in plan:
                ros(f"ros2 lifecycle set {prefix}/{node} {transition}", 60)
        time.sleep(2)
    raise TimeoutError(f"{target} not active after {timeout_s} s")


async def wait_nav2_active(env: dict[str, str], ns: str = "", timeout_s: float = 300.0) -> None:
    """Goals sent before bt_navigator is active are rejected."""
    await asyncio.to_thread(ensure_nav2_active, lambda cmd, t: run(cmd, env, t), ns, timeout_s)
