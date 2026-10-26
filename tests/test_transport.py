"""Bandwidth budget, telemetry bus and A2A encoding."""

import logging

import pytest
from a6g_common.messages import AgentMessage, MessageHeader, Pattern, Performative, SchemaRef
from a6g_common.schemas import TrafficClass
from a6g_transport.bindings.a2a_jsonrpc import decode, encode
from a6g_transport.bindings.mqtt import InMemoryBus
from a6g_transport.budget import Admission, UplinkBudget
from a6g_transport.messaging import A6GMessage, MessageType


def _agent_message(payload: dict, pattern=Pattern.P3_CONTRACT_NET) -> AgentMessage:
    header = MessageHeader(
        sender="t", pattern=pattern, performative=Performative.CFP, schema=SchemaRef(name="cfp")
    )
    return AgentMessage(header=header, payload=payload)


class FakeTime:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_token_bucket_rate_and_burst() -> None:
    now = FakeTime()
    b = UplinkBudget("go2_01", sustained_mbps=3.0, burst_mbps=8.0, monotonic=now)
    assert b.capacity_B == 1_000_000 and b.rate_Bps == 375_000
    assert b.admit(TrafficClass.CONTROL_MSGS, 900_000) is Admission.SEND
    assert b.admit(TrafficClass.CONTROL_MSGS, 200_000) is Admission.QUEUE  # control queues
    now.t = 1.0  # refills at the sustained rate
    assert b.tokens == pytest.approx(475_000)
    assert b.admit(TrafficClass.CONTROL_MSGS, 200_000) is Admission.SEND


def test_lower_classes_back_off_first() -> None:
    now = FakeTime()
    b = UplinkBudget("go2_01", 3.0, 8.0, monotonic=now)
    b.admit(TrafficClass.CONTROL_MSGS, 400_000)  # 600 kB left
    # bulk may only spend above its 70 % reserve -> refused; event images above 30 % -> ok
    assert b.admit(TrafficClass.BULK, 10_000) is Admission.DROP
    assert b.admit(TrafficClass.EVENT_IMAGE, 250_000) is Admission.SEND  # 350 kB left
    assert b.admit(TrafficClass.EVENT_IMAGE, 100_000) is Admission.QUEUE  # would go below 300 kB
    assert b.admit(TrafficClass.CONTROL_MSGS, 300_000) is Admission.SEND  # control uses it all


def test_every_drop_is_logged(caplog) -> None:
    b = UplinkBudget("go2_01", 3.0, 8.0, monotonic=FakeTime())
    with caplog.at_level(logging.WARNING):
        assert b.admit(TrafficClass.EVENT_IMAGE, 2_000_000) is Admission.DROP  # > burst
        b.admit(TrafficClass.CONTROL_MSGS, 1_000_000)
        assert b.admit(TrafficClass.VOICE, 1_000) is Admission.DROP
    assert b.dropped[TrafficClass.EVENT_IMAGE] == 1 and b.dropped[TrafficClass.VOICE] == 1
    drops = [r for r in caplog.records if "DROP" in r.getMessage()]
    assert len(drops) == 2


def test_budget_rejects_bad_config() -> None:
    with pytest.raises(ValueError):
        UplinkBudget("x", sustained_mbps=8.0, burst_mbps=3.0)


async def test_in_memory_bus_wildcards_and_retained() -> None:
    bus = InMemoryBus()
    await bus.publish("a6g/killswitch", b'{"state":"on"}', qos=1, retain=True)
    await bus.publish("a6g/fleet/go2_01/state", "{}")
    sub = bus.subscribe("a6g/#")
    first = await anext(sub)
    assert first.topic == "a6g/killswitch" and first.retain  # retained delivered on subscribe
    await bus.publish("a6g/fleet/summit_01/state", "{}")
    assert (await anext(sub)).topic == "a6g/fleet/summit_01/state"
    assert [m.topic for m in bus.messages("a6g/fleet/+/state")] == [
        "a6g/fleet/go2_01/state",
        "a6g/fleet/summit_01/state",
    ]
    await sub.aclose()


def test_a2a_payload_is_lossless() -> None:
    payload = {"deadline_ms": 1500, "cost": 12.5, "nested": {"ids": [1, 2]}, "none": None}
    msg = encode(_agent_message(payload))
    back = decode(msg.parts)
    assert back.header.schema_ref.name == "cfp" and back.payload == payload
    assert isinstance(back.payload["deadline_ms"], int)


