"""JSON-lines over a Unix socket.

Request ``{"id", "op", "args"}`` -> response ``{"id", "ok", "result" | "error"}``, one JSON
object per line. Requests on one connection run concurrently (a ``cancel`` must overtake a
running ``navigate``); responses carry the request id. Python 3.8 compatible, stdlib only.
"""

import json
import logging
import os
import socketserver
import stat
import threading
from typing import Any, Dict

from a6g_ros_adapter.core import AdapterCore, AdapterError

log = logging.getLogger("a6g_ros_adapter")


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        lock = threading.Lock()
        for raw in self.rfile:
            try:
                req = json.loads(raw.decode("utf-8"))
                if not isinstance(req, dict):
                    raise ValueError("request must be an object")
            except ValueError as exc:
                self._send(lock, {"id": None, "ok": False, "error": f"bad request: {exc}"})
                continue
            t = threading.Thread(target=self._serve, args=(req, lock))
            t.daemon = True
            t.start()

    def _serve(self, req: Dict[str, Any], lock: threading.Lock) -> None:
        core = self.server.core  # type: ignore[attr-defined]
        rid = req.get("id")
        try:
            result = core.handle(req.get("op"), req.get("args") or {})
            resp = {"id": rid, "ok": True, "result": result}
        except AdapterError as exc:
            resp = {"id": rid, "ok": False, "error": str(exc)}
        except Exception as exc:  # never kill the server on a request
            log.exception("op %r failed", req.get("op"))
            resp = {"id": rid, "ok": False, "error": f"internal error: {exc}"}
        self._send(lock, resp)

    def _send(self, lock: threading.Lock, resp: Dict[str, Any]) -> None:
        data = (json.dumps(resp) + "\n").encode("utf-8")
        with lock:
            try:
                self.wfile.write(data)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ValueError):
                pass  # client went away


class AdapterServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path: str, core: AdapterCore) -> None:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        if os.path.exists(path):
            if not stat.S_ISSOCK(os.stat(path).st_mode):
                raise RuntimeError(f"{path} exists and is not a socket")
            os.unlink(path)  # stale socket from a previous run
        self.core = core
        socketserver.ThreadingUnixStreamServer.__init__(self, path, _Handler)
        os.chmod(path, 0o660)

    def server_close(self) -> None:
        path = self.server_address
        socketserver.ThreadingUnixStreamServer.server_close(self)
        if isinstance(path, str) and os.path.exists(path):
            os.unlink(path)
