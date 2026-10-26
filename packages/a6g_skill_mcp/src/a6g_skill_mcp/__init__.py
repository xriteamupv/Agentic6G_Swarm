"""MCP skill server: SkillBackend interface, SimBackend and the 9-tool allow-list (SPEC 6)."""

from a6g_skill_mcp.backend import MotionResult, SkillBackend
from a6g_skill_mcp.server import build_mcp_server
from a6g_skill_mcp.service import SkillService
from a6g_skill_mcp.sim_backend import SimBackend

__all__ = ["MotionResult", "SimBackend", "SkillBackend", "SkillService", "build_mcp_server"]
