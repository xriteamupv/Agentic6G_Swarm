# ruff: noqa: E501  (shell and openssl command lines are kept on one line)
"""The emulated radio lab: Linux network namespaces with veth pairs, a bridge
and ``tc netem``, run as a rootless user namespace (``unshare --user --map-root-user --net
--mount``; no sudo needed).

    edge (10.66.0.1)  --veth--  ran (bridge br0)  --veth--  r1 (10.66.0.11) ... rN

``tc netem`` (delay = RTT/2, jitter, loss, rate) sits on the robot side of each robot leg
(uplink) and on the bridge side (downlink), per link profile of ``config/network.yaml``.
The edge leg is the wired core (no netem). Robot-to-robot traffic crosses the bridge, so it
sees two radio legs, as UE-to-UE traffic through the UPF would.

    unshare --user --map-root-user --net --mount python -m a6g_devstack.protolab.lab job.json

Writes one JSON line per block to the job's ``results`` file.
"""

import asyncio
import json
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

EDGE_IP = "10.66.0.1"
PY = sys.executable


def sh(cmd: str, check: bool = True) -> str:
    out = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and out.returncode != 0:
        raise RuntimeError(f"{cmd}: {out.stderr.strip()}")
    return out.stdout


def robot_ip(i: int) -> str:
    return f"10.66.0.{10 + i}"


# --------------------------------------------------------------------------- topology


def build(n: int) -> None:
    os.makedirs("/run/netns", exist_ok=True)
    sh("mount -t tmpfs none /run/netns")
    for ns in ["edge", "ran", *[f"r{i}" for i in range(1, n + 1)]]:
        sh(f"ip netns add {ns}")
        sh(f"ip -n {ns} link set lo up")
    sh("ip -n ran link add br0 type bridge")
    sh("ip -n ran link set br0 up")
    sh("ip link add ve type veth peer name ve-b")
    sh("ip link set ve netns edge")
    sh("ip link set ve-b netns ran")
    sh(f"ip -n edge addr add {EDGE_IP}/24 dev ve")
    sh("ip -n edge link set ve up")
    sh("ip -n ran link set ve-b master br0 up")
    for i in range(1, n + 1):
        sh(f"ip link add v{i} type veth peer name v{i}-b")
        sh(f"ip link set v{i} netns r{i}")
        sh(f"ip link set v{i}-b netns ran")
        sh(f"ip -n r{i} addr add {robot_ip(i)}/24 dev v{i}")
        sh(f"ip -n r{i} link set v{i} up")
        sh(f"ip -n ran link set v{i}-b master br0 up")


def certs(dirpath: Path, n: int) -> None:
    """A local CA and one server certificate for every lab address (TLS 1.3; server
    authentication: mutual TLS is not available in the stacks yet)."""
    dirpath.mkdir(parents=True, exist_ok=True)
    sans = ",".join(
        [f"IP:{EDGE_IP}", *[f"IP:{robot_ip(i)}" for i in range(1, n + 1)], "IP:127.0.0.1"]
    )
    d = str(dirpath)
    sh(
        f"openssl req -x509 -newkey rsa:2048 -nodes -days 7 -subj /CN=a6g-lab-ca -keyout {d}/ca.key -out {d}/ca.crt 2>/dev/null"
    )
    sh(
        f"openssl req -newkey rsa:2048 -nodes -subj /CN=a6g-lab -keyout {d}/server.key -out {d}/server.csr 2>/dev/null"
    )
    ext = dirpath / "ext.cnf"
    ext.write_text(f"subjectAltName={sans}\nbasicConstraints=CA:FALSE\n")
    sh(
        f"openssl x509 -req -in {d}/server.csr -CA {d}/ca.crt -CAkey {d}/ca.key -CAcreateserial -days 7 -extfile {ext} -out {d}/server.crt 2>/dev/null"
    )


def netem(n: int, profile: dict[str, Any]) -> None:
    half = profile["rtt_ms"] / 2
    jit = profile.get("jitter_ms", 0) / 2
    loss = profile.get("loss_pct", 0)
    for i in range(1, n + 1):
        for ns, dev, rate in (
            (f"r{i}", f"v{i}", profile["ul_mbps"]),
            ("ran", f"v{i}-b", profile["dl_mbps"]),
        ):
            sh(f"ip netns exec {ns} tc qdisc del dev {dev} root", check=False)
            parts = f"delay {half}ms"
            if jit:
                parts += f" {jit}ms distribution normal"
            if loss:
                parts += f" loss {loss}%"
            if rate:
                parts += f" rate {rate}mbit"
            sh(f"ip netns exec {ns} tc qdisc add dev {dev} root netem {parts} limit 100000")


def check_rtt(profile: dict[str, Any]) -> dict[str, Any]:
    out = sh(f"ip netns exec r1 ping -c 20 -i 0.2 -q {EDGE_IP}")
    m = re.search(r"= ([\d.]+)/([\d.]+)/([\d.]+)", out)
    avg = float(m.group(2)) if m else None
    ok = avg is not None and abs(avg - profile["rtt_ms"]) <= 0.1 * profile["rtt_ms"]
    return {"configured_ms": profile["rtt_ms"], "measured_ms": avg, "ok": ok}


