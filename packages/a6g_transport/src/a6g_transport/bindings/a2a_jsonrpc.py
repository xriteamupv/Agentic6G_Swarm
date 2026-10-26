"""A2A JSON-RPC binding on the official ``a2a-sdk`` (SPEC A2.5.3 reference binding, 11).

Every message is one A2A ``Message`` with a single data part::

    {"a6g": "<AgentMessage JSON>"}

The canonical message travels as a JSON string so pydantic round-trips it exactly (a
protobuf ``Value`` would turn integers into floats). Replies are immediate ``Message``s with
the same shape. Agents publish an agent card at ``/.well-known/agent-card.json``.

Wire accounting: httpx event hooks measure every HTTP exchange of a request (request line,
headers and body each way), including the one-off agent-card fetch, so ``wire_bytes`` is
what this binding really puts on the link (TCP/IP and TLS framing excluded).
"""

import asyncio
import contextlib
import contextvars
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
from a2a.client import ClientConfig, ClientFactory
from a2a.helpers.proto_helpers import get_data_parts, new_data_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.request_handlers import DefaultRequestHandlerV2
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Role,
    SendMessageRequest,
)
from a6g_common.messages import AgentMessage
from starlette.applications import Starlette

log = logging.getLogger(__name__)

NAME = "a2a_jsonrpc"
RPC_PATH = "/a2a"
PROTOCOL_VERSION = "1.0"
MEDIA_TYPE = "application/vnd.a6g+json"
_KEY = "a6g"


class Unavailable(ConnectionError):
    """The peer could not be reached or did not answer in time."""


def encode(msg: AgentMessage, role: Role = Role.ROLE_AGENT):
    return new_data_message({_KEY: msg.to_json()}, media_type=MEDIA_TYPE, role=role)


def decode(parts: Sequence[Any]) -> AgentMessage:
    for data in get_data_parts(parts):
        if isinstance(data, dict) and _KEY in data:
            return AgentMessage.from_json(data[_KEY])
    raise ValueError("message carries no a6g data part")


def build_agent_card(
    name: str,
    description: str,
    base_url: str,
    skills: Sequence[tuple[str, str, str]],
    version: str = "0.1.0",
) -> AgentCard:
    """``skills`` are ``(id, name, description)`` triples."""
    return AgentCard(
        name=name,
        description=description,
        version=version,
        supported_interfaces=[
            AgentInterface(
                url=base_url.rstrip("/") + RPC_PATH,
                protocol_binding="JSONRPC",
                protocol_version=PROTOCOL_VERSION,
            )
        ],
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=[MEDIA_TYPE],
        default_output_modes=[MEDIA_TYPE],
        skills=[
            AgentSkill(id=i, name=n, description=d, tags=["agentic6g", "uc2"]) for i, n, d in skills
        ],
    )


# --------------------------------------------------------------------------- server

Handler = Callable[[AgentMessage], Awaitable[AgentMessage]]
#: Builds the reply for a request the handler could not serve (error text) .
ErrorReply = Callable[[AgentMessage, str], AgentMessage]


class _Executor(AgentExecutor):
    def __init__(self, handler: Handler, error_reply: ErrorReply) -> None:
        self._handler = handler
        self._error_reply = error_reply

    async def execute(self, context: RequestContext, event_queue) -> None:
        request = decode(context.message.parts)
        try:
            reply = await self._handler(request)
        except ConnectionError as exc:  # expected: the robot is unreachable (5G shadow, down)
            log.warning("a2a request not served: %s", exc)
            reply = self._error_reply(request, str(exc))
        except Exception as exc:  # report, never crash the server
            log.exception("a2a handler failed")
            reply = self._error_reply(request, str(exc))
        message = encode(reply)
        message.context_id = context.context_id or ""
        await event_queue.enqueue_event(message)
        mine = _FINISHED.get()  # the on_message_send call this request belongs to
        if mine is not None and getattr(context, "task_id", None):
            mine.append(context.task_id)

    async def cancel(self, context: RequestContext, event_queue) -> None:
        return None


