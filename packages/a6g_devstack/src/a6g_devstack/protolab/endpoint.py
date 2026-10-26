"""Protocol micro-benchmark endpoints (SPEC A4.8, stage N5): one server or one client per
process, run inside a network namespace of the lab (``lab.py``), on real sockets, TLS 1.3.

The messages are the agents' canonical ``AgentMessage`` (the same wrapping as the
messaging layer); payload sizes are sampled from recorded G10 message logs. Bindings:

* ``a2a_jsonrpc`` (warm: one pooled connection and a cached agent card; cold: a new client
  and connection per message, the agent card fetched each time), ``a2a_rest``
  (HTTP+JSON), ``a2a_grpc``: the official ``a2a-sdk``;
* ``mqtt``: a persistent session (clean session false), QoS 1, request/response with
  correlation ids and reply topics through the edge broker; encodings ``json`` and ``cbor``.

    python -m a6g_devstack.protolab.endpoint serve '<json spec>'
    python -m a6g_devstack.protolab.endpoint run '<json spec>'

A server logs each request it receives (``recv.jsonl``: message id, time) for duplicate and
late-delivery counts; a client writes one JSON line per message or round (``out.jsonl``).
"""

import asyncio
import base64
import contextlib
import json
import random
import socket
import ssl
import sys
import time
from pathlib import Path
from typing import Any

from a6g_common.messages import NL, AgentMessage, MessageHeader, Pattern, Performative, SchemaRef

REQUEST = {
    "P1": ("envelope", Pattern.P1_ENVELOPE, Performative.REQUEST, "envelope_reply"),
    "P2": ("report", Pattern.P2_REPORT, Performative.INFORM, "report_ack"),
    "P3_cfp": ("cfp", Pattern.P3_CONTRACT_NET, Performative.CFP, "bid"),
    "P3_award": ("award", Pattern.P3_CONTRACT_NET, Performative.AWARD, "award_ack"),
    "P4": ("robot_state", Pattern.P4_STATE, Performative.INFORM, None),
    "P6": ("escalation", Pattern.P6_ESCALATION, Performative.REQUEST, "escalation_ack"),
    "P7": ("report", Pattern.P7_HUMAN, Performative.REQUEST, "operator_ack"),
}


def _now() -> float:
    return time.time()


# --------------------------------------------------------------------------- messages


def message(
    kind: str,
    sender: str,
    receivers: list[str],
    payload_bytes: int,
    nl_bytes: int = 0,
    attachment: bytes | None = None,
    in_reply_to: str | None = None,
    performative: Performative | None = None,
    schema: str | None = None,
) -> AgentMessage:
    """An AgentMessage whose JSON payload is ``payload_bytes`` long (padded), with
    ``nl_bytes`` of model text (nl policy ``full``) and an optional attachment."""
    name, pattern, perf, _ = REQUEST[kind]
    payload: dict[str, Any] = {"type": schema or name, "pad": ""}
    base = len(json.dumps(payload, separators=(",", ":")).encode())
    payload["pad"] = "x" * max(0, payload_bytes - base)
    if attachment is not None:
        payload["evidence_jpeg_b64"] = base64.b64encode(attachment).decode()
    nl = NL(summary="s" * max(0, nl_bytes - 15)) if nl_bytes > 0 else None
    return AgentMessage(
        header=MessageHeader(
            sender=sender,
            receivers=receivers,
            pattern=pattern,
            performative=performative or perf,
            schema=SchemaRef(name=schema or name),
            in_reply_to=in_reply_to,
        ),
        payload=payload,
        nl=nl,
    )


def encode(msg: AgentMessage, encoding: str) -> bytes:
    if encoding == "cbor":
        import cbor2

        data = msg.model_dump(mode="json", by_alias=True, exclude_none=True)
        att = data["payload"].pop("evidence_jpeg_b64", None)
        if att is not None:  # CBOR carries the image as bytes, not base64 text
            data["payload"]["evidence_jpeg"] = base64.b64decode(att)
        return cbor2.dumps(data)
    return msg.to_json().encode()


def decode(raw: bytes, encoding: str) -> AgentMessage:
    if encoding == "cbor":
        import cbor2

        data = cbor2.loads(raw)
        att = data["payload"].pop("evidence_jpeg", None)
        if att is not None:
            data["payload"]["evidence_jpeg_b64"] = base64.b64encode(att).decode()
        return AgentMessage.model_validate(data)
    return AgentMessage.from_json(raw)


