"""sport_api_sim: the dock's Sport API, accepting only StopMove (api_id 1003).

StopMove stops the proxy (``stop`` to cmd_vel_to_sport_sim) and is answered on
``/api/sport/response``. Any other api_id is a violation: logged, counted, never executed.
The count is published (latched) on ``~/violations`` for tests.
"""

import rclpy
import rclpy.executors
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile
from std_msgs.msg import Empty, UInt32
from unitree_api.msg import Request, Response

from a6g_gz_go2_shim.logic import SportApiGuard


class SportApiSim(Node):
    def __init__(self) -> None:
        super().__init__("sport_api_sim")
        self.guard = SportApiGuard()
        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.stop_pub = self.create_publisher(Empty, "stop", 10)
        self.resp_pub = self.create_publisher(Response, "/api/sport/response", 10)
        self.count_pub = self.create_publisher(UInt32, "~/violations", latched)
        self.create_subscription(Request, "/api/sport/request", self._on_request, 10)
        self._publish_count()

    def _publish_count(self) -> None:
        self.count_pub.publish(UInt32(data=self.guard.violations))

    def _on_request(self, msg: Request) -> None:
        api_id = int(msg.header.identity.api_id)
        resp = Response()
        resp.header.identity = msg.header.identity
        if self.guard.handle(api_id):
            self.stop_pub.publish(Empty())
            resp.header.status.code = 0
            self.get_logger().info("StopMove: proxy stopped")
        else:
            resp.header.status.code = 3203  # rejected; never executed
            self.get_logger().error(
                f"Sport API VIOLATION: api_id {api_id} (only StopMove 1003 is allowed); "
                f"count={self.guard.violations}"
            )
            self._publish_count()
        if not msg.header.policy.noreply:
            self.resp_pub.publish(resp)


def main() -> None:
    rclpy.init()
    node = SportApiSim()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, rclpy.executors.ExternalShutdownException):
        pass
    except Exception:
        if rclpy.ok():  # otherwise a shutdown race inside spin()
            raise
    finally:
        node.get_logger().info(f"sport_api_sim violations: {node.guard.violations}")
        node.destroy_node()
        rclpy.try_shutdown()
