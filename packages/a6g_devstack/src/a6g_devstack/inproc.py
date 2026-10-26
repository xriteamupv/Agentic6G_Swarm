"""In-process agent network for single-process stacks (SimStack, GzStack with an embedded
coordinator).

Coordinator, robots and peers talk through the real messaging layer and the
``a2a_jsonrpc`` binding, over an in-memory ASGI transport: the same SDK, encoding, wire
bytes and metrics as across hosts, without sockets. The stacks keep emulating the radio
link themselves (a robot in a coverage shadow is unreachable before anything is sent);
stage C3 moves that emulation into the binding layer.
"""

import os
from collections.abc import Callable
from typing import Any

from a6g_common.schemas import Report
from a6g_robot_agent.runtime import RobotAgent
from a6g_robot_agent.server import build_robot_app
from a6g_transport.messaging import (
    A6GMessage,
    MessageType,
    MessagingClient,
    build_agent_app,
    build_agent_card,
    close_handlers,
)

#: In-process requests never wait for a network: no deadline shorter than the slowest
#: agent step (a paused simulator, an LLM call), as when these were direct calls.
INPROC_TIMEOUT_S = float(os.environ.get("A6G_INPROC_TIMEOUT_S", "300"))
COORDINATOR_URL = "http://coordinator.inproc"


class _AgentProxy:
    """The robot's current agent process (it can crash and be rebuilt)."""

    def __init__(self, robot_id: str, resolve: Callable[[], RobotAgent]) -> None:
        self.robot_id = robot_id
        self._resolve = resolve
        self.deps = resolve().deps  # the agent card (built once) reads the manifest

    def __getattr__(self, name: str) -> Any:
        return getattr(self._resolve(), name)


class InProcessNet:
    """Clients between in-process agents, created on first use."""

    def __init__(
        self,
        robot: Callable[[str], RobotAgent],
        coordinator: Callable[[], Any],
    ) -> None:
        self._robot = robot  # robot_id -> its current agent (raises ConnectionError)
        self._coordinator = coordinator  # the running coordinator, or None
        self.handlers: list[Any] = []
        self._apps: dict[str, Any] = {}
        self._clients: dict[tuple[str, str], MessagingClient] = {}

    @staticmethod
    def robot_url(robot_id: str) -> str:
        return f"http://{robot_id}.inproc"

    def _app(self, agent_id: str) -> Any:
        app = self._apps.get(agent_id)
        if app is None:
            if agent_id == "coordinator":
                app = self._coordinator_app()
            else:
                proxy = _AgentProxy(agent_id, lambda: self._robot(agent_id))
                app = build_robot_app(
                    proxy,  # type: ignore[arg-type]
                    self.robot_url(agent_id),
                    handlers=self.handlers,
                )
            self._apps[agent_id] = app
        return app

    def client(self, sender: str, receiver: str) -> MessagingClient:
        key = (sender, receiver)
        client = self._clients.get(key)
        if client is None:
            url = COORDINATOR_URL if receiver == "coordinator" else self.robot_url(receiver)
            client = self._clients[key] = MessagingClient.in_process(
                self._app(receiver),
                url,
                sender=sender,
                receiver=receiver,
                timeout_s=INPROC_TIMEOUT_S,
            )
        return client

    def _coordinator_app(self) -> Any:
        async def handle(msg: A6GMessage) -> A6GMessage:
            if msg.type is not MessageType.REPORT:
                raise ConnectionError(f"in-process coordinator: unsupported {msg.type.value}")
            coordinator = self._coordinator()
            report = Report.model_validate(msg.payload)
            if coordinator is None or not await coordinator.handle_report(report):
                # Not taken (coordinator down, report outside a mission): the robot keeps
                # it queued, as the direct call's False did.
                raise ConnectionError("coordinator did not accept the report")
            return A6GMessage(MessageType.REPORT_ACK, {"report_id": report.report_id})

        card = build_agent_card(
            name="a6g-coordinator",
            description="Swarm Coordinator (in-process stack)",
            base_url=COORDINATOR_URL,
            skills=[("report", "Robot report", "Skill results, task status, observations.")],
        )
        return build_agent_app(card, handle, agent_id="coordinator", handlers=self.handlers)

    def forget(self, agent_id: str) -> None:
        """Drop the app of an agent rebuilt with a new identity (not needed for restarts)."""
        self._apps.pop(agent_id, None)

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()
        await close_handlers(self.handlers)
        self.handlers.clear()
        self._apps.clear()
