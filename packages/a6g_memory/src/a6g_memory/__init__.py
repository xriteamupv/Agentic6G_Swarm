"""Shared memory stand-in, to be replaced by the AgenticOps memory service.

``SharedMemory`` is the interface the agents use; ``StoreSharedMemory`` implements it on a
LangGraph ``BaseStore`` (in-memory in tests, Postgres-backed under the Agent Server). Maps
are stored only as references and change annotations, never geometry.
"""

from typing import Any, Protocol

from langgraph.store.base import BaseStore

NS = "a6g"


class SharedMemory(Protocol):
    async def put_context(self, scope: str, key: str, value: dict[str, Any]) -> None: ...

    async def get_context(self, scope: str, key: str) -> dict[str, Any] | None: ...

    async def put_ltm(self, kind: str, key: str, value: dict[str, Any]) -> None: ...

    async def query_ltm(
        self, kind: str, filter: dict[str, Any] | None = None, limit: int = 20
    ) -> list[dict[str, Any]]: ...


class StoreSharedMemory:
    """Context = short-lived per-scope state (e.g. the mission registry); LTM = durable facts
    (confirmed observations, map change annotations)."""

    def __init__(self, store: BaseStore) -> None:
        self.store = store

    async def put_context(self, scope: str, key: str, value: dict[str, Any]) -> None:
        await self.store.aput((NS, "context", scope), key, value)

    async def get_context(self, scope: str, key: str) -> dict[str, Any] | None:
        item = await self.store.aget((NS, "context", scope), key)
        return item.value if item else None

    async def list_context(self, scope: str, limit: int = 100) -> list[dict[str, Any]]:
        items = await self.store.asearch((NS, "context", scope), limit=limit)
        return [i.value for i in items]

    async def put_ltm(self, kind: str, key: str, value: dict[str, Any]) -> None:
        await self.store.aput((NS, "ltm", kind), key, value)

    async def query_ltm(
        self, kind: str, filter: dict[str, Any] | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        items = await self.store.asearch((NS, "ltm", kind), filter=filter, limit=limit)
        return [i.value for i in items]
