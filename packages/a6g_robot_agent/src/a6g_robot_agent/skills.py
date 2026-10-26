"""MCP client for the robot's skill server (the agent's only way to act, SPEC Rule 2)."""

from typing import Any

from a6g_common.schemas import RobotState, SkillName, SkillOutcome, SkillResult
from mcp import Client


class SkillClient:
    """Keeps one MCP session open. ``target`` is a URL or an in-process MCPServer (tests)."""

    def __init__(self, target: Any) -> None:
        self._target = target
        self._client: Client | None = None

    async def __aenter__(self) -> "SkillClient":
        self._client = Client(self._target)
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.__aexit__(*exc)
            self._client = None

    def _session(self) -> Client:
        if self._client is None:
            raise RuntimeError("SkillClient not connected")
        return self._client

    async def call(
        self,
        tool: SkillName | str,
        args: dict[str, Any] | None = None,
        mission_id: str | None = None,
    ) -> SkillResult:
        skill = SkillName(tool)
        meta = {"a6g/mission_id": mission_id} if mission_id else None
        result = await self._session().call_tool(skill.value, args or {}, meta=meta)
        if result.is_error or result.structured_content is None:
            text = " ".join(getattr(c, "text", "") for c in result.content)
            return SkillResult(skill=skill, outcome=SkillOutcome.FAILED, detail=text, duration_s=0)
        return SkillResult.model_validate(result.structured_content)

    async def get_state(self) -> RobotState:
        result = await self._session().call_tool(SkillName.GET_STATE.value, {})
        return RobotState.model_validate(result.structured_content)
