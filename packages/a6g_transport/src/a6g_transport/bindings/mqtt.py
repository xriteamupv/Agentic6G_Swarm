"""MQTT binding (SPEC A2.5.3, 11): an in-memory bus with MQTT semantics (tests, in-process
stacks), an aiomqtt bus, the embedded broker and MQTT wire-size accounting.

The buses move raw bytes; :mod:`a6g_transport.messaging` wraps each payload in an
``AgentMessage`` and measures it. Topic names live in the messaging layer."""

import asyncio
import contextlib
import json
import socket
from collections import defaultdict
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

from paho.mqtt.client import topic_matches_sub


@dataclass(frozen=True)
class BusMessage:
    topic: str
    payload: bytes
    retain: bool = False
    #: The canonical message it arrived in (set by the messaging layer), if any.
    message: Any = None

    def json(self) -> Any:
        return json.loads(self.payload)


class TelemetryBus(Protocol):
    async def publish(
        self, topic: str, payload: bytes | str, qos: int = 0, retain: bool = False
    ) -> None: ...

    def subscribe(self, topic_filter: str) -> AsyncIterator[BusMessage]: ...


class InMemoryBus:
    """Broker-less bus with MQTT wildcard and retained-message semantics (tests, sim)."""

    def __init__(self) -> None:
        self.retained: dict[str, BusMessage] = {}
        self.history: list[BusMessage] = []
        self._subscribers: dict[str, list[asyncio.Queue[BusMessage]]] = defaultdict(list)

    async def publish(
        self, topic: str, payload: bytes | str, qos: int = 0, retain: bool = False
    ) -> None:
        data = payload.encode() if isinstance(payload, str) else payload
        msg = BusMessage(topic, data, retain)
        self.history.append(msg)
        if retain:
            self.retained[topic] = msg
        # As a broker: live deliveries carry retain=False; only the copy replayed to a new
        # subscriber is flagged retained (MQTT 3.1.1 3.3.1.3).
        live = BusMessage(topic, data, False) if retain else msg
        for flt, queues in list(self._subscribers.items()):
            if topic_matches_sub(flt, topic):
                for q in queues:
                    q.put_nowait(live)

    async def subscribe(self, topic_filter: str) -> AsyncIterator[BusMessage]:
        queue: asyncio.Queue[BusMessage] = asyncio.Queue()
        self._subscribers[topic_filter].append(queue)
        for topic, msg in self.retained.items():
            if topic_matches_sub(topic_filter, topic):
                queue.put_nowait(msg)
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers[topic_filter].remove(queue)

    def messages(self, topic_filter: str) -> list[BusMessage]:
        return [m for m in self.history if topic_matches_sub(topic_filter, m.topic)]


class MqttBus:
    """aiomqtt-backed bus. Use as ``async with MqttBus(...) as bus``.

    One persistent connection publishes (and carries the last-will); each subscription
    opens its own connection so consumers are independent.
    """

    def __init__(
        self,
        host: str,
        port: int = 1883,
        *,
        client_id: str | None = None,
        keepalive: int = 30,
        will: tuple[str, bytes, int, bool] | None = None,
    ) -> None:
        self.host, self.port, self.client_id, self.keepalive = host, port, client_id, keepalive
        self.will = will
        self._pub = None

    def _client(self, identifier: str | None, with_will: bool):
        import aiomqtt

        will = None
        if with_will and self.will is not None:
            topic, payload, qos, retain = self.will
            will = aiomqtt.Will(topic, payload, qos=qos, retain=retain)
        return aiomqtt.Client(
            self.host,
            self.port,
            identifier=identifier,
            keepalive=self.keepalive,
            will=will,
            # paho leaves Nagle on: a small message then waits for the peer's delayed ACK
            # (~40 ms each way; found by the protocol micro-benchmark, SPEC A4.8)
            socket_options=[(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)],
        )

    async def __aenter__(self) -> "MqttBus":
        self._pub = self._client(self.client_id, with_will=True)
        await self._pub.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._pub is not None:
            await self._pub.__aexit__(*exc)
            self._pub = None

    async def publish(
        self, topic: str, payload: bytes | str, qos: int = 0, retain: bool = False
    ) -> None:
        if self._pub is None:
            raise RuntimeError("MqttBus not connected; use `async with MqttBus(...)`")
        await self._pub.publish(topic, payload, qos=qos, retain=retain)

    async def subscribe(self, topic_filter: str) -> AsyncIterator[BusMessage]:
        async with self._client(None, with_will=False) as client:
            await client.subscribe(topic_filter, qos=1)
            async for message in client.messages:
                if not topic_matches_sub(topic_filter, str(message.topic)):
                    continue  # some brokers replay unrelated retained messages
                payload = message.payload
                if isinstance(payload, str):
                    payload = payload.encode()
                yield BusMessage(str(message.topic), bytes(payload or b""), bool(message.retain))


async def consume(bus: TelemetryBus, topic_filter: str, handler) -> None:
    """Run ``handler(msg)`` for every message; for use in a background task."""
    with contextlib.suppress(asyncio.CancelledError):
        async for msg in bus.subscribe(topic_filter):
            await handler(msg)


def publish_wire_bytes(topic: str, payload_len: int, qos: int = 0) -> int:
    """Size of the MQTT 3.1.1 PUBLISH packet: fixed header (1 byte + remaining length),
    topic (2-byte length + UTF-8), packet id for QoS > 0, payload. TCP/IP not included."""
    remaining = 2 + len(topic.encode()) + (2 if qos > 0 else 0) + payload_len
    length_bytes = 1
    while remaining >= 128**length_bytes and length_bytes < 4:
        length_bytes += 1
    return 1 + length_bytes + remaining


async def start_embedded_broker(host: str, port: int):
    """In-process MQTT broker (amqtt) for development stacks; ``await broker.shutdown()``."""
    from amqtt.broker import Broker

    broker = Broker(
        {
            "listeners": {"default": {"type": "tcp", "bind": f"{host}:{port}"}},
            "plugins": {
                "amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True}
            },
        }
    )
    await broker.start()
    return broker
