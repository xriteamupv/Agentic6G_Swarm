"""gz_control: the simulator's side door for ``a6g-gz-stack`` (system Python, gz-transport).

Robots live in separate ROS domains (Rule 17), so the stack reads ground truth straight
from Gazebo instead of from ROS. A small JSON HTTP API (stdlib) on ``--port``:

- ``GET  /poses``   ground-truth pose and body twist of every tracked model + sim time;
- ``POST /track``   ``{"robot_id"}``: start following ``/model/<id>/odometry``;
- ``POST /freeze``  ``{"robot_id"}``: hold the model still: zero ``cmd_vel`` at 20 Hz
  (``robot_fail``: Gazebo's velocity systems otherwise keep the last command);
- ``POST /spawn``   ``{"name", "sdf", "x", "y", "z", "yaw"}`` (anomaly markers);
- ``POST /remove``  ``{"name"}``.

Simulation-only infrastructure: never part of a robot deployment.
"""

import argparse
import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from gz.msgs10.boolean_pb2 import Boolean
from gz.msgs10.clock_pb2 import Clock
from gz.msgs10.entity_factory_pb2 import EntityFactory
from gz.msgs10.entity_pb2 import Entity
from gz.msgs10.odometry_pb2 import Odometry
from gz.msgs10.twist_pb2 import Twist
from gz.transport13 import Node


def _yaw(q) -> float:
    return math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))


class GzControl:
    def __init__(self, world: str) -> None:
        self.world = world
        self.node = Node()
        self.requests = Node()  # service requests (see _request)
        self.lock = threading.Lock()
        self.poses: dict[str, dict] = {}
        self.sim_time = 0.0
        self.frozen: dict[str, object] = {}
        self.node.subscribe(Clock, "/clock", self._on_clock)
        threading.Thread(target=self._freeze_loop, daemon=True).start()

    def _on_clock(self, msg: Clock) -> None:
        self.sim_time = msg.sim.sec + msg.sim.nsec * 1e-9

    def track(self, robot_id: str) -> None:
        if robot_id in self.poses:
            return

        def on_odom(msg: Odometry, rid: str = robot_id) -> None:
            p, q = msg.pose.position, msg.pose.orientation
            t = msg.header.stamp.sec + msg.header.stamp.nsec * 1e-9
            with self.lock:
                self.poses[rid] = {
                    "x": p.x,
                    "y": p.y,
                    "yaw": _yaw(q),
                    "vx": msg.twist.linear.x,
                    "vy": msg.twist.linear.y,
                    "wz": msg.twist.angular.z,
                    "t": t,
                }

        with self.lock:
            self.poses.setdefault(robot_id, {})
        self.node.subscribe(Odometry, f"/model/{robot_id}/odometry", on_odom)

    def snapshot(self) -> dict:
        with self.lock:
            robots = {k: dict(v) for k, v in self.poses.items()}
        return {"sim_time": self.sim_time, "robots": robots}

    def freeze(self, robot_id: str) -> None:
        with self.lock:
            if robot_id not in self.frozen:
                self.frozen[robot_id] = self.node.advertise(f"/model/{robot_id}/cmd_vel", Twist)

    def _freeze_loop(self) -> None:
        zero = Twist()
        while True:
            with self.lock:
                pubs = list(self.frozen.values())
            for pub in pubs:
                pub.publish(zero)
            time.sleep(0.05)

    def spawn(self, name: str, sdf: str, x: float, y: float, z: float, yaw: float) -> bool:
        req = EntityFactory()
        req.sdf = sdf
        req.name = name
        req.allow_renaming = False
        req.pose.position.x, req.pose.position.y, req.pose.position.z = x, y, z
        req.pose.orientation.z, req.pose.orientation.w = math.sin(yaw / 2), math.cos(yaw / 2)
        return self._request(f"/world/{self.world}/create", req, EntityFactory)

    def remove(self, name: str) -> bool:
        req = Entity()
        req.name = name
        req.type = Entity.MODEL
        return self._request(f"/world/{self.world}/remove", req, Entity)

    def _request(self, service: str, req, req_type, timeout_s: float = 5.0) -> bool:
        """Requests fail until service discovery has completed: retry for ``timeout_s``."""
        end = time.monotonic() + timeout_s
        while True:
            ok, rep = self.requests.request(service, req, req_type, Boolean, 2000)
            if ok:
                return bool(rep.data)
            if time.monotonic() > end:
                return False
            time.sleep(0.3)


def make_handler(ctl: GzControl):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # quiet
            pass

        def _send(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path == "/poses":
                self._send(200, ctl.snapshot())
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/track":
                    ctl.track(body["robot_id"])
                    self._send(200, {"ok": True})
                elif self.path == "/freeze":
                    ctl.freeze(body["robot_id"])
                    self._send(200, {"ok": True})
                elif self.path == "/spawn":
                    ok = ctl.spawn(
                        body["name"], body["sdf"], float(body.get("x", 0)), float(body.get("y", 0)),
                        float(body.get("z", 0)), float(body.get("yaw", 0)),
                    )  # fmt: skip
                    self._send(200 if ok else 500, {"ok": ok})
                elif self.path == "/remove":
                    ok = ctl.remove(body["name"])
                    self._send(200 if ok else 500, {"ok": ok})
                else:
                    self._send(404, {"error": "not found"})
            except (KeyError, ValueError) as exc:
                self._send(400, {"error": str(exc)})

    return Handler


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--world", required=True)
    p.add_argument("--port", type=int, default=8310)
    p.add_argument("--track", default="", help="comma-separated model names")
    a = p.parse_args()
    ctl = GzControl(a.world)
    for rid in filter(None, a.track.split(",")):
        ctl.track(rid)
    server = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(ctl))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
