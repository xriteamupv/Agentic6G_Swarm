"""The coordinator's heartbeat survives a broker restart (2026-10-26: after restarting the
2D stack, whose embedded broker the coordinator used, robots in coverage reported the
coordinator unreachable)."""

import asyncio
import socket
import time

import aiomqtt
from a6g_coordinator import gateway
from a6g_transport.bindings.mqtt import start_embedded_broker
from a6g_transport.messaging import TOPIC_HEARTBEAT


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _fresh_heartbeat(port: int, timeout_s: float = 8.0) -> bool:
    """A heartbeat published after we subscribed (not the retained one)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            async with aiomqtt.Client("127.0.0.1", port) as c:
                await c.subscribe(TOPIC_HEARTBEAT)
                async with asyncio.timeout(3.0):
                    async for m in c.messages:
                        if not m.retain and b"online" in bytes(m.payload):
                            return True
        except (aiomqtt.MqttError, TimeoutError, OSError):
            await asyncio.sleep(0.3)
    return False


async def test_the_heartbeat_comes_back_after_a_broker_restart(monkeypatch) -> None:
    port = _free_port()
    monkeypatch.setattr(gateway.CFG.fleet.edge.mqtt, "host", "127.0.0.1")
    monkeypatch.setattr(gateway.CFG.fleet.edge.mqtt, "port", port)
    stop = asyncio.Event()
    # the coordinator starts first: no broker yet
    loop = asyncio.create_task(gateway._telemetry_loop(stop, retry_s=0.3))
    await asyncio.sleep(0.5)
    broker = await start_embedded_broker("127.0.0.1", port)
    try:
        assert await _fresh_heartbeat(port)
        await broker.shutdown()  # terminal 1 restarted
        broker = await start_embedded_broker("127.0.0.1", port)
        assert await _fresh_heartbeat(port)
    finally:
        stop.set()
        await asyncio.wait_for(loop, 5.0)
        await broker.shutdown()
