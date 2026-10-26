"""Robot liveness from the telemetry plane (SPEC 10.2 handle_failure, 9.4).

Turns ``a6g/fleet/<id>/state`` (1 Hz) and ``a6g/fleet/<id>/status`` (retained, ``offline``
is the robot's MQTT last-will) into coordinator events:

* ``robot_failed``: an ``offline`` last-will (the robot/agent died) -> reallocate all its work;
* ``robot_lost``: ``robot_timeout_s`` of silence, e.g. a 5G coverage shadow. The robot may
  well be fine (it finishes its current task and holds), so only work it has not started
  is reallocated;
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
    def __init__(self, known: set[str], timeout_s: float) -> None:
        self.known = set(known)
        self.timeout = timedelta(seconds=timeout_s)
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
                # work stayed with it (baseline bench, 2026-10-21). Missions whose twin
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
        if rid not in self.known:
            self.known.add(rid)
            events.append({"type": "robot_added", "robot_id": rid, "a2a_url": self.urls.get(rid)})
        elif rid in self.failed and kind == "state":
            self.failed.discard(rid)
            events.append({"type": "robot_recovered", "robot_id": rid})
        return events

    def check(self, now: datetime) -> list[dict[str, Any]]:
        events = []
        for rid, seen in sorted(self.last_seen.items()):
            if rid not in self.failed and now - seen > self.timeout:
                self.failed.add(rid)
                events.append(
                    {
                        "type": "robot_lost",
                        "robot_id": rid,
                        "reason": f"no state for {self.timeout.total_seconds():.0f} s",
                    }
                )
        return events
