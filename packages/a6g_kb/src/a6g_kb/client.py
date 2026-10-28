"""Client of the knowledge server; every call is measured as pattern P10."""

import asyncio
import json
import time
from typing import Any

from a6g_common.clock import time_flows
from a6g_common.schemas import StatusReport
from a6g_observability.comms import comms_log
from mcp import Client

from a6g_kb.server import AGENT_META_KEY
from a6g_kb.service import KnowledgeUnavailable


class KnowledgeClient:
    """Calls to the knowledge server. ``target``: URL or in-process MCPServer.

    Each call opens its own short MCP session: knowledge calls are infrequent, and a session
    shared across tasks would break anyio's task-bound cancel scopes. The session setup is
    part of what P10 measures."""

    def __init__(self, target: Any, *, agent_id: str, timeout_s: float = 3.0) -> None:
        self._target = target
        self.agent_id = agent_id
        self.timeout_s = timeout_s

    async def __aenter__(self) -> "KnowledgeClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def call(self, tool: str, args: dict[str, Any], mission_id: str | None = None) -> Any:
        started = time.perf_counter()
        request = json.dumps({"tool": tool, "args": args}, separators=(",", ":"), default=str)
        outcome, reply = "ok", ""
        try:
            async with (
                time_flows(),
                asyncio.timeout(self.timeout_s),
                Client(self._target) as session,
            ):
                result = await session.call_tool(tool, args, meta={AGENT_META_KEY: self.agent_id})
            if result.is_error or result.structured_content is None:
                text = " ".join(getattr(c, "text", "") for c in result.content)
                outcome, reply = "failed", text
                raise KnowledgeUnavailable(f"{tool}: {text}")
            content = result.structured_content
            reply = json.dumps(content, separators=(",", ":"), default=str)
            return content
        except KnowledgeUnavailable:
            raise
        except Exception as exc:
            outcome = "unavailable"
            raise KnowledgeUnavailable(f"{tool}: {exc!r}") from exc
        finally:
            _record(self.agent_id, tool, mission_id, request, reply, started, outcome)

    # Typed helpers ----------------------------------------------------------

    async def search(self, query: str, k: int = 5, **filters: Any) -> list[dict[str, Any]]:
        args: dict[str, Any] = {"query": query, "k": k}
        if filters:
            args["filters"] = filters
        return (await self.call("kb_search", args))["passages"]

    async def resolve_place(self, text: str, mission_id: str | None = None) -> list[dict]:
        return (await self.call("kb_resolve_place", {"text": text}, mission_id))["candidates"]

    async def status(self, subjects: list[str]) -> dict[str, dict[str, Any]]:
        return (await self.call("kb_status", {"subjects": subjects}))["statuses"]

    async def plan_route(
        self, start: Any, goal: Any, profile: str = "default", avoid: list[str] | None = None
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"start": start, "goal": goal, "profile": profile}
        if avoid:
            args["avoid"] = avoid
        return await self.call("plan_route", args)

    async def nearest_shelter(self, start: Any, profile: str = "default") -> dict[str, Any]:
        return await self.call("nearest_shelter", {"start": start, "profile": profile})

    async def route_compliance(
        self, segments: list[dict[str, Any]], profile: str = "default"
    ) -> dict[str, Any]:
        return await self.call("kb_route_compliance", {"segments": segments, "profile": profile})

    async def add_notice(self, doc: dict[str, Any], status: str | None = None) -> dict:
        args: dict[str, Any] = {"doc": doc}
        if status:
            args["status"] = status
        return await self.call("kb_add_notice", args)

    async def add_observation(self, obs: dict[str, Any], mission_id: str | None = None) -> dict:
        return await self.call("kb_add_observation", {"obs": obs}, mission_id)

    async def observations_near(
        self, pose: dict[str, float], query: str = "", radius_m: float | None = None, k: int = 5
    ) -> list[dict[str, Any]]:
        args: dict[str, Any] = {"pose": pose, "query": query, "k": k}
        if radius_m is not None:
            args["radius_m"] = radius_m
        return (await self.call("kb_observations_near", args))["observations"]

    async def put_status(self, report: StatusReport, mission_id: str | None = None) -> dict:
        return await self.call(
            "kb_put_status", {"report": report.model_dump(mode="json")}, mission_id
        )


def _record(
    agent: str,
    tool: str,
    mission_id: str | None,
    request: str,
    reply: str,
    started: float,
    outcome: str,
) -> None:
    """P10: agent -> knowledge tool, request/response."""
    e2e = round((time.perf_counter() - started) * 1000.0, 3)
    log = comms_log()
    base = {
        "ts": time.time(),
        "agent": agent,
        "conversation_id": mission_id,
        "mission_id": mission_id,
        "pattern": "P10",
        "binding": "mcp",
        "encoding": "json",
        "nl_bytes": 0,
        "attempt": 1,
    }
    msg_id = f"kb-{time.time_ns()}"
    log.record(
        {
            **base,
            "direction": "send",
            "msg_id": msg_id,
            "in_reply_to": None,
            "sender": agent,
            "receivers": ["kb"],
            "performative": "query",
            "schema": tool,
            "payload_bytes": len(request.encode()),
            "wire_bytes": len(request.encode()),
            "e2e_ms": e2e,
            "outcome": outcome,
        }
    )
    if reply:
        log.record(
            {
                **base,
                "direction": "reply",
                "msg_id": f"{msg_id}-r",
                "in_reply_to": msg_id,
                "sender": "kb",
                "receivers": [agent],
                "performative": "inform",
                "schema": f"{tool}_reply",
                "payload_bytes": len(reply.encode()),
                "wire_bytes": len(reply.encode()),
                "outcome": outcome,
            }
        )
