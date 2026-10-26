"""Chat runs must not fork a mission thread from a stale checkpoint (lost robot reports)."""

from a6g_coordinator.gateway import LatestCheckpointOnly
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient


async def echo(request: Request) -> JSONResponse:
    return JSONResponse(await request.json())


APP = Starlette(
    routes=[
        Route("/threads/{tid}/runs", echo, methods=["POST"]),
        Route("/threads/{tid}/runs/stream", echo, methods=["POST"]),
        Route("/threads/{tid}/history", echo, methods=["POST"]),
    ],
    middleware=[Middleware(LatestCheckpointOnly)],
)
PINNED = {
    "input": {"messages": [{"type": "human", "content": "status"}]},
    "checkpoint": {"checkpoint_id": "1f1c", "checkpoint_ns": ""},
    "checkpoint_id": "1f1c",
    "config": {"configurable": {"checkpoint_id": "1f1c", "user": "op"}},
}


def test_run_requests_are_unpinned() -> None:
    client = TestClient(APP)
    for path in ("/threads/t1/runs", "/threads/t1/runs/stream"):
        body = client.post(path, json=PINNED).json()
        assert "checkpoint" not in body and "checkpoint_id" not in body
        assert body["config"]["configurable"] == {"user": "op"}
        assert body["input"] == PINNED["input"]


def test_opt_in_and_other_routes_untouched() -> None:
    client = TestClient(APP)
    forked = client.post("/threads/t1/runs", json=PINNED, headers={"x-a6g-allow-fork": "1"})
    assert forked.json() == PINNED
    assert client.post("/threads/t1/history", json=PINNED).json() == PINNED