#: Task ids finished within one ``on_message_send`` call. The SDK runs the executor in a
#: task created inside that call, so it inherits this context: each call releases only its
#: own tasks. (A list shared by the handler let one request release another's task while
#: that one was still waiting for its reply: the second request then hung for good.)
_FINISHED: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "a6g_a2a_finished", default=None
)


class _ReleasingHandler(DefaultRequestHandlerV2):
    """Releases each request's SDK ``ActiveTask`` once its reply is out.

    Our servers answer every request with a single message (message mode). a2a-sdk 1.2.2
    then keeps the request's ActiveTask (event queues + dispatcher task) waiting for more
    requests on that task id, for the life of the server: one leaked set per robot report
    or envelope, and one "Dispatcher task is not running" warning each at shutdown. This
    closes them as the SDK's own registry ``aclose`` does, one task at a time. Uses the
    SDK's private ``_active_task_registry``; ``test_a2a_requests_do_not_accumulate`` guards
    it across SDK upgrades.
    """

    async def on_message_send(self, params: Any, context: Any) -> Any:
        mine: list[str] = []
        token = _FINISHED.set(mine)
        try:
            result = await super().on_message_send(params, context)
        finally:
            _FINISHED.reset(token)
        for task_id in mine:
            await self._release(task_id)
        return result

    async def _release(self, task_id: str) -> None:
        registry = self._active_task_registry
        active = await registry.get(task_id)
        if active is None:
            return
        with contextlib.suppress(Exception):
            await active.aclose()
        with registry._lock:
            registry._active_tasks.pop(task_id, None)


def routes(
    card: AgentCard,
    handler: Handler,
    error_reply: ErrorReply,
    prefix: str = "",
    *,
    handlers: list[Any] | None = None,
) -> list[Any]:
    """Agent card + JSON-RPC routes, optionally under ``prefix`` (to mount in another app).

    The SDK request handler is appended to ``handlers``: close it on shutdown with
    ``close_handlers`` (otherwise the SDK warns "Dispatcher task is not running" once per
    request queue still open when the event loop stops)."""
    from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH

    request_handler = _ReleasingHandler(_Executor(handler, error_reply), InMemoryTaskStore(), card)
    if handlers is not None:
        handlers.append(request_handler)
    return [
        *create_agent_card_routes(card, card_url=prefix + AGENT_CARD_WELL_KNOWN_PATH),
        *create_jsonrpc_routes(request_handler, prefix + RPC_PATH),
    ]


async def close_handlers(handlers: Sequence[Any]) -> None:
    """Drain the SDK handlers' active tasks (call from the app's lifespan, on shutdown)."""
    for request_handler in handlers:
        with contextlib.suppress(Exception):
            await request_handler.aclose()


def build_app(
    card: AgentCard,
    handler: Handler,
    error_reply: ErrorReply,
    extra_routes: Sequence[Any] = (),
    handlers: list[Any] | None = None,
) -> Starlette:
    """A Starlette app serving ``card``. ``handlers`` (optional) collects the SDK handlers
    for callers that serve the app without its lifespan (in-process ASGI transport)."""
    collected: list[Any] = handlers if handlers is not None else []

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        yield
        await close_handlers(collected)

    return Starlette(
        routes=[*routes(card, handler, error_reply, handlers=collected), *extra_routes],
        lifespan=lifespan,
    )


# --------------------------------------------------------------------------- client


@dataclass
class WireStats:
    """What one request put on the wire, both ways (all HTTP exchanges it caused)."""

    request_bytes: int = 0
    response_bytes: int = 0
    exchanges: int = 0
    first_send_at: float | None = None  # perf_counter when the first HTTP request left
    paths: list[str] = field(default_factory=list)


_CURRENT: contextvars.ContextVar[WireStats | None] = contextvars.ContextVar(
    "a6g_a2a_wire", default=None
)


