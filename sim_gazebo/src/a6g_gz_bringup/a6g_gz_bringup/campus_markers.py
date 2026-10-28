"""campus_markers: draw the campus overlay (POIs, sectors, routes, shadows, recovery points,
staging) and the robots' trails in the Gazebo GUI, like the 2D view.

Trails come from each model's ground-truth ``/model/<id>/odometry`` (robots that appear later
are picked up) and are redrawn once a second.

Markers go to the GUI's ``/marker_array`` service with GUI-only visibility: they are never
part of the world, so the robots' rendering-based LiDARs and cameras cannot see them. The
GUI may start later or restart, so the set is re-sent periodically (ADD_MODIFY: no flicker).

    ros2 run a6g_gz_bringup campus_markers --file <world>.markers.json
"""

import argparse
import json
import re
import threading
import time

from gz.msgs10.boolean_pb2 import Boolean
from gz.msgs10.marker_pb2 import Marker
from gz.msgs10.marker_v_pb2 import Marker_V
from gz.msgs10.odometry_pb2 import Odometry
from gz.transport13 import Node

from a6g_gz_bringup.overlay import Trails, overlay_specs

ODOM = re.compile(r"^/model/([^/]+)/odometry$")

NS_PREFIX = "a6g_campus/"


def to_msg(specs) -> Marker_V:
    out = Marker_V()
    for s in specs:
        m = out.marker.add()
        m.ns, m.id = NS_PREFIX + s.ns, s.id
        m.action = Marker.ADD_MODIFY
        m.type = Marker.Type.Value(s.type)
        m.visibility = Marker.GUI
        m.pose.position.x, m.pose.position.y, m.pose.position.z = s.pose
        m.pose.orientation.w = 1.0
        m.scale.x, m.scale.y, m.scale.z = s.scale
        # Emissive: flat map colours whatever the lighting; double-sided: flat shapes and
        # stroke triangles show whichever way they are wound.
        for color in (m.material.ambient, m.material.diffuse, m.material.emissive):
            color.r, color.g, color.b, color.a = s.rgba
        m.material.double_sided = True
        for x, y, z in s.points:
            p = m.point.add()
            p.x, p.y, p.z = x, y, z
        if s.text:
            m.text = s.text
    return out


class TrailFeed:
    """Subscribes to every model's ground-truth odometry as it appears."""

    def __init__(self, node: Node, trails: Trails) -> None:
        self.node, self.trails = node, trails
        self.lock = threading.Lock()
        self.robots: set[str] = set()

    def discover(self) -> None:
        for topic in self.node.topic_list():
            m = ODOM.match(topic)
            if m and m.group(1) not in self.robots:
                rid = m.group(1)
                self.robots.add(rid)
                self.node.subscribe(Odometry, topic, lambda msg, r=rid: self._on(r, msg))

    def _on(self, rid: str, msg: Odometry) -> None:
        with self.lock:
            self.trails.add(rid, msg.pose.position.x, msg.pose.position.y)

    def message(self) -> Marker_V | None:
        with self.lock:
            return to_msg(self.trails.specs()) if self.trails.changed else None


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--file", required=True)
    p.add_argument("--period", type=float, default=10.0, help="campus re-send period (s)")
    p.add_argument("--ros-args", nargs="*", help=argparse.SUPPRESS)  # ros2 launch adds it
    args, _ = p.parse_known_args()
    with open(args.file) as f:
        campus = json.load(f)
    msg = to_msg(overlay_specs(campus))
    node = Node()
    feed = TrailFeed(node, Trails(scale=float(campus.get("scale") or 1.0)))
    sent_at, shown = 0.0, False
    while True:
        now = time.monotonic()
        if now - sent_at >= (args.period if shown else 2.0):
            feed.discover()
            ok, rep = node.request("/marker_array", msg, Marker_V, Boolean, 2000)
            if ok and rep.data and not shown:
                print(f"campus overlay: {len(msg.marker)} markers in the Gazebo GUI", flush=True)
                feed.trails.changed = True  # a (re)started GUI gets the whole trails again
            shown, sent_at = bool(ok and rep.data), now
        trails = feed.message() if shown else None
        if trails is not None and len(trails.marker):
            node.request("/marker_array", trails, Marker_V, Boolean, 2000)
        time.sleep(1.0)


if __name__ == "__main__":
    main()
