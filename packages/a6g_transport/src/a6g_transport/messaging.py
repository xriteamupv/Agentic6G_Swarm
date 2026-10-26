"""Messaging layer (SPEC A2.5, rule 22): the only way agents talk to each other.

Agents send typed :class:`A6GMessage`\\ s (requests: envelopes, reports, CFPs, awards,
state queries, operator commands) through a :class:`MessagingClient`, serve them with
:func:`agent_routes` / :func:`build_agent_app`, and publish or subscribe topics through a
:class:`MessagingBus`. This layer

* maps each message type and topic to its interaction pattern (P1-P9, A2.5.2);
* wraps it in the canonical :class:`~a6g_common.messages.AgentMessage`;
* picks the binding, encoding and NL policy from ``config/comms.yaml`` (A2.5.3);
* measures every message (rule 23) into :mod:`a6g_observability.comms`.

Transport libraries are imported only by :mod:`a6g_transport.bindings`.
"""

import json
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from a6g_common.clock import time_flows
from a6g_common.config import BindingName, CommsConfig, EncodingName, PatternSpec, Route
from a6g_common.messages import NL, AgentMessage, MessageHeader, Pattern, Performative, SchemaRef
from a6g_observability.comms import CommsLog, comms_log, configure_comms_log

from a6g_transport.bindings import a2a_jsonrpc as _a2a
from a6g_transport.bindings import mqtt as _mqtt
from a6g_transport.bindings.mqtt import BusMessage, TelemetryBus, consume

log = logging.getLogger(__name__)

__all__ = [
    "A2AUnavailable",
    "A6GMessage",
    "BusMessage",
    "MessageType",
    "MessagingBus",
    "MessagingClient",
    "TelemetryBus",
    "agent_routes",
    "build_agent_app",
    "build_agent_card",
    "close_handlers",
    "configure_messaging",
    "consume",
    "memory_bus",
    "mqtt_bus",
    "start_embedded_broker",
]

TOPIC_STATE = "a6g/fleet/{robot_id}/state"
TOPIC_STATUS = "a6g/fleet/{robot_id}/status"  # retained online/offline; offline is the last-will
TOPIC_ENVELOPE = "a6g/fleet/{robot_id}/envelope"  # retained summary of the accepted envelope
TOPIC_HEARTBEAT = "a6g/coordinator/heartbeat"
TOPIC_OBSERVATIONS = "a6g/observations"
TOPIC_DECISIONS = "a6g/decisions"
TOPIC_KILLSWITCH = "a6g/killswitch"

A2AUnavailable = _a2a.Unavailable
start_embedded_broker = _mqtt.start_embedded_broker
close_handlers = _a2a.close_handlers
build_agent_card = _a2a.build_agent_card


class MessageType(StrEnum):
    ENVELOPE = "envelope"
    ENVELOPE_REPLY = "envelope_reply"
    CFP = "cfp"
    BID = "bid"
    AWARD = "award"
    AWARD_ACK = "award_ack"
    REPORT = "report"
    REPORT_ACK = "report_ack"
    OPERATOR = "operator"
    STATE = "state"
    STATE_REPLY = "state_reply"
    QUEUE = "queue"  # the robot's own task order, for explanations
    QUEUE_REPLY = "queue_reply"
    OPERATOR_ACK = "operator_ack"
    ERROR = "error"


@dataclass(frozen=True)
class A6GMessage:
    """What agents send: a type, the authoritative structured payload and, optionally,
    model-written text (rule 24: data, never instructions)."""

    type: MessageType
    payload: dict[str, Any]
    nl: NL | None = None


# --------------------------------------------------------------------------- catalogue

