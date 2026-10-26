"""HTTP API of the InteractionStore (SPEC A3.5): create, get, update, delete, restore.

The caller identifies itself with the ``X-A6G-Agent`` header (stand-in for the mutual-TLS
identities of SPEC A3.9). Bodies and responses are never logged.
"""

from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from a6g_interactions.store import InteractionDenied, InteractionNotFound, InteractionStore

AGENT_HEADER = "X-A6G-Agent"


def build_interactions_app(store: InteractionStore) -> Starlette:
    def caller(request: Request) -> str:
        return request.headers.get(AGENT_HEADER, "")

    def guarded(fn):
        async def endpoint(request: Request) -> Response:
            try:
                return JSONResponse(await fn(request))
            except InteractionNotFound:
                return JSONResponse({"error": "not found"}, status_code=404)
            except InteractionDenied as exc:
                return JSONResponse({"error": str(exc)}, status_code=403)
            except (ValueError, KeyError, TypeError) as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)

        return endpoint

    async def create(request: Request) -> dict[str, Any]:
        body = await request.json()
        return store.create(
            body["interaction_id"], body.get("content") or {}, body["participants"], caller(request)
        )

    async def restore(request: Request) -> dict[str, Any]:
        return store.restore(await request.json(), caller(request))

    async def one(request: Request) -> dict[str, Any]:
        iid = request.path_params["interaction_id"]
        if request.method == "GET":
            return store.get(iid, caller(request))
        if request.method == "DELETE":
            return {"deleted": store.delete(iid, caller(request))}
        body = await request.json()
        return store.update(
            iid, caller(request), content=body.get("content"), participants=body.get("participants")
        )

    async def health(_request: Request) -> dict[str, Any]:
        return {"ok": True, "entries": len(store)}

    return Starlette(
        routes=[
            Route("/interactions", guarded(create), methods=["POST"]),
            Route("/interactions/restore", guarded(restore), methods=["POST"]),
            Route(
                "/interactions/{interaction_id}", guarded(one), methods=["GET", "PATCH", "DELETE"]
            ),
            Route("/health", guarded(health), methods=["GET"]),
        ]
    )