def reply_to(req: AgentMessage, sender: str, size: int) -> AgentMessage:
    kind = next(
        (
            k
            for k, v in REQUEST.items()
            if v[0] == req.header.schema_ref.name and v[1] == req.header.pattern
        ),
        "P2",
    )
    reply_schema = REQUEST[kind][3] or "ack"
    return message(
        kind,
        sender,
        [req.header.sender],
        size,
        in_reply_to=req.header.msg_id,
        performative=Performative.BID if reply_schema == "bid" else Performative.ACK,
        schema=reply_schema,
    )


# --------------------------------------------------------------------------- TLS


def server_tls(certs: Path) -> ssl.SSLContext:
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_cert_chain(certs / "server.crt", certs / "server.key")
    return ctx


def client_tls(certs: Path) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=str(certs / "ca.crt"))
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    return ctx


# --------------------------------------------------------------------------- servers


class Receipts:
    def __init__(self, path: Path) -> None:
        self.path = path

    def note(self, msg: AgentMessage) -> None:
        with self.path.open("a") as fh:
            fh.write(
                json.dumps(
                    {"msg_id": msg.header.msg_id, "t": _now(), "schema": msg.header.schema_ref.name}
                )
                + "\n"
            )


async def serve_a2a(spec: dict[str, Any]) -> None:
    import grpc
    import uvicorn
    from a2a.server.request_handlers import GrpcHandler
    from a2a.server.routes import (
        create_agent_card_routes,
        create_jsonrpc_routes,
        create_rest_routes,
    )
    from a2a.server.tasks import InMemoryTaskStore
    from a2a.types import AgentCapabilities, AgentCard, AgentInterface, a2a_pb2_grpc
    from a6g_transport.bindings.a2a_jsonrpc import _Executor, _ReleasingHandler
    from starlette.applications import Starlette

    certs = Path(spec["certs"])
    host, port, me = spec["host"], int(spec["port"]), spec["agent"]
    receipts = Receipts(Path(spec["recv"]))
    reply_size = int(spec.get("reply_bytes", 120))

    async def handler(req: AgentMessage) -> AgentMessage:
        receipts.note(req)
        return reply_to(req, me, reply_size)

    binding = {"a2a_jsonrpc": "JSONRPC", "a2a_rest": "HTTP+JSON", "a2a_grpc": "GRPC"}[
        spec["binding"]
    ]
    url = f"https://{host}:{port}" if binding != "GRPC" else f"{host}:{port + 1}"
    card = AgentCard(
        name=f"a6g-bench-{me}",
        description="protocol micro-benchmark endpoint",
        version="1",
        supported_interfaces=[
            AgentInterface(url=url, protocol_binding=binding, protocol_version="1.0")
        ],
        capabilities=AgentCapabilities(streaming=False),
        default_input_modes=["application/vnd.a6g+json"],
        default_output_modes=["application/vnd.a6g+json"],
    )
    rh = _ReleasingHandler(_Executor(handler, lambda m, e: m), InMemoryTaskStore(), card)
    routes = list(create_agent_card_routes(card))
    if binding == "JSONRPC":
        routes += list(create_jsonrpc_routes(rh, "/"))
    elif binding == "HTTP+JSON":
        routes += list(create_rest_routes(rh))
    else:  # gRPC on port + 1 (TLS); the agent card over HTTPS on port
        server = grpc.aio.server()
        a2a_pb2_grpc.add_A2AServiceServicer_to_server(GrpcHandler(rh), server)
        creds = grpc.ssl_server_credentials(
            [((certs / "server.key").read_bytes(), (certs / "server.crt").read_bytes())]
        )
        server.add_secure_port(f"{host}:{port + 1}", creds)
        await server.start()
    app = Starlette(routes=routes)
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="error",
        ssl_keyfile=str(certs / "server.key"),
        ssl_certfile=str(certs / "server.crt"),
        timeout_keep_alive=600,
    )
    Path(spec["ready"]).write_text("ok")
    await uvicorn.Server(config).serve()


async def serve_mqtt_broker(spec: dict[str, Any]) -> None:
    from amqtt.broker import Broker

    certs = Path(spec["certs"])
    broker = Broker(
        {
            "listeners": {
                "default": {
                    "type": "tcp",
                    "bind": f"{spec['host']}:{spec['port']}",
                    "ssl": True,
                    "certfile": str(certs / "server.crt"),
                    "keyfile": str(certs / "server.key"),
                }
            },
            "plugins": {
                "amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True}
            },
        }
    )
    await broker.start()
    Path(spec["ready"]).write_text("ok")
    await asyncio.Event().wait()