_P = Pattern
_F = Performative
_TYPES: dict[MessageType, tuple[Pattern, Performative]] = {
    MessageType.ENVELOPE: (_P.P1_ENVELOPE, _F.REQUEST),
    MessageType.ENVELOPE_REPLY: (_P.P1_ENVELOPE, _F.ACK),
    MessageType.REPORT: (_P.P2_REPORT, _F.INFORM),
    MessageType.REPORT_ACK: (_P.P2_REPORT, _F.ACK),
    MessageType.CFP: (_P.P3_CONTRACT_NET, _F.CFP),
    MessageType.BID: (_P.P3_CONTRACT_NET, _F.BID),
    MessageType.AWARD: (_P.P3_CONTRACT_NET, _F.AWARD),
    MessageType.AWARD_ACK: (_P.P3_CONTRACT_NET, _F.ACK),
    MessageType.STATE: (_P.P4_STATE, _F.QUERY),
    MessageType.STATE_REPLY: (_P.P4_STATE, _F.INFORM),
    MessageType.QUEUE: (_P.P4_STATE, _F.QUERY),
    MessageType.QUEUE_REPLY: (_P.P4_STATE, _F.INFORM),
    MessageType.OPERATOR: (_P.P7_HUMAN, _F.REQUEST),
    MessageType.OPERATOR_ACK: (_P.P7_HUMAN, _F.ACK),
}
_TOPICS: tuple[tuple[str, Pattern, Performative], ...] = (
    (r"^a6g/fleet/[^/]+/(state|envelope)$", _P.P4_STATE, _F.INFORM),
    (r"^a6g/fleet/[^/]+/status$", _P.P5_LIVENESS, _F.INFORM),
    (r"^a6g/coordinator/heartbeat$", _P.P5_LIVENESS, _F.INFORM),
    (r"^a6g/killswitch$", _P.P8_KILLSWITCH, _F.REQUEST),
    (r"^a6g/(observations|decisions)$", _P.P2_REPORT, _F.INFORM),
    (r"^a6g/kb/status$", _P.P10_KNOWLEDGE, _F.INFORM),  # statuses pushed to robots (A3.4.5)
)


def classify(msg: A6GMessage) -> tuple[Pattern, Performative]:
    """The pattern and performative of a request or reply."""
    if msg.type is MessageType.REPORT and msg.payload.get("kind") == "observation":
        return _P.P6_ESCALATION, _F.INFORM  # an observation goes up with its evidence
    if msg.type is MessageType.REPORT and msg.payload.get("kind") == "human_request":
        return _P.P7_HUMAN, _F.REQUEST  # a person's request, relayed by the robot
    return _TYPES.get(msg.type, (_P.P4_STATE, _F.INFORM))


def topic_pattern(topic: str) -> tuple[Pattern, Performative]:
    for regex, pattern, performative in _TOPICS:
        if re.match(regex, topic):
            return pattern, performative
    return _P.P4_STATE, _F.INFORM


