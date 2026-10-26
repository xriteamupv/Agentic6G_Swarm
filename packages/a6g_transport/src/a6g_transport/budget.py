"""Per-robot uplink bandwidth budget (SPEC Rule 8, Section 11).

A token bucket refilled at the sustained rate with a capacity of one second at the burst
rate. Traffic classes are prioritised by *reserve*: a class may only spend tokens while
the bucket stays above its reserve fraction, so lower classes back off first and
``control_msgs`` can always use the whole bucket. When a message does not fit, the class
policy decides whether it is queued (retried later) or dropped; every drop is logged.
"""

import logging
import time
from collections import Counter
from collections.abc import Callable
from enum import StrEnum

from a6g_common.schemas import TrafficClass

log = logging.getLogger(__name__)

#: Highest priority first.
CLASS_PRIORITY: tuple[TrafficClass, ...] = tuple(TrafficClass)

DEFAULT_RESERVE: dict[TrafficClass, float] = {
    TrafficClass.CONTROL_MSGS: 0.0,
    TrafficClass.VOICE: 0.1,
    TrafficClass.EVENT_IMAGE: 0.3,
    TrafficClass.MAP_DELTA: 0.5,
    TrafficClass.BULK: 0.7,
}


class Overflow(StrEnum):
    QUEUE = "queue"
    DROP = "drop"


DEFAULT_OVERFLOW: dict[TrafficClass, Overflow] = {
    TrafficClass.CONTROL_MSGS: Overflow.QUEUE,
    TrafficClass.VOICE: Overflow.DROP,  # stale audio is useless
    TrafficClass.EVENT_IMAGE: Overflow.QUEUE,
    TrafficClass.MAP_DELTA: Overflow.QUEUE,
    TrafficClass.BULK: Overflow.DROP,
}


class Admission(StrEnum):
    SEND = "send"
    QUEUE = "queue"
    DROP = "drop"


class UplinkBudget:
    def __init__(
        self,
        robot_id: str,
        sustained_mbps: float,
        burst_mbps: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        reserve: dict[TrafficClass, float] | None = None,
        overflow: dict[TrafficClass, Overflow] | None = None,
    ) -> None:
        if sustained_mbps <= 0 or burst_mbps < sustained_mbps:
            raise ValueError("need 0 < sustained_mbps <= burst_mbps")
        self.robot_id = robot_id
        self.rate_Bps = sustained_mbps * 1e6 / 8
        self.capacity_B = burst_mbps * 1e6 / 8
        self.reserve = reserve or DEFAULT_RESERVE
        self.overflow = overflow or DEFAULT_OVERFLOW
        self._monotonic = monotonic
        self._tokens = self.capacity_B
        self._last = monotonic()
        self.sent_bytes: Counter[TrafficClass] = Counter()
        self.sent_msgs: Counter[TrafficClass] = Counter()
        self.dropped: Counter[TrafficClass] = Counter()
        self.queued: Counter[TrafficClass] = Counter()

    @classmethod
    def from_config(cls, robot_id: str, budget, **kwargs) -> "UplinkBudget":
        return cls(robot_id, budget.sustained_mbps, budget.burst_mbps, **kwargs)

    @property
    def tokens(self) -> float:
        self._refill()
        return self._tokens

    def _refill(self) -> None:
        now = self._monotonic()
        self._tokens = min(self.capacity_B, self._tokens + (now - self._last) * self.rate_Bps)
        self._last = now

    def admit(self, traffic_class: TrafficClass, nbytes: int) -> Admission:
        """Spend tokens for one message, or say whether to queue or drop it."""
        self._refill()
        if nbytes > self.capacity_B:
            return self._refuse(
                traffic_class, nbytes, "larger than the burst capacity", force_drop=True
            )
        floor = self.reserve[traffic_class] * self.capacity_B
        if self._tokens - nbytes >= floor:
            self._tokens -= nbytes
            self.sent_bytes[traffic_class] += nbytes
            self.sent_msgs[traffic_class] += 1
            return Admission.SEND
        return self._refuse(traffic_class, nbytes, "budget exhausted")

    def _refuse(
        self, traffic_class: TrafficClass, nbytes: int, why: str, force_drop: bool = False
    ) -> Admission:
        if force_drop or self.overflow[traffic_class] is Overflow.DROP:
            self.dropped[traffic_class] += 1
            log.warning(
                "%s uplink DROP %s message (%d B): %s", self.robot_id, traffic_class, nbytes, why
            )
            return Admission.DROP
        self.queued[traffic_class] += 1
        return Admission.QUEUE
