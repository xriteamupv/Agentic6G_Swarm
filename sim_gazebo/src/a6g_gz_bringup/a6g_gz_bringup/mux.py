"""Priority selection of twist_mux (ROS-free)."""

from dataclasses import dataclass, field


@dataclass
class PriorityMux:
    priorities: dict[str, int]
    timeouts: dict[str, float]
    _last: dict[str, float] = field(default_factory=dict)

    def accept(self, name: str, now: float) -> bool:
        """Record a message on ``name``; True if it should be forwarded."""
        self._last[name] = now
        mine = self.priorities[name]
        for other, t in self._last.items():
            if other == name or self.priorities[other] <= mine:
                continue
            if now - t <= self.timeouts[other]:
                return False  # a higher-priority input is active
        return True