async def test_unreachable_robot_is_a_warning_not_a_traceback(caplog) -> None:
    """A robot in a 5G shadow cannot serve requests: expected, so one warning line."""
    import logging
    from types import SimpleNamespace

    from a6g_transport.bindings.a2a_jsonrpc import _Executor

    sent = []

    class Queue:
        async def enqueue_event(self, message):
            sent.append(message)

    async def shadowed(_msg):
        raise ConnectionError("go2_01 is out of 5G coverage")

    async def broken(_msg):
        raise RuntimeError("bug")

    request = encode(_agent_message({}, Pattern.P4_STATE))
    context = SimpleNamespace(message=request, context_id="c1")

    def error_reply(am, _text):
        return am

    with caplog.at_level(logging.WARNING, logger="a6g_transport.bindings.a2a_jsonrpc"):
        await _Executor(shadowed, error_reply).execute(context, Queue())
        await _Executor(broken, error_reply).execute(context, Queue())
    shadow, bug = caplog.records
    assert shadow.levelno == logging.WARNING and shadow.exc_info is None
    assert "out of 5G coverage" in shadow.getMessage()
    assert bug.levelno == logging.ERROR and bug.exc_info is not None  # real bugs keep it
    assert len(sent) == 2  # both still answer the caller with an error message


async def test_a2a_requests_do_not_accumulate(caplog) -> None:
    """a2a-sdk keeps a message-mode request's ActiveTask (queues + dispatcher task) for the
    life of the server; we release it after the reply. Before: one leaked set per robot
    report and one "Dispatcher task is not running" warning each at Ctrl-C."""
    import asyncio
    import logging
    import socket

    import uvicorn
    from a6g_transport.messaging import (
        MessagingClient,
        agent_routes,
        build_agent_card,
    )
    from starlette.applications import Starlette

    async def handle(_msg):
        return A6GMessage(MessageType.REPORT_ACK, {"ok": True})

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    url = f"http://127.0.0.1:{port}"
    card = build_agent_card(name="t", description="t", base_url=url, skills=[("x", "x", "x")])
    handlers: list = []
    app = Starlette(routes=agent_routes(card, handle, agent_id="t", handlers=handlers))  # no hook
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    registry = handlers[0]._active_task_registry
    client = MessagingClient(url, timeout_s=5)
    with caplog.at_level(logging.WARNING, logger="a2a"):
        for _ in range(20):
            await client.send(A6GMessage(MessageType.STATE, {}))
        await asyncio.sleep(0.3)
        assert len(registry._active_tasks) == 0
        server.should_exit = True
        await task
    assert not [r for r in caplog.records if "Dispatcher task is not running" in r.getMessage()]


async def test_concurrent_requests_to_one_agent_all_get_their_reply() -> None:
    """Each request releases only its own SDK task. A list shared by the handler let one
    request release another's task while that one was still waiting: it hung for good (the
    coordinator-restart scenario stalled once in three runs)."""
    import asyncio
    import random

    from a6g_transport.messaging import (
        A6GMessage,
        MessageType,
        MessagingClient,
        build_agent_app,
        build_agent_card,
        close_handlers,
    )

    async def handle(msg):
        await asyncio.sleep(random.uniform(0, 0.02))  # replies finish out of order
        return A6GMessage(MessageType.REPORT_ACK, {"n": msg.payload["n"]})

    url = "http://coordinator.inproc"
    card = build_agent_card(name="t", description="t", base_url=url, skills=[("x", "x", "x")])
    handlers: list = []
    app = build_agent_app(card, handle, agent_id="coordinator", handlers=handlers)
    clients = [
        MessagingClient.in_process(app, url, sender=f"r{i}", receiver="coordinator", timeout_s=10)
        for i in range(5)
    ]
    sends = [
        clients[n % 5].send(A6GMessage(MessageType.REPORT, {"n": n, "report_id": f"rep{n}"}))
        for n in range(100)
    ]
    replies = await asyncio.wait_for(asyncio.gather(*sends), 30)
    assert sorted(r.payload["n"] for r in replies) == list(range(100))
    assert not await handlers[0]._active_task_registry.get("any")  # nothing left open
    for c in clients:
        await c.aclose()
    await close_handlers(handlers)
