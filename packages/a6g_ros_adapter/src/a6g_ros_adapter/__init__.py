"""ROS 2 adapter (Python 3.8-compatible, stdlib + rclpy only), SPEC 6.3.

Runs under the robot's ROS Python and serves the skill primitives to ``a6g_skill_mcp`` as
JSON lines on a Unix socket: ``python3 -m a6g_ros_adapter --config <robot>.json``.
"""