def _mqtt_client(spec: dict[str, Any], ident: str):
    import aiomqtt

    return aiomqtt.Client(
        spec["broker"],
        int(spec["broker_port"]),
        identifier=ident,
        clean_session=False,  # persistent session
        tls_context=client_tls(Path(spec["certs"])),
        # paho leaves Nagle on: small requests then wait for delayed ACKs (~40 ms each
        # way, measured 84 ms on localhost); every other binding's stack sets NODELAY
        socket_options=[(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)],
        keepalive=10,
        timeout=10,
    )


async def serve_mqtt(spec: dict[str, Any]) -> None:
    """A responder on ``a6g/bench/req/<agent>``: replies to each request's reply topic."""
    import aiomqtt

    me, enc = spec["agent"], spec["encoding"]
    receipts = Receipts(Path(spec["recv"]))
    reply_size = int(spec.get("reply_bytes", 120))
    ready = False
    while True:
        try:
            async with _mqtt_client(spec, f"srv-{me}") as client:
                await client.subscribe(f"a6g/bench/req/{me}", qos=1)
                await client.subscribe(f"a6g/bench/state/{me}", qos=1)
                if not ready:
                    Path(spec["ready"]).write_text("ok")
                    ready = True
                async for m in client.messages:
                    box = json.loads(m.payload[:200].split(b"\n", 1)[0])
                    req = decode(m.payload.split(b"\n", 1)[1], enc)
                    receipts.note(req)
                    if box.get("reply_to"):
                        rep = reply_to(req, me, reply_size)
                        body = json.dumps({"corr": box["corr"]}).encode() + b"\n" + encode(rep, enc)
                        await client.publish(box["reply_to"], body, qos=1)
        except aiomqtt.MqttError:
            await asyncio.sleep(0.5)


# --------------------------------------------------------------------------- clients


class A2AClient:
    """warm: one httpx client and A2A client, reused; cold: new ones per request."""

    def __init__(self, spec: dict[str, Any], target: str) -> None:
        self.spec, self.target = spec, target
        self.binding = {"a2a_jsonrpc": "JSONRPC", "a2a_rest": "HTTP+JSON", "a2a_grpc": "GRPC"}[
            spec["binding"]
        ]
        self.cold = spec.get("mode") == "cold"
        self._http = None
        self._client = None

    async def _make(self):
        import grpc
        import httpx
        from a2a.client import ClientConfig, ClientFactory

        certs = Path(self.spec["certs"])
        http = httpx.AsyncClient(
            verify=client_tls(certs), timeout=float(self.spec.get("timeout_s", 10))
        )
        creds = grpc.ssl_channel_credentials((certs / "ca.crt").read_bytes())
        factory = ClientFactory(
            ClientConfig(
                httpx_client=http,
                streaming=False,
                supported_protocol_bindings=[self.binding],
                grpc_channel_factory=lambda u: grpc.aio.secure_channel(u, creds),
            )
        )
        client = await factory.create_from_url(f"https://{self.target}")
        return http, client

    async def request(self, msg: AgentMessage) -> AgentMessage:
        from a2a.types import Role, SendMessageRequest
        from a6g_transport.bindings.a2a_jsonrpc import decode as a2a_decode
        from a6g_transport.bindings.a2a_jsonrpc import encode as a2a_encode

        if self.cold or self._client is None:
            await self.close()
            self._http, self._client = await self._make()
        try:
            req = SendMessageRequest(message=a2a_encode(msg, role=Role.ROLE_USER))
            async for ev in self._client.send_message(req):
                if ev.HasField("message"):
                    return a2a_decode(ev.message.parts)
            raise ConnectionError("no reply")
        except Exception:
            await self.close()  # the next request reconnects
            raise
        finally:
            if self.cold:
                await self.close()

    async def close(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.close()
        if self._http is not None:
            with contextlib.suppress(Exception):
                await self._http.aclose()
        self._http = self._client = None


class MqttRequester:
    """Request/response over the broker: requests to ``a6g/bench/req/<peer>``, replies on
    this client's own reply topic, matched by correlation id."""

    def __init__(self, spec: dict[str, Any]) -> None:
        self.spec, self.me, self.enc = spec, spec["agent"], spec["encoding"]
        self.pending: dict[str, asyncio.Future] = {}
        self.client = None
        self._task = None
        self._connected = asyncio.Event()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())
        await asyncio.wait_for(self._connected.wait(), 30)

    async def _loop(self) -> None:
        import aiomqtt

        while True:
            try:
                async with _mqtt_client(self.spec, f"cli-{self.me}") as client:
                    self.client = client
                    await client.subscribe(f"a6g/bench/rep/{self.me}", qos=1)
                    self._connected.set()
                    async for m in client.messages:
                        head, _, body = m.payload.partition(b"\n")
                        corr = json.loads(head).get("corr")
                        fut = self.pending.pop(corr, None)
                        if fut is not None and not fut.done():
                            fut.set_result(decode(body, self.enc))
            except aiomqtt.MqttError:
                self.client = None
                self._connected.clear()
                await asyncio.sleep(0.5)

    async def request(self, peer: str, msg: AgentMessage) -> AgentMessage:
        # one correlation id per request: a CFP goes to several peers as the same message
        corr = f"{msg.header.msg_id}:{peer}"
        fut = asyncio.get_running_loop().create_future()
        self.pending[corr] = fut
        box = json.dumps({"corr": corr, "reply_to": f"a6g/bench/rep/{self.me}"}).encode()
        await self._connected.wait()
        await self.client.publish(
            f"a6g/bench/req/{peer}", box + b"\n" + encode(msg, self.enc), qos=1
        )
        return await fut

    async def publish(self, topic: str, msg: AgentMessage) -> None:
        await self._connected.wait()
        box = json.dumps({"corr": msg.header.msg_id}).encode()
        await self.client.publish(topic, box + b"\n" + encode(msg, self.enc), qos=1)

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task


