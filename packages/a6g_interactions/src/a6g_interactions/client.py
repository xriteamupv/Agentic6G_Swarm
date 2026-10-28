"""InteractionStore client for agents: over HTTP at the edge, or the store object itself in
single-process stacks. Errors: :class:`InteractionNotFound`, :class:`InteractionDenied`,
:class:`StoreUnavailable` (no answer within ``timeout_s``)."""

import asyncio
from typing import Any

import httpx

from a6g_interactions.server import AGENT_HEADER
from a6g_interactions.store import InteractionDenied, InteractionNotFound, InteractionStore


class StoreUnavailable(ConnectionError):
    """The store did not answer (down, or past the timeout)."""


class InteractionClient:
    def __init__(self, target: Any, *, agent_id: str, timeout_s: float = 3.0) -> None:
        self.agent_id = agent_id
        self.timeout_s = timeout_s
        self._store = target if isinstance(target, InteractionStore) else None
        self._target = target

    # ------------------------------------------------------------------ transport

    async def _http(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        if isinstance(self._target, str):
            transport, base = None, self._target.rstrip("/")
        else:  # an ASGI app (tests)
            transport, base = httpx.ASGITransport(app=self._target), "http://interactions"
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url=base, timeout=self.timeout_s
            ) as http:
                r = await http.request(
                    method, path, json=body, headers={AGENT_HEADER: self.agent_id}
                )
        except (httpx.HTTPError, OSError) as exc:
            raise StoreUnavailable(f"interaction store unavailable: {exc}") from exc
        if r.status_code == 404:
            raise InteractionNotFound(path.rsplit("/", 1)[-1])
        if r.status_code == 403:
            raise InteractionDenied(r.json().get("error", "denied"))
        if r.status_code >= 400:
            raise ValueError(r.json().get("error", r.text))
        return r.json()

    async def _call(self, direct: Any, method: str, path: str, body: Any = None) -> Any:
        """``direct(store)`` in-process, else the HTTP request."""
        if self._store is not None:
            if self.down:
                raise StoreUnavailable("interaction store unavailable (simulated)")
            return direct(self._store)
        try:
            async with asyncio.timeout(self.timeout_s + 1.0):
                return await self._http(method, path, body)
        except TimeoutError as exc:
            raise StoreUnavailable("interaction store timed out") from exc

    #: Tests: an in-process store that does not answer.
    down = False

    # ------------------------------------------------------------------ API

    async def create(
        self, interaction_id: str, content: dict[str, Any], participants: list[str]
    ) -> dict[str, Any]:
        body = {"interaction_id": interaction_id, "content": content, "participants": participants}
        return await self._call(
            lambda s: s.create(interaction_id, content, participants, self.agent_id),
            "POST",
            "/interactions",
            body,
        )

    async def restore(self, entry: dict[str, Any]) -> dict[str, Any]:
        return await self._call(
            lambda s: s.restore(entry, self.agent_id), "POST", "/interactions/restore", entry
        )

    async def get(self, interaction_id: str) -> dict[str, Any]:
        return await self._call(
            lambda s: s.get(interaction_id, self.agent_id), "GET", f"/interactions/{interaction_id}"
        )

    async def update(
        self,
        interaction_id: str,
        content: dict[str, Any] | None = None,
        participants: list[str] | None = None,
    ) -> dict[str, Any]:
        return await self._call(
            lambda s: s.update(interaction_id, self.agent_id, content, participants),
            "PATCH",
            f"/interactions/{interaction_id}",
            {"content": content, "participants": participants},
        )

    async def delete(self, interaction_id: str) -> bool:
        out = await self._call(
            lambda s: s.delete(interaction_id, self.agent_id),
            "DELETE",
            f"/interactions/{interaction_id}",
        )
        return out if isinstance(out, bool) else bool(out.get("deleted"))