def _ids(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    """(mission id, conversation id) carried by a payload."""
    mission = payload.get("mission_id")
    if mission is None and isinstance(payload.get("envelope"), dict):
        mission = payload["envelope"].get("mission_id")
    conversation = payload.get("cfp_id") or mission
    return mission, conversation


def _retry_key(msg: A6GMessage) -> str | None:
    for key in ("report_id", "envelope_id", "cfp_id", "award_id"):
        if msg.payload.get(key):
            return f"{msg.type.value}:{msg.payload[key]}"
    return None


# --------------------------------------------------------------------------- routing


def _default_comms() -> CommsConfig:
    names = {
        "P1": "envelope dispatch",
        "P2": "report",
        "P3": "contract-net",
        "P4": "robot state",
        "P5": "liveness",
        "P6": "escalation with evidence",
        "P7": "human request",
        "P8": "kill-switch",
        "P9": "network intents",
        "P10": "knowledge query",
        "P11": "health report",
    }
    pubsub = {"P4", "P5", "P8", "P11"}
    return CommsConfig(
        patterns={p: PatternSpec(name=n, shape="") for p, n in names.items()},
        presets={
            "hybrid": {
                p: Route(
                    binding=BindingName.MQTT
                    if p in pubsub
                    else BindingName.MCP
                    if p == "P10"
                    else BindingName.A2A_JSONRPC
                )
                for p in names
            }
        },
        metrics={"path": ""},
    )


@dataclass
class _Settings:
    comms: CommsConfig
    log: CommsLog | None = None  # None: the process-wide sink

    def sink(self) -> CommsLog:
        return self.log or comms_log()


_settings = _Settings(_default_comms())


def configure_messaging(
    comms: CommsConfig | None, *, log_path: str | Path | bool | None = True
) -> None:
    """Use ``comms`` (``config/comms.yaml``) for routing. ``log_path``: ``True`` takes
    ``comms.metrics.path``; a path overrides it; ``None``/``False`` keeps memory only."""
    global _settings
    comms = comms or _default_comms()
    _settings = _Settings(comms)
    if log_path is True:
        log_path = comms.metrics.path or None
    configure_comms_log(log_path or None)


def _route(pattern: Pattern) -> Route:
    route = _settings.comms.route(pattern.value)
    if route.encoding is not EncodingName.JSON or route.compression != "none":
        raise NotImplementedError(
            f"{pattern.value}: encoding {route.encoding.value} / compression "
            f"{route.compression} arrive with stage C3 (config/comms.yaml)"
        )
    return route


def _priority(pattern: Pattern) -> int:
    spec = _settings.comms.patterns.get(pattern.value)
    return spec.priority_class if spec else 0


def _apply_nl(nl: NL | None, policy: str) -> NL | None:
    """NL policy (A2.5.3): ``full``, ``none`` or ``capped(n)`` (n words per field)."""
    if nl is None or policy == "full":
        return nl
    if policy == "none":
        return None
    n = int(policy[len("capped(") : -1])
    return NL(**{k: " ".join(v.split()[:n]) for k, v in nl.model_dump(exclude_none=True).items()})


def _wrap(
    msg: A6GMessage,
    *,
    sender: str,
    receivers: Sequence[str],
    pattern: Pattern,
    performative: Performative,
    route: Route,
    in_reply_to: str | None = None,
    conversation: str | None = None,
    deadline_s: float | None = None,
    service_ms: float | None = None,
) -> AgentMessage:
    _, conv = _ids(msg.payload)
    now = datetime.now(UTC)
    header = MessageHeader(
        conversation_id=conversation or conv,
        in_reply_to=in_reply_to,
        sender=sender,
        receivers=list(receivers),
        pattern=pattern,
        performative=performative,
        schema=SchemaRef(name=msg.type.value),
        priority_class=_priority(pattern),
        deadline=now + timedelta(seconds=deadline_s) if deadline_s else None,
        sent_at=now,
        service_ms=service_ms,
    )
    return AgentMessage(header=header, payload=msg.payload, nl=_apply_nl(msg.nl, route.nl))


def _unwrap(am: AgentMessage) -> A6GMessage:
    return A6GMessage(MessageType(am.header.schema_ref.name), am.payload, am.nl)


def _sizes(am: AgentMessage) -> tuple[int, int]:
    payload = len(json.dumps(am.payload, separators=(",", ":")).encode())
    nl = len(am.nl.model_dump_json(exclude_none=True).encode()) if am.nl else 0
    return payload, nl


def _record(
    direction: str,
    agent: str,
    am: AgentMessage,
    binding: str,
    *,
    mission_id: str | None = None,
    **fields: Any,
) -> None:
    payload, nl = _sizes(am)
    h = am.header
    _settings.sink().record(
        {
            "ts": time.time(),
            "direction": direction,
            "agent": agent,
            "msg_id": h.msg_id,
            "in_reply_to": h.in_reply_to,
            "conversation_id": h.conversation_id,
            "mission_id": mission_id if mission_id is not None else _ids(am.payload)[0],
            "sender": h.sender,
            "receivers": h.receivers,
            "pattern": h.pattern.value,
            "performative": h.performative.value,
            "schema": h.schema_ref.name,
            "binding": binding,
            "encoding": EncodingName.JSON.value,
            "payload_bytes": payload,
            "nl_bytes": nl,
            **fields,
        }
    )


def _ms(seconds: float | None) -> float | None:
    return None if seconds is None else round(seconds * 1000.0, 3)


# --------------------------------------------------------------------------- requests


class MessagingClient:
    """Requests to one agent (A2A JSON-RPC in the ``hybrid`` preset).

    ``sender`` / ``receiver`` are agent ids for the message headers; ``httpx_client`` can
    carry an in-process transport (``httpx.ASGITransport``) for single-process stacks."""

    def __init__(
        self,
        base_url: str,
        *,
        sender: str = "unknown",
        receiver: str | None = None,
        timeout_s: float = 5.0,
        httpx_client: Any = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.sender = sender
        self.receiver = receiver or self.base_url
        self.timeout_s = timeout_s
        self._conn = _a2a.Connection(self.base_url, timeout_s=timeout_s, httpx_client=httpx_client)
        self._attempts: dict[str, int] = {}

    async def send(self, msg: A6GMessage, *, pattern: Pattern | None = None) -> A6GMessage:
        """Request/response; raises :data:`A2AUnavailable` when the peer cannot be reached
        or could not serve the request (as before Part A2)."""
        pat, perf = classify(msg)
        pattern = pattern or pat
        route = _route(pattern)
        # Request/response over MQTT (correlation ids, reply topics) arrives with stage C3;
        # until then requests use the reference binding and the record says so.
        binding = _a2a.NAME
        started = time.perf_counter()
        am = _wrap(
            msg,
            sender=self.sender,
            receivers=[self.receiver],
            pattern=pattern,
            performative=perf,
            route=route,
            deadline_s=self.timeout_s,
        )
        body = am.to_json()
        serialized = time.perf_counter()
        key = _retry_key(msg)  # the same report / envelope sent again is a retry
        attempt = 1
        if key:
            if len(self._attempts) > 10_000:
                self._attempts.clear()
            attempt = self._attempts[key] = self._attempts.get(key, 0) + 1
        mission = _ids(msg.payload)[0]
        try:
            async with time_flows():  # waiting on another agent: the world does not wait
                reply_am, wire = await self._conn.request(am)
        except _a2a.Unavailable:
            _record(
                "send",
                self.sender,
                am,
                binding,
                wire_bytes=len(body.encode()),
                serialize_ms=_ms(serialized - started),
                e2e_ms=_ms(time.perf_counter() - started),
                attempt=attempt,
                outcome="unavailable",
            )
            raise
        done = time.perf_counter()
        queue = (wire.first_send_at - serialized) if wire.first_send_at else None
        service = (reply_am.header.service_ms or 0.0) / 1000.0
        network = max(0.0, (done - serialized) - (queue or 0.0) - service)
        failed = reply_am.header.schema_ref.name == MessageType.ERROR.value
        _record(
            "send",
            self.sender,
            am,
            binding,
            wire_bytes=wire.request_bytes,
            serialize_ms=_ms(serialized - started),
            queue_ms=_ms(queue),
            network_ms=_ms(network),
            service_ms=reply_am.header.service_ms,
            e2e_ms=_ms(done - started),
            attempt=attempt,
            outcome="failed" if failed else "ok",
        )
        _record(
            "reply",
            self.sender,
            reply_am,
            binding,
            mission_id=mission,
            wire_bytes=wire.response_bytes,
            outcome="failed" if failed else "ok",
        )
        reply = _unwrap(reply_am)
        if reply.type is MessageType.ERROR:
            raise A2AUnavailable(f"{self.base_url}: {reply.payload.get('error')}")
        return reply

    @classmethod
    def in_process(
        cls, app: Any, base_url: str, *, sender: str, receiver: str, timeout_s: float = 5.0
    ) -> "MessagingClient":
        """A client for an agent app served in this process (no sockets), through the same
        binding and encoding as over the network."""
        return cls(
            base_url,
            sender=sender,
            receiver=receiver,
            timeout_s=timeout_s,
            httpx_client=_a2a.asgi_client(app, base_url, timeout_s),
        )

    async def aclose(self) -> None:
        await self._conn.aclose()


Handler = Callable[[A6GMessage], Awaitable[A6GMessage]]


def _server_handler(handler: Handler, agent_id: str) -> Callable[[AgentMessage], Awaitable[Any]]:
    async def serve(am: AgentMessage) -> AgentMessage:
        started = time.perf_counter()
        reply = await handler(_unwrap(am))
        pattern, perf = classify(reply)
        if reply.type is MessageType.ERROR:
            pattern, perf = am.header.pattern, Performative.FAILURE
        elif pattern is not am.header.pattern and am.header.pattern is _P.P6_ESCALATION:
            pattern = am.header.pattern  # the ack of an escalation belongs to P6
        return _wrap(
            reply,
            sender=agent_id,
            receivers=[am.header.sender],
            pattern=pattern,
            performative=perf,
            route=_route(pattern),
            in_reply_to=am.header.msg_id,
            conversation=am.header.conversation_id,
            service_ms=_ms(time.perf_counter() - started),
        )

    return serve


def _error_reply(agent_id: str) -> Callable[[AgentMessage, str], AgentMessage]:
    def build(am: AgentMessage, text: str) -> AgentMessage:
        return _wrap(
            A6GMessage(MessageType.ERROR, {"error": text}),
            sender=agent_id,
            receivers=[am.header.sender],
            pattern=am.header.pattern,
            performative=Performative.FAILURE,
            route=_route(am.header.pattern),
            in_reply_to=am.header.msg_id,
            conversation=am.header.conversation_id,
        )

    return build


def agent_routes(
    card: Any,
    handler: Handler,
    prefix: str = "",
    *,
    agent_id: str,
    handlers: list[Any] | None = None,
) -> list[Any]:
    """Routes serving ``handler`` as agent ``agent_id`` (to mount in another app)."""
    return _a2a.routes(
        card,
        _server_handler(handler, agent_id),
        _error_reply(agent_id),
        prefix,
        handlers=handlers,
    )


def build_agent_app(
    card: Any,
    handler: Handler,
    extra_routes: Sequence[Any] = (),
    *,
    agent_id: str,
    handlers: list[Any] | None = None,
) -> Any:
    """A Starlette app serving ``handler`` as agent ``agent_id``."""
    return _a2a.build_app(
        card,
        _server_handler(handler, agent_id),
        _error_reply(agent_id),
        extra_routes,
        handlers=handlers,
    )


# --------------------------------------------------------------------------- pub/sub


def _as_payload(data: bytes) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return {"_raw": data.decode(errors="replace")}
    return value if isinstance(value, dict) else {"_json": value}


def _as_bytes(payload: dict[str, Any]) -> bytes:
    if set(payload) == {"_raw"}:
        return str(payload["_raw"]).encode()
    if set(payload) == {"_json"}:
        return json.dumps(payload["_json"]).encode()
    return json.dumps(payload).encode()


_TOPIC_SCHEMAS = {
    "state": "robot_state",
    "envelope": "envelope_summary",
    "status": "robot_status",
    "heartbeat": "heartbeat",
    "killswitch": "killswitch",
    "observations": "observation_note",
    "decisions": "decision_note",
}


def _topic_schema(topic: str) -> str:
    """Schema name of a publication (distinct from the request schemas)."""
    leaf = topic.rsplit("/", 1)[-1]
    return _TOPIC_SCHEMAS.get(leaf, f"topic:{leaf}")


def wrap_publication(
    topic: str, payload: bytes | str, *, sender: str
) -> tuple[AgentMessage, bytes]:
    """The canonical message for one publication, and its wire bytes."""
    data = payload.encode() if isinstance(payload, str) else payload
    pattern, perf = topic_pattern(topic)
    msg = A6GMessage(MessageType.STATE, _as_payload(data))  # schema: the topic's payload
    am = _wrap(
        msg,
        sender=sender,
        receivers=[topic],
        pattern=pattern,
        performative=perf,
        route=_route(pattern),
    )
    am.header.schema_ref = SchemaRef(name=_topic_schema(topic))
    return am, am.to_json().encode()


class MessagingBus:
    """Pub/sub for agent ``agent_id`` (MQTT in the ``hybrid`` preset).

    Publishers keep passing raw payloads and subscribers keep receiving them
    (:class:`BusMessage`); on the wire each one travels as an ``AgentMessage``, measured.
    """

    def __init__(self, inner: Any, agent_id: str) -> None:
        self.inner = inner
        self.agent_id = agent_id

    async def __aenter__(self) -> "MessagingBus":
        if hasattr(self.inner, "__aenter__"):
            await self.inner.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if hasattr(self.inner, "__aexit__"):
            await self.inner.__aexit__(*exc)

    def as_agent(self, agent_id: str) -> "MessagingBus":
        """The same bus, publishing as another agent (in-process stacks share one bus)."""
        return MessagingBus(self.inner, agent_id)

    async def publish(
        self, topic: str, payload: bytes | str, qos: int = 0, retain: bool = False
    ) -> None:
        started = time.perf_counter()
        am, wire = wrap_publication(topic, payload, sender=self.agent_id)
        serialized = time.perf_counter()
        outcome = "ok"
        try:
            await self.inner.publish(topic, wire, qos=qos, retain=retain)
        except Exception:
            outcome = "failed"
            raise
        finally:
            _record(
                "send",
                self.agent_id,
                am,
                _mqtt_name(),
                wire_bytes=_mqtt.publish_wire_bytes(topic, len(wire), qos),
                serialize_ms=_ms(serialized - started),
                queue_ms=_ms(time.perf_counter() - serialized),
                attempt=1,
                outcome=outcome,
            )

    def _open(self, msg: BusMessage, record: bool) -> BusMessage:
        try:
            am = AgentMessage.from_json(msg.payload)
        except ValueError:
            return msg  # not ours (e.g. retained by an older version): pass through
        if record:
            age = (datetime.now(UTC) - am.header.sent_at).total_seconds()
            _record(
                "recv",
                self.agent_id,
                am,
                _mqtt_name(),
                wire_bytes=_mqtt.publish_wire_bytes(msg.topic, len(msg.payload)),
                e2e_ms=None if msg.retain else _ms(max(0.0, age)),
                outcome="retained" if msg.retain else "ok",
            )
        return replace(msg, payload=_as_bytes(am.payload), message=am)

    async def subscribe(self, topic_filter: str) -> AsyncIterator[BusMessage]:
        async for msg in self.inner.subscribe(topic_filter):
            yield self._open(msg, record=True)

    # In-memory introspection (tests and in-process stacks), unwrapped like subscriptions.
    def messages(self, topic_filter: str) -> list[BusMessage]:
        return [self._open(m, record=False) for m in self.inner.messages(topic_filter)]

    @property
    def retained(self) -> dict[str, BusMessage]:
        return {t: self._open(m, record=False) for t, m in self.inner.retained.items()}

    @property
    def history(self) -> list[BusMessage]:
        return [self._open(m, record=False) for m in self.inner.history]


def _mqtt_name() -> str:
    return BindingName.MQTT.value


def memory_bus(agent_id: str = "devstack") -> MessagingBus:
    """A broker-less bus with MQTT semantics (tests, in-process stacks)."""
    return MessagingBus(_mqtt.InMemoryBus(), agent_id)


def mqtt_bus(
    host: str,
    port: int = 1883,
    *,
    agent_id: str,
    client_id: str | None = None,
    keepalive: int = 30,
    will: tuple[str, bytes | str, int, bool] | None = None,
) -> MessagingBus:
    """An MQTT bus (``async with``); the last-will travels as an ``AgentMessage`` too."""
    raw_will = None
    if will is not None:
        topic, payload, qos, retain = will
        raw_will = (topic, wrap_publication(topic, payload, sender=agent_id)[1], qos, retain)
    return MessagingBus(
        _mqtt.MqttBus(host, port, client_id=client_id, keepalive=keepalive, will=raw_will),
        agent_id,
    )