def _sizes(spec: dict[str, Any], key: str) -> list[int]:
    sizes = json.loads(Path(spec["payloads"]).read_text())
    return sizes.get(key) or [500]


async def run_client(spec: dict[str, Any]) -> None:
    """Run one block; write one line per message (or round) to ``out``."""
    rng = random.Random(spec.get("seed", 0))
    out = Path(spec["out"])
    pattern, count, me = spec["pattern"], int(spec.get("count", 1)), spec["agent"]
    peers: list[str] = spec["peers"]  # agent ids
    targets: dict[str, str] = spec.get("targets") or {}  # agent id -> host:port (A2A)
    nl_policy = spec.get("nl", "full")
    mqtt = spec["binding"] == "mqtt"
    requester = MqttRequester(spec) if mqtt else None
    if requester is not None:
        await requester.start()
    a2a = {p: A2AClient(spec, targets[p]) for p in peers} if not mqtt else {}
    sizes = _sizes(spec, pattern if pattern != "P3" else "P3_cfp")
    nl_sizes = _sizes(spec, f"{pattern}_nl") if nl_policy == "full" else [0]
    deadline_s = float(spec.get("deadline_s", 2.0))
    jpeg = random.Random(7).randbytes(300_000) if pattern == "P6" else None

    async def ask(peer: str, msg: AgentMessage) -> AgentMessage:
        if mqtt:
            return await asyncio.wait_for(
                requester.request(peer, msg), float(spec.get("timeout_s", 10))
            )
        return await asyncio.wait_for(a2a[peer].request(msg), float(spec.get("timeout_s", 10)))

    lines = []
    try:
        if pattern == "P4":  # robot state at 1 Hz for `duration_s`
            end = time.monotonic() + float(spec.get("duration_s", 300))
            i = 0
            while time.monotonic() < end:
                msg = message("P4", me, peers, rng.choice(sizes))
                t0 = _now()
                try:
                    if mqtt:
                        await requester.publish(f"a6g/bench/state/{peers[0]}", msg)
                    else:  # A2A has no publish/subscribe: a request per update
                        await ask(peers[0], msg)
                    lines.append(
                        {"i": i, "id": msg.header.msg_id, "t0": t0, "t1": _now(), "ok": True}
                    )
                except Exception as exc:
                    lines.append(
                        {
                            "i": i,
                            "id": msg.header.msg_id,
                            "t0": t0,
                            "ok": False,
                            "err": type(exc).__name__,
                        }
                    )
                i += 1
                await asyncio.sleep(max(0.0, 1.0 - (_now() - t0)))
        elif pattern == "P3":  # CFP to N-1 peers, bids, award + ack
            for i in range(count):
                t0 = _now()
                cfp = message("P3_cfp", me, peers, rng.choice(sizes))
                try:
                    bids = await asyncio.wait_for(
                        asyncio.gather(*(ask(p, cfp) for p in peers), return_exceptions=True),
                        float(spec.get("cfp_deadline_s", 1.5)),
                    )
                    got = [
                        p for p, b in zip(peers, bids, strict=True) if isinstance(b, AgentMessage)
                    ]
                    award = message("P3_award", me, got[:1], 220)
                    if got:
                        await ask(got[0], award)
                    t1 = _now()
                    lines.append(
                        {
                            "i": i,
                            "t0": t0,
                            "t1": t1,
                            "ok": bool(got),
                            "bids": len(got),
                            "miss": (t1 - t0) >= float(spec.get("cfp_deadline_s", 1.5)),
                        }
                    )
                except Exception as exc:
                    lines.append(
                        {
                            "i": i,
                            "t0": t0,
                            "t1": _now(),
                            "ok": False,
                            "miss": True,
                            "err": type(exc).__name__,
                        }
                    )
        elif pattern == "outage":  # P2 every `period_s` while the link goes down and up
            end = time.monotonic() + float(spec.get("duration_s", 120))
            i = 0
            while time.monotonic() < end:
                msg = message("P2", me, peers, rng.choice(sizes))
                t0 = _now()
                attempts = 0
                delivered = None
                while delivered is None and time.monotonic() < end + 90:
                    attempts += 1
                    try:
                        await ask(peers[0], msg)
                        delivered = _now()
                    except Exception:
                        await asyncio.sleep(1.0)  # retry (the same message id)
                lines.append(
                    {
                        "i": i,
                        "id": msg.header.msg_id,
                        "t0": t0,
                        "t1": delivered,
                        "ok": delivered is not None,
                        "attempts": attempts,
                        "late": delivered is not None and delivered - t0 > deadline_s,
                    }
                )
                i += 1
                await asyncio.sleep(max(0.0, float(spec.get("period_s", 2.0)) - (_now() - t0)))
        else:  # P1, P2, P6, P7: request + ack, one at a time
            for i in range(count):
                msg = message(
                    pattern, me, peers, rng.choice(sizes), rng.choice(nl_sizes), attachment=jpeg
                )
                t0 = _now()
                try:
                    await ask(peers[0], msg)
                    t1 = _now()
                    lines.append(
                        {"i": i, "t0": t0, "t1": t1, "ok": True, "miss": (t1 - t0) >= deadline_s}
                    )
                except Exception as exc:
                    lines.append(
                        {
                            "i": i,
                            "t0": t0,
                            "t1": _now(),
                            "ok": False,
                            "miss": True,
                            "err": type(exc).__name__,
                        }
                    )
    finally:
        for c in a2a.values():
            await c.close()
        if requester is not None:
            await requester.close()
        out.write_text("".join(json.dumps(x) + "\n" for x in lines))