def _headers_bytes(headers: httpx.Headers) -> int:
    return sum(len(k) + len(v) + 4 for k, v in headers.raw) + 2


async def _on_request(request: httpx.Request) -> None:
    stats = _CURRENT.get()
    if stats is None:
        return
    if stats.first_send_at is None:
        stats.first_send_at = time.perf_counter()
    line = len(f"{request.method} {request.url.raw_path.decode()} HTTP/1.1\r\n")
    stats.request_bytes += line + _headers_bytes(request.headers) + len(request.content)
    stats.exchanges += 1
    stats.paths.append(request.url.path)


async def _on_response(response: httpx.Response) -> None:
    stats = _CURRENT.get()
    if stats is None:
        return
    await response.aread()
    line = len(f"HTTP/1.1 {response.status_code} {response.reason_phrase}\r\n")
    stats.response_bytes += line + _headers_bytes(response.headers) + len(response.content)


class _ServerSideTransport(httpx.AsyncBaseTransport):
    """``httpx.ASGITransport`` whose server side runs in its own task, as across a network.

    A plain ASGI transport runs the app inside the caller's task, so cancelling a caller
    (e.g. a coordinator killed mid-request) also cancels the server half-way, including the
    SDK's event dispatcher, and later requests to that app then wait forever. Here a
    cancelled caller only abandons its reply; the server finishes the request on its own.
    """

    def __init__(self, app: Any) -> None:
        self._inner = httpx.ASGITransport(app=app)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        task = asyncio.ensure_future(self._inner.handle_async_request(request))
        task.add_done_callback(_consume)
        return await asyncio.shield(task)

    async def aclose(self) -> None:
        await self._inner.aclose()


def _consume(task: asyncio.Future) -> None:
    if not task.cancelled():
        task.exception()  # retrieved: an abandoned request's error is not "never retrieved"


def asgi_client(app: Any, base_url: str, timeout_s: float) -> httpx.AsyncClient:
    """An httpx client reaching ``app`` in-process (no sockets), for single-process stacks:
    the messages still go through the SDK and the JSON-RPC encoding, byte for byte."""
    return httpx.AsyncClient(
        transport=_ServerSideTransport(app), base_url=base_url, timeout=timeout_s
    )


class Connection:
    """Requests to one peer; the agent card is resolved once and cached.

    ``httpx_client`` may use any transport, e.g. ``httpx.ASGITransport`` to reach an
    in-process agent app through the real SDK and JSON-RPC encoding, without sockets."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 5.0,
        httpx_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._http = httpx_client or httpx.AsyncClient(timeout=timeout_s)
        self._http.event_hooks.setdefault("request", []).append(_on_request)
        self._http.event_hooks.setdefault("response", []).append(_on_response)
        self._client = None
        self._lock = asyncio.Lock()

    async def _get_client(self):
        async with self._lock:
            if self._client is None:
                factory = ClientFactory(ClientConfig(httpx_client=self._http, streaming=False))
                self._client = await factory.create_from_url(self.base_url)
            return self._client

    async def request(self, msg: AgentMessage) -> tuple[AgentMessage, WireStats]:
        """Send ``msg`` and return the peer's reply; raises :class:`Unavailable`."""
        stats = WireStats()
        token = _CURRENT.set(stats)
        try:
            async with asyncio.timeout(self.timeout_s):
                client = await self._get_client()
                request = SendMessageRequest(message=encode(msg, role=Role.ROLE_USER))
                async for event in client.send_message(request):
                    if event.HasField("message"):
                        return decode(event.message.parts), stats
        except (TimeoutError, httpx.HTTPError, OSError) as exc:
            raise Unavailable(f"{self.base_url}: {exc!r}") from exc
        except Exception as exc:  # SDK-level transport errors
            raise Unavailable(f"{self.base_url}: {exc!r}") from exc
        finally:
            _CURRENT.reset(token)
        raise Unavailable(f"{self.base_url}: no reply message")

    async def aclose(self) -> None:
        await self._http.aclose()