def counters(ns: str, dev: str) -> tuple[int, int]:
    base = f"/sys/class/net/{dev}/statistics"
    tx = int(sh(f"ip netns exec {ns} cat {base}/tx_bytes"))
    rx = int(sh(f"ip netns exec {ns} cat {base}/rx_bytes"))
    return tx, rx


# --------------------------------------------------------------------------- processes


async def spawn(
    ns: str, cmd: str, spec: dict[str, Any], workdir: Path, name: str
) -> asyncio.subprocess.Process:
    path = workdir / f"{name}.json"
    path.write_text(json.dumps(spec))
    log = (workdir / f"{name}.log").open("w")
    return await asyncio.create_subprocess_exec(
        "ip",
        "netns",
        "exec",
        ns,
        PY,
        "-m",
        "a6g_devstack.protolab.endpoint",
        cmd,
        str(path),
        stdout=log,
        stderr=log,
    )


async def wait_ready(paths: list[Path], timeout: float = 60) -> None:
    end = time.monotonic() + timeout
    while not all(p.exists() for p in paths):
        if time.monotonic() > end:
            raise TimeoutError(f"endpoints not ready: {[str(p) for p in paths if not p.exists()]}")
        await asyncio.sleep(0.1)


async def stop(procs: list[asyncio.subprocess.Process]) -> None:
    for p in procs:
        if p.returncode is None:
            p.terminate()
    for p in procs:
        try:
            await asyncio.wait_for(p.wait(), 5)
        except TimeoutError:
            p.kill()
            await p.wait()


# --------------------------------------------------------------------------- one block


