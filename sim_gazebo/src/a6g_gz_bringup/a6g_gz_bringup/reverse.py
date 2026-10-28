"""Hard reverse limits for the Summit (ROS-free).

MPPI only *prefers* driving forward; when every forward trajectory is blocked (a dead
end) reversing becomes its only option and it would back out blind. The governor enforces
the manifest's ``reverse_limits`` on every command that reaches the base, whatever its
source: backward speed is capped, and once one continuous reverse move has covered
``max_distance_m`` (measured on odometry) further backward commands are zeroed until the
robot has driven ``rearm_forward_m`` forwards (so short wiggles cannot add up to a long
blind reverse).
"""

from dataclasses import dataclass

#: Below this measured vx (m/s) the base is moving backwards; above -this, forwards.
MOVING_VX = 0.02


@dataclass
class ReverseGovernor:
    max_speed_mps: float
    max_distance_m: float
    rearm_forward_m: float = 0.2
    reversed_m: float = 0.0  # current reverse move
    forward_m: float = 0.0  # forward travel since the last reverse move
    locked: bool = False

    def on_odom(self, vx: float, dt: float) -> None:
        """Measured body-frame velocity over ``dt`` seconds."""
        if vx < -MOVING_VX:
            self.reversed_m += -vx * dt
            self.forward_m = 0.0
            if self.reversed_m >= self.max_distance_m:
                self.locked = True
        elif vx > MOVING_VX:
            self.forward_m += vx * dt
            if self.forward_m >= self.rearm_forward_m:
                self.reversed_m, self.locked = 0.0, False

    def filter(self, vx: float) -> float:
        """The forward command allowed through."""
        if vx >= 0.0:
            return vx
        if self.locked:
            return 0.0
        return max(vx, -self.max_speed_mps)
