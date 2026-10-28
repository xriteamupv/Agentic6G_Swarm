"""Test-only Gazebo models (spawned into the running world by the --gazebo tests)."""

import math


def dead_end_sdf(
    x: float, y: float, yaw: float, *, half_width: float = 0.60, front: float = 0.70,
    back: float = 1.2, height: float = 1.0, thickness: float = 0.10,
) -> str:  # fmt: skip
    """A pocket around (x, y): walls ``half_width`` m to each side and ``front`` m ahead,
    open behind. Inner faces are at those distances from the given pose."""
    length = front + back
    walls = {
        "left": (front - length / 2, half_width + thickness / 2, length, thickness),
        "right": (front - length / 2, -(half_width + thickness / 2), length, thickness),
        "end": (front + thickness / 2, 0.0, thickness, 2 * (half_width + thickness)),
    }
    links = []
    for name, (lx, ly, sx, sy) in walls.items():
        geom = f"<geometry><box><size>{sx:.3f} {sy:.3f} {height:.3f}</size></box></geometry>"
        links.append(
            f'<link name="{name}"><pose>{lx:.3f} {ly:.3f} {height / 2:.3f} 0 0 0</pose>'
            f'<collision name="c">{geom}</collision><visual name="v">{geom}'
            "<material><ambient>0.6 0.2 0.2 1</ambient><diffuse>0.6 0.2 0.2 1</diffuse></material>"
            "</visual></link>"
        )
    return (
        '<?xml version="1.0"?><sdf version="1.9"><model name="dead_end"><static>true</static>'
        f"<pose>{x:.3f} {y:.3f} 0 0 0 {yaw:.6f}</pose>{''.join(links)}</model></sdf>"
    )


def wall_distance_check(half_width: float, footprint=(0.37, 0.31), padding: float = 0.10):
    """(clearance ok, nav2 can turn in place) for the pocket and the Summit footprint."""
    corner = math.hypot(footprint[0] + padding, footprint[1] + padding)
    return half_width >= 0.5, corner < half_width