async def block(job: dict[str, Any], b: dict[str, Any], n: int, workdir: Path) -> dict[str, Any]:
    """One (binding, mode, encoding, profile, pattern, nl, rep) block."""
    wd = workdir / b["id"]
    if wd.exists():
        shutil.rmtree(wd)
    wd.mkdir(parents=True)
    cert = str(workdir / "certs")
    binding, pattern = b["binding"], b["pattern"]
    mqtt = binding == "mqtt"
    port = 8443
    procs: list[asyncio.subprocess.Process] = []
    ready: list[Path] = []
    common = {"binding": binding, "certs": cert, "encoding": b.get("encoding", "json")}
    # who serves and who asks
    if pattern == "P1":
        client_ns, client_agent = "edge", "coordinator"
        servers = [("r1", "r1", robot_ip(1))]
    elif pattern == "P3":
        client_ns, client_agent = "r1", "r1"
        servers = [(f"r{i}", f"r{i}", robot_ip(i)) for i in range(2, n + 1)]
    else:  # P2, P4, P6, P7, outage: robot -> coordinator
        client_ns, client_agent = "r1", "r1"
        servers = [("edge", "coordinator", EDGE_IP)]
    try:
        if mqtt:
            r = wd / "broker.ready"
            procs.append(
                await spawn(
                    "edge",
                    "serve",
                    {
                        "binding": "broker",
                        "certs": cert,
                        "host": EDGE_IP,
                        "port": 8883,
                        "ready": str(r),
                    },
                    wd,
                    "broker",
                )
            )
            await wait_ready([r])
        for ns, agent, ip in servers:
            r = wd / f"{agent}.ready"
            spec = {
                **common,
                "agent": agent,
                "host": ip,
                "port": port,
                "ready": str(r),
                "recv": str(wd / f"{agent}.recv.jsonl"),
                "broker": EDGE_IP,
                "broker_port": 8883,
                "reply_bytes": b.get("reply_bytes", 120),
            }
            procs.append(await spawn(ns, "serve", spec, wd, f"srv-{agent}"))
            ready.append(r)
        await wait_ready(ready)
        await asyncio.sleep(0.5)
        legs = [("r1", "v1")] if pattern != "P3" else [(f"r{i}", f"v{i}") for i in range(1, n + 1)]
        before = {leg: counters(*leg) for leg in legs}
        cap = None
        if b.get("capture"):  # a packet capture checks the counters (one block per binding)
            ready_cap = wd / "cap.ready"
            cap = await spawn(
                "r1",
                "capture",
                {"dev": "v1", "out": str(wd / "cap.json"), "ready": str(ready_cap)},
                wd,
                "capture",
            )
            await wait_ready([ready_cap])
        client = {
            **common,
            "agent": client_agent,
            "mode": b.get("mode", "warm"),
            "pattern": pattern,
            "count": b.get("count", 1),
            "duration_s": b.get("duration_s", 300),
            "period_s": 2.0,
            "nl": b.get("nl", "full"),
            "peers": [agent for _, agent, _ in servers],
            "targets": {agent: f"{ip}:{port}" for _, agent, ip in servers},
            "broker": EDGE_IP,
            "broker_port": 8883,
            "payloads": job["payloads"],
            "out": str(wd / "out.jsonl"),
            "seed": b.get("seed", 0),
            "deadline_s": 2.0 if pattern != "P3" else 1.5,
            "cfp_deadline_s": 1.5,
            "timeout_s": 15.0 if pattern != "P6" else 60.0,
        }
        started = time.time()
        cproc = await spawn(client_ns, "run", client, wd, "client")
        outage = None
        if pattern == "outage":  # the robot's link goes down for b["outage_s"] seconds
            await asyncio.sleep(30)
            down_at = time.time()
            sh("ip -n r1 link set v1 down")
            await asyncio.sleep(b["outage_s"])
            sh("ip -n r1 link set v1 up")
            outage = {"down_at": down_at, "up_at": time.time()}
        await asyncio.wait_for(cproc.wait(), b.get("wall_s", 1800))
        if cap is not None:
            await asyncio.sleep(1.0)
            cap.terminate()
            await asyncio.wait_for(cap.wait(), 10)
        after = {leg: counters(*leg) for leg in legs}
        lines = [json.loads(x) for x in (wd / "out.jsonl").read_text().splitlines() if x.strip()]
        up = sum(after[leg][0] - before[leg][0] for leg in legs)
        down = sum(after[leg][1] - before[leg][1] for leg in legs)
        units = max(1, len(lines))
        lat = [x["t1"] - x["t0"] for x in lines if x.get("ok") and x.get("t1")]
        res: dict[str, Any] = {
            **{
                k: b.get(k)
                for k in (
                    "id",
                    "binding",
                    "mode",
                    "encoding",
                    "profile",
                    "pattern",
                    "nl",
                    "rep",
                    "outage_s",
                )
            },
            "n": len(lines),
            "ok": sum(1 for x in lines if x.get("ok")),
            "wire_up_bytes_per_unit": round(up / units, 1),
            "wire_down_bytes_per_unit": round(down / units, 1),
            "wire_bytes_per_unit": round((up + down) / units, 1),
            "latency_ms": {
                "p50": round(_pct(lat, 0.5) * 1000, 2) if lat else None,
                "p95": round(_pct(lat, 0.95) * 1000, 2) if lat else None,
                "p99": round(_pct(lat, 0.99) * 1000, 2) if lat else None,
                "mean": round(statistics.fmean(lat) * 1000, 2) if lat else None,
            },
            "deadline_misses": sum(1 for x in lines if x.get("miss")),
            "timeouts": sum(1 for x in lines if not x.get("ok")),
            "duration_s": round(time.time() - started, 1),
        }
        if cap is not None:
            got = wd / "cap.json"
            pb = json.loads(got.read_text())["bytes"] if got.exists() else 0
            res["pcap_check"] = {
                "capture_bytes": pb,
                "counter_bytes": up + down,
                "ratio": round(pb / max(1, up + down), 3),
            }
        if outage is not None:
            recv = []
            for _, agent, _ in servers:
                p = wd / f"{agent}.recv.jsonl"
                if p.exists():
                    recv += [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
            seen: dict[str, int] = {}
            for r in recv:
                seen[r["msg_id"]] = seen.get(r["msg_id"], 0) + 1
            first_after = min(
                (x["t1"] for x in lines if x.get("t1") and x["t1"] >= outage["up_at"]), default=None
            )
            res["outage"] = {
                **outage,
                "restore_to_first_delivery_s": round(first_after - outage["up_at"], 3)
                if first_after
                else None,
                "lost": sum(1 for x in lines if not x.get("ok")),
                "duplicated": sum(1 for c in seen.values() if c > 1),
                "late": sum(1 for x in lines if x.get("late")),
                "attempts_max": max((x.get("attempts", 1) for x in lines), default=0),
            }
        return res
    finally:
        await stop(procs)


def _pct(values: list[float], q: float) -> float:
    v = sorted(values)
    k = (len(v) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


# --------------------------------------------------------------------------- the job


async def main(job_path: Path) -> None:
    job = json.loads(job_path.read_text())
    workdir = Path(job["workdir"])
    workdir.mkdir(parents=True, exist_ok=True)
    n = int(job["robots"])
    build(n)
    certs(workdir / "certs", n)
    results = Path(job["results"])
    blocks = list(job["blocks"])
    random.Random(job.get("seed", 0)).shuffle(blocks)  # block order shuffled by seed
    current = None
    for b in blocks:
        prof = job["profiles"][b["profile"]]
        if current != b["profile"]:
            netem(n, prof)
            rtt = check_rtt(prof)
            current = b["profile"]
            with results.open("a") as fh:
                fh.write(json.dumps({"rtt_check": b["profile"], **rtt}) + "\n")
            if not rtt["ok"]:
                with results.open("a") as fh:
                    fh.write(
                        json.dumps(
                            {
                                "warning": f"RTT {rtt['measured_ms']} ms is not within 10 % of {rtt['configured_ms']} ms"
                            }
                        )
                        + "\n"
                    )
        try:
            res = await block(job, b, n, workdir)
        except Exception as exc:  # recorded, the lab goes on
            res = {"id": b["id"], "error": repr(exc)}
        res["profile_values"] = prof
        with results.open("a") as fh:
            fh.write(json.dumps(res) + "\n")


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
