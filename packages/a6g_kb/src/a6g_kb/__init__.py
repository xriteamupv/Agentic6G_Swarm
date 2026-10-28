"""Campus knowledge server: stand-in interface for the I11 RAG tool and the I4
knowledge services. A tool, not an agent."""

from a6g_kb.client import KnowledgeClient
from a6g_kb.server import build_kb_server
from a6g_kb.service import KnowledgeService, KnowledgeUnavailable

__all__ = ["KnowledgeClient", "KnowledgeService", "KnowledgeUnavailable", "build_kb_server"]