def capture(spec: dict[str, Any]) -> None:
    """A packet capture of one interface (AF_PACKET, every frame both ways) that counts
    frame bytes until SIGTERM: checks the interface counters (tcpdump cannot be stopped
    from the lab under the distribution's AppArmor profile)."""
    import signal

    eth_p_all = 0x0003
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(eth_p_all))
    sock.bind((spec["dev"], 0))
    sock.settimeout(0.2)
    total = frames = 0
    stop = False

    def done(*_: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, done)
    Path(spec["ready"]).write_text("ok")
    while not stop:
        try:
            data = sock.recv(65536)
        except TimeoutError:
            continue
        except OSError:
            break
        total += len(data)
        frames += 1
    Path(spec["out"]).write_text(json.dumps({"bytes": total, "frames": frames}))


def main() -> None:
    cmd, raw = sys.argv[1], sys.argv[2]
    spec = json.loads(Path(raw).read_text()) if raw.endswith(".json") else json.loads(raw)
    if cmd == "capture":
        capture(spec)
        return
    if cmd == "serve":
        if spec["binding"] == "broker":
            asyncio.run(serve_mqtt_broker(spec))
        elif spec["binding"] == "mqtt":
            asyncio.run(serve_mqtt(spec))
        else:
            asyncio.run(serve_a2a(spec))
    else:
        asyncio.run(run_client(spec))


if __name__ == "__main__":
    main()
