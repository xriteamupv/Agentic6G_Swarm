"""``python3 -m a6g_ros_adapter --config <robot>.json [--dry-run]`` (robot ROS Python).

The JSON config comes from ``uv run a6g adapter-config <robot_id> --profile <profile>``
(manifest + profile, one source of truth). Start-up order:

1. join the robot's ROS domain and wait ``clock_check_s`` for discovery;
2. simulation-clock guard: ``real`` with a /clock publisher, or ``gazebo`` without one,
   refuses to start (exit code 3);
3. serve the JSON-lines protocol on the Unix socket until SIGINT/SIGTERM.
"""

import argparse
import json
import logging
import signal
import sys
import threading
import time

from a6g_ros_adapter.core import AdapterCore, ProfileGuardError, check_profile
from a6g_ros_adapter.protocol import AdapterServer

log = logging.getLogger("a6g_ros_adapter")

EXIT_PROFILE_GUARD = 3


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="a6g_ros_adapter", description=__doc__)
    p.add_argument("--config", required=True, help="adapter JSON (a6g adapter-config)")
    p.add_argument("--dry-run", action="store_true", help="log intended actions, send nothing")
    p.add_argument("--socket", default=None, help="override the socket path")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(), format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    with open(args.config) as f:
        config = json.load(f)
    socket_path = args.socket or config["socket"]

    import rclpy
    from rclpy.executors import MultiThreadedExecutor

    from a6g_ros_adapter.ros_io import RosIO

    rclpy.init()
    io = RosIO(config)
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(io.node)
    spinner = threading.Thread(target=executor.spin)
    spinner.daemon = True
    spinner.start()
    server = None
    try:
        time.sleep(float(config.get("clock_check_s", 3.0)))
        try:
            log.warning(check_profile(config["profile"], io.clock_publishers()))
        except ProfileGuardError as exc:
            log.error("%s: %s", config["robot_id"], exc)
            return EXIT_PROFILE_GUARD
        core = AdapterCore(config, io, dry_run=args.dry_run)
        server = AdapterServer(socket_path, core)
        log.warning(
            "%s adapter (%s%s) serving on %s",
            config["robot_id"], config["profile"], ", DRY-RUN" if args.dry_run else "", socket_path,
        )  # fmt: skip

        def _shutdown(signum, frame):
            threading.Thread(target=server.shutdown).start()

        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)
        server.serve_forever(poll_interval=0.5)
        return 0
    finally:
        if server is not None:
            server.server_close()
        # Order matters: stop spinning and join before destroying the node, else the
        # executor's wait set outlives its entities and the process aborts.
        executor.shutdown(timeout_sec=5.0)
        spinner.join(5.0)
        io.close()
        if rclpy.ok():  # Foxy has no try_shutdown
            rclpy.shutdown()


if __name__ == "__main__":
    sys.exit(main())
