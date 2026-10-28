"""Robot liveness from the telemetry plane (``handle_failure``).

Turns ``a6g/fleet/<id>/state`` (1 Hz) and ``a6g/fleet/<id>/status`` (retained, ``offline``
is the robot's MQTT last-will) into coordinator events:

* ``robot_failed``: an ``offline`` last-will (the robot/agent died) -> reallocate all its work;
* ``robot_lost``: ``robot_timeout_s`` of silence, e.g. a 5G coverage shadow. The robot may
  well be fine (it finishes its current task and holds), so only work it has not started
  is reallocated;
* ``robot_lost_long``: still silent ``lost_release_s`` after ``robot_lost``, and again every
  ``lost_release_s`` while it stays silent (``silent_s``); the coordinator decides when the
  robot is presumed stuck;
* ``robot_charged``: a robot's battery (from its state messages) crosses up to
  ``charged_pct``: it can take work again, whatever reports it sent or lost;
* ``robot_recovered``: a failed/lost robot is heard again; ``robot_added``: a new robot
  announces itself (fleet change).
Soft state only: it is rebuilt from the bus after a restart.
"""

import json
from datetime import datetime, timedelta
from typing import Any

from a6g_transport.messaging import BusMessage


def _status(payload: bytes | None) -> dict[str, Any]:
    text = (payload or b"").decode(errors="replace").strip()
    try:
        data = json.loads(text)
    except ValueError:
        data = {"state": text}
    return data if isinstance(data, dict) else {"state": str(data)}


class FleetMonitor:
    def __init__(
        self,
        known: set[str],
        timeout_s: float,
        release_s: float | None = None,
        charged_pct: float | None = None,
    ) -> None:
        self.charged_pct = charged_pct
        self.battery: dict[str, float] = {}  # last battery seen in a state message
        self.known = set(known)
        self.timeout = timedelta(seconds=timeout_s)
        self.release = timedelta(seconds=release_s) if release_s else None
        self.lost_due: dict[str, datetime] = {}  # lost robots: next robot_lost_long
        self.last_seen: dict[str, datetime] = {}
        self.failed: set[str] = set()  # failed or lost
        self.urls: dict[str, str] = {}

    def observe(self, msg: BusMessage, now: datetime) -> list[dict[str, Any]]:
        parts = msg.topic.split("/")
        if len(parts) != 4 or parts[0] != "a6g" or parts[1] != "fleet":
            return []
        rid, kind = parts[2], parts[3]
        events: list[dict[str, Any]] = []
        if kind == "status":
            status = _status(msg.payload)
            if status.get("a2a_url"):
                self.urls[rid] = status["a2a_url"]
            if status.get("state") == "offline":
                # Any robot's last-will, not only the configured ones: after a coordinator
                # restart a hot-added robot that died meanwhile is unknown here, and its
                # work would stay with it. Missions whose twin
                # does not have it ignore the event.
                if rid not in self.failed:
                    self.known.add(rid)
                    self.failed.add(rid)
                    events.append(
                        {"type": "robot_failed", "robot_id": rid, "reason": "offline (last-will)"}
                    )
                return events
            if status.get("state") != "online":
                return events
        elif kind != "state":
            return events
        if msg.retain and kind == "state":
            return events  # stale retained state is not a heartbeat
        self.last_seen[rid] = now
        if kind == "state" and self.charged_pct is not None:
            try:
                pct = float(json.loads(msg.payload).get("battery_pct"))
            except (ValueError, TypeError, AttributeError):
                pct = None
            if pct is not None:
                before = self.battery.get(rid)
                self.battery[rid] = pct
                if before is not None and before < self.charged_pct <= pct:
                    events.append({"type": "robot_charged", "robot_id": rid, "battery_pct": pct})
        if rid not in self.known:
            self.known.add(rid)
            events.append({"type": "robot_added", "robot_id": rid, "a2a_url": self.urls.get(rid)})
        elif rid in self.failed and kind == "state":
            self.failed.discard(rid)
            self.lost_due.pop(rid, None)
            events.append({"type": "robot_recovered", "robot_id": rid})
        return events

    def check(self, now: datetime) -> list[dict[str, Any]]:
        events = []
        for rid, seen in sorted(self.last_seen.items()):
            if rid not in self.failed and now - seen > self.timeout:
                self.failed.add(rid)
                if self.release is not None:
                    self.lost_due[rid] = now + self.release
                events.append(
                    {
                        "type": "robot_lost",
                        "robot_id": rid,
                        "reason": f"no state for {self.timeout.total_seconds():.0f} s",
                    }
                )
        for rid, due in sorted(self.lost_due.items()):
            if now > due and self.release is not None:
                self.lost_due[rid] = now + self.release
                silent = round((now - self.last_seen.get(rid, due)).total_seconds())
                events.append(
                    {
                        "type": "robot_lost_long",
                        "robot_id": rid,
                        "silent_s": silent,
                        "reason": f"no state for {silent} s",
                    }
                )
        return events
