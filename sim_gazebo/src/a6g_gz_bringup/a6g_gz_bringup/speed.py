"""One driving speed limit per robot (ROS-free, unit-tested).

``max_speed`` (m/s) sets Nav2's cruise speed (Go2 RPP ``desired_linear_vel``, Summit MPPI
``vx_max``) and the velocity smoother's limit, and, in simulation, the platform cap:
``cmd_vel_to_sport_sim``'s ``vx_max`` on the Go2 (the real dock clamps at 0.30) and, on the
Summit, the DiffDrive limit (1.0 m/s). The Summit's reverse limit stays at its manifest
``reverse_limits.max_speed_mps`` (or ``max_speed`` if lower).
"""

SUMMIT_BASE_MAX = 1.0
SUMMIT_REVERSE_MAX = 0.15  # config/robots/summit_01.yaml reverse_limits.max_speed_mps
SIM_MAX = 1.0


def check_speed(platform: str, v: float) -> None:
    limit = SUMMIT_BASE_MAX if platform == "summit" else SIM_MAX
    if not 0 < v <= limit:
        raise ValueError(f"max_speed {v} m/s out of range for {platform} (0, {limit}]")


def apply_max_speed(params: dict, platform: str, v: float) -> dict:
    """Set the speed in a platform Nav2 parameter dict (node keys at the top level)."""
    check_speed(platform, v)
    ctrl = params["controller_server"]["ros__parameters"]["FollowPath"]
    reverse = min(SUMMIT_REVERSE_MAX, v) if platform == "summit" else v
    if "MPPI" in ctrl.get("plugin", ""):
        ctrl["vx_max"] = v
        ctrl["vx_min"] = -reverse
    else:  # RPP
        ctrl["desired_linear_vel"] = v
        ctrl["regulated_linear_scaling_min_speed"] = min(
            ctrl["regulated_linear_scaling_min_speed"], v
        )
        ctrl["min_approach_linear_velocity"] = min(ctrl["min_approach_linear_velocity"], v)
    vs = params["velocity_smoother"]["ros__parameters"]
    vs["max_velocity"] = [v, *vs["max_velocity"][1:]]
    vs["min_velocity"] = [-reverse, *vs["min_velocity"][1:]]
    return params
