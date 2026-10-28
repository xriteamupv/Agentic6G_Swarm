"""MCP server exposing exactly the skill allow-list.

There is no generic publish/call tool. Arming is *not* an MCP tool: agents must never be
able to arm a robot. It is served on a separate operator HTTP channel
(``/operator/arm|disarm|status``) guarded by a bearer token from ``A6G_OPERATOR_TOKEN``,
used only by the ``a6g`` CLI. With no token configured the channel is closed.
"""

import hmac
import os
from datetime import datetime
from typing import Any

from a6g_common.schemas import Pose2D, RobotState, SkillName, SkillResult
from mcp.server.mcpserver import Context, MCPServer
from mcp_types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from a6g_skill_mcp.service import (
    MAX_EVENT_IMAGE_KB,
    MISSION_META_KEY,
    SkillService,
    current_mission,
)

OPERATOR_TOKEN_ENV = "A6G_OPERATOR_TOKEN"

_READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
_MOTION = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
_SAFETY = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


def _bind_mission(ctx: Context) -> None:
    """Attribute this call's Decision to the caller's mission (from the request ``_meta``)."""
    meta = ctx.request_context.meta or {}
    current_mission.set(meta.get(MISSION_META_KEY))


def build_mcp_server(service: SkillService, operator_token: str | None = None) -> MCPServer:
    """Create the skill MCP server for one robot."""
    robot_id = service.manifest.robot_id
    server = MCPServer(
        name=f"a6g-skills-{robot_id}",
        title=f"Agentic6G skills for {robot_id}",
        instructions=(
            "Coarse robot skills only. Motion skills require the robot to be armed by the "
            "operator; a disarmed robot answers outcome='refused_disarmed'. Poses are in "
            "the campus_map frame. Results are summaries (SkillResult)."
        ),
        version="0.1.0",
    )

    @server.tool(name=SkillName.GET_STATE.value, annotations=_READ_ONLY)
    async def get_state(ctx: Context) -> RobotState:
        """Return the robot's current state (pose, battery, link, armed)."""
        _bind_mission(ctx)
        return await service.get_state()

    @server.tool(name=SkillName.NAVIGATE_TO.value, annotations=_MOTION)
    async def navigate_to(
        ctx: Context, pose: Pose2D | None = None, poi_id: str | None = None
    ) -> SkillResult:
        """Navigate to a campus_map pose OR a known POI, and wait for the result."""
        _bind_mission(ctx)
        return await service.navigate_to(pose=pose, poi_id=poi_id)

    @server.tool(name=SkillName.FOLLOW_ROUTE.value, annotations=_MOTION)
    async def follow_route(ctx: Context, waypoints: list[Pose2D]) -> SkillResult:
        """Visit up to 20 waypoints in order; stops at the first failure."""
        _bind_mission(ctx)
        return await service.follow_route(waypoints)

    @server.tool(name=SkillName.INSPECT.value, annotations=_MOTION)
    async def inspect(
        ctx: Context, poi_id: str, dwell_s: float, viewpoint: Pose2D | None = None
    ) -> SkillResult:
        """Go to a POI viewpoint (or the given one, when several robots share the POI), face
        it, dwell, capture an event image (URI returned)."""
        _bind_mission(ctx)
        return await service.inspect(poi_id, dwell_s, viewpoint)

    @server.tool(name=SkillName.HOLD_POST.value, annotations=_MOTION)
    async def hold_post(ctx: Context, pose: Pose2D, until: datetime) -> SkillResult:
        """Navigate to a pose and hold it until a UTC time; cancellable."""
        _bind_mission(ctx)
        return await service.hold_post(pose, until)

    @server.tool(name=SkillName.RETURN_HOME.value, annotations=_MOTION)
    async def return_home(ctx: Context) -> SkillResult:
        """Navigate to the configured staging pose."""
        _bind_mission(ctx)
        return await service.return_home()

    @server.tool(name=SkillName.CANCEL.value, annotations=_SAFETY)
    async def cancel(ctx: Context) -> SkillResult:
        """Cancel the current skill."""
        _bind_mission(ctx)
        return await service.cancel()

    @server.tool(name=SkillName.STOP.value, annotations=_SAFETY)
    async def stop(ctx: Context) -> SkillResult:
        """Immediate stop (cancel navigation + platform stop). Allowed while disarmed."""
        _bind_mission(ctx)
        return await service.stop()

    @server.tool(name=SkillName.CAPTURE_EVENT_IMAGE.value, annotations=_READ_ONLY)
    async def capture_event_image(ctx: Context, max_kb: int = MAX_EVENT_IMAGE_KB) -> SkillResult:
        """Capture a JPEG (<= 300 KB) from the configured camera; returns its URI."""
        _bind_mission(ctx)
        return await service.capture_event_image(max_kb)

    _add_operator_routes(server, service, operator_token)
    return server


def _add_operator_routes(
    server: MCPServer, service: SkillService, operator_token: str | None
) -> None:
    token = operator_token if operator_token is not None else os.environ.get(OPERATOR_TOKEN_ENV)

    def _authorized(request: Request) -> Response | None:
        if not token:
            return JSONResponse({"error": "operator channel disabled (no token)"}, status_code=503)
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(supplied.encode(), token.encode()):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return None

    def _status() -> dict[str, Any]:
        return {
            "robot_id": service.manifest.robot_id,
            "armed": service.armed,
            "armed_by": service.arming.armed_by,
        }

    async def _operator(request: Request) -> str:
        try:
            body = await request.json()
        except ValueError:
            body = {}
        return str(body.get("operator") or "unknown")

    @server.custom_route("/operator/arm", methods=["POST"], include_in_schema=False)
    async def arm(request: Request) -> Response:
        if (denied := _authorized(request)) is not None:
            return denied
        service.arm(await _operator(request))
        return JSONResponse(_status())

    @server.custom_route("/operator/disarm", methods=["POST"], include_in_schema=False)
    async def disarm(request: Request) -> Response:
        if (denied := _authorized(request)) is not None:
            return denied
        service.disarm(await _operator(request))
        return JSONResponse(_status())

    @server.custom_route("/operator/status", methods=["GET"], include_in_schema=False)
    async def status(request: Request) -> Response:
        if (denied := _authorized(request)) is not None:
            return denied
        return JSONResponse(_status())

    @server.custom_route("/health", methods=["GET"], include_in_schema=False)
    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok", "robot_id": service.manifest.robot_id})
