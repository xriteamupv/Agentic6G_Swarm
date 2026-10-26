"""``a6g`` operator CLI.

Talks to the robots' skill servers (arm/disarm/killswitch), the coordinator on the LangGraph
Agent Server (goal/resume/status, ``$A6G_COORDINATOR_API``) and the simulation stack
(``sim ...``, ``$A6G_SIM_URL``).
"""

import asyncio
import getpass
import json
import os
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer
from a6g_common.config import A6GConfig, ConfigError, FleetRobot, load_config, load_manifest
from a6g_common.jsonschema_export import export_json_schemas
from a6g_observability.decision_log import DecisionLog, format_timeline

app = typer.Typer(help="Agentic6G UC2 swarm operator CLI.", no_args_is_help=True)
schemas_app = typer.Typer(help="Shared schema utilities.", no_args_is_help=True)
config_app = typer.Typer(help="Configuration utilities.", no_args_is_help=True)
sim_app = typer.Typer(help="Simulator control and fault injection.", no_args_is_help=True)
inject_app = typer.Typer(help="Inject a fault or event into the simulator.", no_args_is_help=True)
app.add_typer(schemas_app, name="schemas")
app.add_typer(config_app, name="config")
app.add_typer(sim_app, name="sim")
comms_app = typer.Typer(help="Inter-agent traffic (SPEC A2.5).", no_args_is_help=True)
app.add_typer(comms_app, name="comms")
kb_app = typer.Typer(help="Campus knowledge server (SPEC A3.3).", no_args_is_help=True)
app.add_typer(kb_app, name="kb")
notice_app = typer.Typer(help="Operator notices.", no_args_is_help=True)
kb_app.add_typer(notice_app, name="notice")


campus_app = typer.Typer(
    help="Campus world and map generation (SPEC A1 Section 5).", no_args_is_help=True
)
app.add_typer(campus_app, name="campus")
sim_app.add_typer(inject_app, name="inject")

ConfigDirOpt = Annotated[
    Path | None,
    typer.Option(
        "--config-dir", "-c", help="Config directory (default: $A6G_CONFIG_DIR or ./config)"
    ),
]


OPERATOR_TOKEN_ENV = "A6G_OPERATOR_TOKEN"


def _http_client() -> httpx.Client:
    """HTTP client for the operator channel (patched in tests)."""
    return httpx.Client(timeout=5.0)


def _bus(cfg: A6GConfig):
    """MQTT bus for operator commands (patched in tests)."""
    from a6g_transport.messaging import configure_messaging, mqtt_bus

    configure_messaging(cfg.comms)
    mqtt = cfg.fleet.edge.mqtt
    return mqtt_bus(mqtt.host, mqtt.port, agent_id="operator", client_id=f"a6g-cli-{os.getpid()}")


def _mcp_target(robot: FleetRobot):
    """What the MCP client connects to for ``robot`` (patched in tests)."""
    return robot.mcp_url


# --------------------------------------------------------------------------- implemented


@schemas_app.command("export")
def schemas_export(
    out: Annotated[Path, typer.Option("--out", "-o", help="Output directory")] = Path("schemas"),
) -> None:
    """Export JSON Schemas of the shared messages for partners."""
    for path in export_json_schemas(out):
        typer.echo(str(path))


@config_app.command("validate")
def config_validate(
    config_dir: ConfigDirOpt = None,
    manifest: Annotated[
        Path | None, typer.Option("--manifest", "-m", help="Validate a single robot manifest")
    ] = None,
    profile: Annotated[
        str | None, typer.Option("--profile", help="sim | gazebo | real (config/profiles/)")
    ] = None,
) -> None:
    """Validate the configuration tree (or one manifest). Exits 1 on any problem."""
    try:
        if manifest is not None:
            m = load_manifest(manifest)
            typer.echo(f"OK manifest {m.robot_id} ({m.platform.value})")
            return
        cfg = load_config(config_dir, profile=profile)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    robots = ", ".join(f"{k} ({v.platform.value})" for k, v in cfg.manifests.items())
    shown = f"; profile {cfg.profile.profile.value}" if cfg.profile else ""
    typer.echo(f"OK fleet {cfg.fleet.fleet_id}: {robots}; campus {cfg.campus.campus_id}{shown}")


@app.command("adapter-config")
def adapter_config_cmd(
    robot_id: str,
    profile: Annotated[str, typer.Option("--profile", help="gazebo | real")],
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
    image_dir: Annotated[str, typer.Option("--image-dir")] = "var/images",
    config_dir: ConfigDirOpt = None,
) -> None:
    """Write the ros_adapter JSON config for one robot (manifest + profile)."""
    from a6g_common.config import adapter_config

    try:
        cfg = load_config(config_dir, profile=profile)
        if robot_id not in cfg.manifests:
            raise ConfigError(cfg.config_dir, [f"unknown robot {robot_id!r}"])
        text = json.dumps(adapter_config(cfg, robot_id, image_dir), indent=1)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    if output is None:
        typer.echo(text)
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n")
        typer.echo(f"wrote {output}")


def _kb(config_dir, coro_factory):
    """Run one call on the knowledge server as the operator; print the JSON answer."""
    from a6g_kb import KnowledgeClient, KnowledgeUnavailable

    cfg = load_config(config_dir)
    client = KnowledgeClient(cfg.knowledge.url, agent_id="operator", timeout_s=10.0)
    try:
        result = asyncio.run(coro_factory(client))
    except KnowledgeUnavailable as exc:
        typer.echo(f"knowledge server unavailable at {cfg.knowledge.url}: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps(result, indent=2, ensure_ascii=False))


@kb_app.command("resolve")
def kb_resolve(text: str, config_dir: ConfigDirOpt = None) -> None:
    """Which campus place is this? (es, ca-valencia, en)"""
    _kb(config_dir, lambda kb: kb.resolve_place(text))


@kb_app.command("route")
def kb_route(
    start: str,
    goal: str,
    profile: Annotated[str, typer.Option("--profile")] = "default",
    config_dir: ConfigDirOpt = None,
) -> None:
    """A people route between two places for a routing profile (step_free, ...)."""
    _kb(config_dir, lambda kb: kb.plan_route(start, goal, profile))


@kb_app.command("status")
def kb_status(subjects: list[str], config_dir: ConfigDirOpt = None) -> None:
    """Effective status of nodes, edges or POIs (rule 27)."""
    _kb(config_dir, lambda kb: kb.status(subjects))


@kb_app.command("search")
def kb_search(query: str, k: int = 5, config_dir: ConfigDirOpt = None) -> None:
    """Search the documents and place descriptions."""
    _kb(config_dir, lambda kb: kb.search(query, k))


@notice_app.command("add")
def kb_notice_add(
    text: str,
    applies_to: Annotated[list[str], typer.Option("--applies-to", help="POI / node / edge ids")],
    notice_id: Annotated[str | None, typer.Option("--id")] = None,
    status: Annotated[
        str | None,
        typer.Option("--status", help="closed | obstructed | flooded | out_of_service | ok"),
    ] = None,
    lang: Annotated[str, typer.Option("--lang")] = "en",
    valid_until: Annotated[str | None, typer.Option("--valid-until", help="ISO time")] = None,
    config_dir: ConfigDirOpt = None,
) -> None:
    """Publish a notice (e.g. "Path closed for works"); with --status it also sets the
    status of what it applies to."""
    import uuid

    doc = {
        "id": notice_id or f"notice_{uuid.uuid4().hex[:8]}",
        "kind": "notice",
        "text": text,
        "lang": lang,
        "applies_to": list(applies_to),
        "valid_until": valid_until,
        "source": "operator",
    }
    _kb(config_dir, lambda kb: kb.add_notice(doc, status))


@comms_app.command("report")
def comms_report(
    mission: Annotated[str, typer.Option("--mission", help="Mission id")],
    log: Annotated[
        list[Path] | None,
        typer.Option("--log", help="Message log(s) (default: comms.yaml path + var/*/comms.jsonl)"),
    ] = None,
    csv_out: Annotated[Path | None, typer.Option("--csv", help="Write the messages as CSV")] = None,
    summary_only: Annotated[bool, typer.Option("--summary-only")] = False,
    config_dir: ConfigDirOpt = None,
) -> None:
    """Per-mission traffic: every message, and bytes and latency per interaction pattern."""
    from a6g_cli.comms_report import default_logs, render

    paths = log or default_logs(config_dir)
    text, records = render(mission, paths, messages=not summary_only)
    if not records:
        typer.echo(f"no messages of mission {mission} in {', '.join(map(str, paths))}", err=True)
        raise typer.Exit(code=1)
    typer.echo(text)
    if csv_out is not None:
        from a6g_observability.comms import write_records_csv

        typer.echo(f"wrote {write_records_csv(csv_out, records)}")


decisions_app = typer.Typer(help="The decision log (SPEC 12, A3.9).")
app.add_typer(decisions_app, name="decisions")
LogOpt = Annotated[
    Path | None, typer.Option("--log", help="Decision log (default from fleet.yaml)")
]


def _decision_log(log: Path | None, config_dir: Path | None) -> Path:
    if log is not None:
        return log
    try:
        return Path(load_config(config_dir).fleet.edge.decision_log_path)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@decisions_app.callback(invoke_without_command=True)
def decisions(
    ctx: typer.Context,
    mission: Annotated[str | None, typer.Option("--mission", help="Mission id")] = None,
    log: LogOpt = None,
    config_dir: ConfigDirOpt = None,
) -> None:
    """Print the auditable decision timeline (``verify``: check the hash chain)."""
    if ctx.invoked_subcommand is not None:
        return
    log = _decision_log(log, config_dir)
    records = DecisionLog(log).read(mission_id=mission)
    if not records:
        typer.echo("no decisions found", err=True)
        raise typer.Exit(code=1)
    typer.echo(format_timeline(records))


@decisions_app.command("verify")
def decisions_verify(
    mission: Annotated[str | None, typer.Option("--mission", help="Mission id")] = None,
    log: LogOpt = None,
    config_dir: ConfigDirOpt = None,
) -> None:
    """Check the tamper-evident chain of the whole log (SPEC A3.9): fails on a modified,
    deleted, reordered or inserted record."""
    from a6g_observability.decision_log import verify

    result = verify(_decision_log(log, config_dir), mission)
    for problem in result["problems"]:
        typer.echo(problem, err=True)
    scope = f", {result['mission_records']} of mission {mission}" if mission else ""
    legacy = f", {result['legacy']} from before the chain" if result["legacy"] else ""
    typer.echo(f"{'OK' if result['ok'] else 'FAILED'}: {result['records']} records{scope}{legacy}")
    if not result["ok"]:
        raise typer.Exit(code=1)


def _set_armed(robot_id: str, armed: bool, config_dir: Path | None, operator: str | None) -> None:
    operator = operator or getpass.getuser()
    try:
        robot = load_config(config_dir).fleet.robot(robot_id)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    except KeyError as exc:
        typer.echo(f"unknown robot {robot_id!r}", err=True)
        raise typer.Exit(code=1) from exc
    token = os.environ.get(OPERATOR_TOKEN_ENV)
    if not token:
        typer.echo(f"set {OPERATOR_TOKEN_ENV} to use the operator channel", err=True)
        raise typer.Exit(code=1)
    url = f"{robot.operator_url}/operator/{'arm' if armed else 'disarm'}"
    try:
        with _http_client() as client:
            response = client.post(
                url, json={"operator": operator}, headers={"Authorization": f"Bearer {token}"}
            )
    except httpx.HTTPError as exc:
        typer.echo(f"{robot_id}: cannot reach {url}: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if response.status_code != 200:
        typer.echo(f"{robot_id}: {response.status_code} {response.text}", err=True)
        raise typer.Exit(code=1)
    status = response.json()
    typer.echo(f"{robot_id}: {'ARMED' if status['armed'] else 'disarmed'}")
    if status["armed"] != armed:
        raise typer.Exit(code=1)


OperatorOpt = Annotated[
    str | None,
    typer.Option("--operator", help="Operator name for the audit trail (default: $USER)"),
]


@app.command("arm")
def arm(robot_id: str, config_dir: ConfigDirOpt = None, operator: OperatorOpt = None) -> None:
    """Arm a robot for motion (operator only; needs A6G_OPERATOR_TOKEN)."""
    _set_armed(robot_id, True, config_dir, operator)


@app.command("disarm")
def disarm(robot_id: str, config_dir: ConfigDirOpt = None, operator: OperatorOpt = None) -> None:
    """Disarm a robot."""
    _set_armed(robot_id, False, config_dir, operator)


# --------------------------------------------------------------------------- stubs


KILL_PATHS = ("mqtt", "mcp")


async def _killswitch(
    cfg: A6GConfig,
    engage: bool,
    robot_ids: list[str],
    operator: str,
    targeted: bool = False,
    paths: tuple[str, ...] = KILL_PATHS,
) -> dict[str, dict[str, str]]:
    """Publish on a6g/killswitch AND call stop on each robot's MCP; report each path.

    ``paths`` restricts the paths for drills (SPEC A1 G7: either path alone must stop).
    """
    from a6g_transport.messaging import TOPIC_KILLSWITCH
    from mcp import Client

    report: dict[str, dict[str, str]] = {rid: {} for rid in robot_ids}
    payload = {
        "state": "on" if engage else "off",
        "robot_id": robot_ids[0] if targeted else None,
        "issued_by": operator,
    }
    if "mqtt" in paths:
        try:
            async with asyncio.timeout(5), _bus(cfg) as bus:
                await bus.publish(TOPIC_KILLSWITCH, json.dumps(payload), qos=1, retain=True)
            mqtt = "published"
        except Exception as exc:
            mqtt = f"FAILED ({exc!r})"
        for rid in robot_ids:
            report[rid]["mqtt"] = mqtt
    if not engage or "mcp" not in paths:
        return report

    async def stop(rid: str) -> None:
        try:
            async with asyncio.timeout(5), Client(_mcp_target(cfg.fleet.robot(rid))) as client:
                result = await client.call_tool("stop", {})
            outcome = (result.structured_content or {}).get("outcome", "error")
            report[rid]["mcp_stop"] = outcome if outcome == "succeeded" else f"FAILED ({outcome})"
        except Exception as exc:
            report[rid]["mcp_stop"] = f"FAILED ({exc!r})"

    await asyncio.gather(*(stop(rid) for rid in robot_ids))
    return report


@app.command("killswitch")
def killswitch(
    state: Annotated[str, typer.Argument(help="on | off")],
    robot: Annotated[str | None, typer.Option("--robot", help="Single robot")] = None,
    config_dir: ConfigDirOpt = None,
    operator: OperatorOpt = None,
    only: Annotated[
        str | None,
        typer.Option("--only", help="drill: use only this path (mqtt | mcp)", hidden=True),
    ] = None,
) -> None:
    """Kill-switch: MQTT broadcast AND direct MCP stop (either path alone stops a robot)."""
    if only not in (None, *KILL_PATHS):
        typer.echo("--only must be mqtt or mcp", err=True)
        raise typer.Exit(code=2)
    if state not in ("on", "off"):
        typer.echo("state must be 'on' or 'off'", err=True)
        raise typer.Exit(code=2)
    try:
        cfg = load_config(config_dir)
        robots = (
            [cfg.fleet.robot(robot).robot_id] if robot else [r.robot_id for r in cfg.fleet.robots]
        )
    except (ConfigError, KeyError) as exc:
        typer.echo(f"{exc}", err=True)
        raise typer.Exit(code=1) from exc
    report = asyncio.run(
        _killswitch(
            cfg,
            state == "on",
            robots,
            operator or getpass.getuser(),
            robot is not None,
            (only,) if only else KILL_PATHS,
        )
    )
    failed = False
    for rid, paths in report.items():
        typer.echo(f"{rid}: " + ", ".join(f"{k}={v}" for k, v in paths.items()))
        if all(v.startswith("FAILED") for v in paths.values()):
            failed = True
    if failed:
        typer.echo("KILL-SWITCH NOT CONFIRMED for at least one robot", err=True)
        raise typer.Exit(code=1)


# --------------------------------------------------------------------------- coordinator

COORDINATOR_API_ENV = "A6G_COORDINATOR_API"  # LangGraph Agent Server (langgraph dev: :2024)
SIM_API_ENV = "A6G_SIM_URL"  # a6g-sim-stack control API


def _langgraph_client():
    from langgraph_sdk import get_client

    return get_client(url=os.environ.get(COORDINATOR_API_ENV, "http://127.0.0.1:2024"))


def _last_ai(state: dict) -> str:
    for message in reversed(state.get("messages") or []):
        if (
            message.get("type") if isinstance(message, dict) else getattr(message, "type", "")
        ) == "ai":
            return message["content"] if isinstance(message, dict) else message.content
    return ""


AssistantOpt = Annotated[str, typer.Option("--assistant", help="Graph id in langgraph.json")]
PlainOpt = Annotated[
    bool,
    typer.Option("--plain", help="Talk to `a6g-coordinator` (A2A) instead of the Agent Server"),
]


def _plain(command: str, payload: dict, config_dir: Path | None = None) -> dict:
    """Operator command to the plain coordinator host over A2A (SPEC A1 12.3)."""
    from a6g_transport.messaging import (
        A2AUnavailable,
        A6GMessage,
        MessageType,
        MessagingClient,
        configure_messaging,
    )

    cfg = load_config(config_dir)
    configure_messaging(cfg.comms)
    url = cfg.fleet.edge.coordinator_a2a_url

    async def run() -> dict:
        reply = await MessagingClient(
            url, sender="operator", receiver="coordinator", timeout_s=120
        ).send(A6GMessage(MessageType.OPERATOR, {"command": command, **payload}))
        return reply.payload

    try:
        return asyncio.run(run())
    except A2AUnavailable as exc:
        typer.echo(f"cannot reach the coordinator at {url}: {exc}", err=True)
        raise typer.Exit(code=1) from exc


def mission_thread(mission: str) -> str:
    """Agent Server thread ids are UUIDs: names like ``demo-1`` map to a stable uuid5."""
    import uuid

    try:
        return str(uuid.UUID(mission))
    except ValueError:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"a6g:mission:{mission}"))


async def _goal(client, mission: str, text: str, posture: str, assistant: str) -> dict:
    await client.threads.create(
        thread_id=mission, if_exists="do_nothing", metadata={"a6g": "mission"}
    )
    event = {"type": "goal", "text": text, "posture": posture, "issued_by": "a6g-cli"}
    return await client.runs.wait(
        mission, assistant, input={"event": event}, multitask_strategy="enqueue"
    )


@app.command("goal")
def goal(
    text: str,
    posture: Annotated[str, typer.Option(help="routine | interactive | emergency")] = "routine",
    mission: Annotated[
        str | None, typer.Option(help="Mission id (= thread id); new if omitted")
    ] = None,
    assistant: AssistantOpt = "coordinator",
    plain: PlainOpt = False,
) -> None:
    """Submit a mission goal to the coordinator (LangGraph Agent Server)."""
    import uuid

    thread = mission_thread(mission) if mission else str(uuid.uuid4())
    if plain:
        reply = _plain("goal", {"mission_id": thread, "text": text, "posture": posture})
        typer.echo(f"mission {mission or thread} (thread {thread})\n\n{reply.get('reply', '')}")
        return
    state = asyncio.run(_goal(_langgraph_client(), thread, text, posture, assistant))
    label = f"{mission} (thread {thread})" if mission and mission != thread else thread
    typer.echo(f"mission {label}\n\n{_last_ai(state)}")


async def _resume(
    client,
    mission: str,
    decision: str,
    message: str | None,
    assistant: str,
    args: dict | None = None,
) -> dict:
    body: dict = {"type": decision}
    if decision == "reject" and message:
        body["message"] = message
    if decision == "edit":
        body["edited_action"] = {"name": "operator_edit", "args": args or {}}
    return await client.runs.wait(
        mission, assistant, command={"resume": {"decisions": [body]}}, multitask_strategy="enqueue"
    )


@app.command("resume")
def resume(
    mission_id: str,
    decision: Annotated[str, typer.Argument(help="approve | reject | edit")],
    message: Annotated[str | None, typer.Option(help="reason (reject)")] = None,
    args: Annotated[
        str | None, typer.Option(help='JSON args for edit, e.g. \'{"label": "bag"}\'')
    ] = None,
    assistant: AssistantOpt = "coordinator",
    plain: PlainOpt = False,
) -> None:
    """Answer the coordinator's pending escalation (same format as agent-chat-ui)."""
    if decision not in ("approve", "reject", "edit"):
        typer.echo("decision must be approve, reject or edit", err=True)
        raise typer.Exit(code=2)
    if plain:
        body: dict = {"type": decision, **({"message": message} if message else {})}
        if decision == "edit":
            body["edited_action"] = {"name": "operator_edit", "args": json.loads(args or "{}")}
        reply = _plain("resume", {"mission_id": mission_thread(mission_id), "decisions": [body]})
        typer.echo(reply.get("reply", ""))
        return
    state = asyncio.run(
        _resume(
            _langgraph_client(),
            mission_thread(mission_id),
            decision,
            message,
            assistant,
            json.loads(args) if args else None,
        )
    )
    typer.echo(_last_ai(state))


@app.command("status")
def status(mission_id: str, plain: PlainOpt = False) -> None:
    """Print the coordinator's view of a mission."""
    if plain:
        reply = _plain("status", {"mission_id": mission_thread(mission_id)})
        for tid, t in sorted(reply.get("tasks", {}).items()):
            typer.echo(f"{tid:<40} {t['status']:<12} {t.get('robot_id') or '-'}")
        for pending in reply.get("pending", []):
            typer.echo(f"PENDING ESCALATION: {pending}")
        return

    async def run() -> dict:
        return await _langgraph_client().threads.get_state(mission_thread(mission_id))

    snapshot = asyncio.run(run())
    values = snapshot.get("values") or {}
    for tid, t in sorted((values.get("tasks") or {}).items()):
        typer.echo(f"{tid:<40} {t['status']:<12} {t.get('robot_id') or '-'}")
    for task in snapshot.get("tasks") or []:
        for intr in task.get("interrupts") or []:
            typer.echo(f"PENDING ESCALATION: {intr['value']['action_requests'][0]['description']}")


# --------------------------------------------------------------------------- campus


@campus_app.command("build")
def campus_build(
    campus: Annotated[Path, typer.Option("--campus", help="Campus v2 file")] = Path(
        "config/campus/upv_vera_cpi.yaml"
    ),
    out: Annotated[
        Path | None, typer.Option("--out", help="Output dir (default: from file)")
    ] = None,
    refresh: Annotated[bool, typer.Option("--refresh", help="Re-fetch the OSM extract")] = False,
    scale: Annotated[
        float | None,
        typer.Option(
            "--scale", help="campus scale, e.g. 0.1 (default: world.scale or $A6G_WORLD_SCALE)"
        ),
    ] = None,
) -> None:
    """Generate the Gazebo world, Nav2 prior map and 2D-sim obstacles from the campus file."""
    from a6g_campus_tools.build import build

    try:
        result = build(campus, out_dir=out, refresh=refresh, scale=scale)
    except (ConfigError, ValueError, OSError) as exc:
        typer.echo(f"campus build failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"{result.campus_id}: {result.buildings} buildings, {result.trees} trees")
    for path in result.files:
        typer.echo(f"  {path}")


dev_app = typer.Typer(help="Development stack housekeeping.", no_args_is_help=True)
app.add_typer(dev_app, name="dev")


@dev_app.command(
    "serve", context_settings={"allow_extra_args": True, "ignore_unknown_options": True}
)
def dev_serve(
    ctx: typer.Context,
    keep_state: Annotated[
        bool, typer.Option("--keep-state", help="keep the threads of the last session")
    ] = False,
) -> None:
    """Start the coordinator (``langgraph dev --no-browser --no-reload``) on a clean slate:
    the dev server's saved threads (``.langgraph_api/``) are deleted first, and it writes
    none during the session (LANGGRAPH_DISABLE_FILE_PERSISTENCE). Saved state grew to
    gigabytes and stalled every model call (17-35 s for "status", 2026-10-22). Extra
    arguments go to ``langgraph dev``; A6G_UNDERSTANDING etc. pass through."""
    import shutil
    import sys

    from a6g_devstack.reset import PROCESSES, find_root, human, port_in_use, size_of

    root = find_root()
    if port_in_use(PROCESSES["server"][0][0]):
        typer.echo("A coordinator is already running on port 2024: stop it first.", err=True)
        raise typer.Exit(code=1)
    state = root / ".langgraph_api"
    if state.exists() and not keep_state:
        typer.echo(f"Deleting the last session's coordinator state ({human(size_of(state))}).")
        shutil.rmtree(state)
    env = {**os.environ, "LANGGRAPH_DISABLE_FILE_PERSISTENCE": "true"}
    exe = Path(sys.executable).parent / "langgraph"
    args = [str(exe), "dev", "--no-browser", "--no-reload", *ctx.args]
    os.chdir(root)
    os.execve(str(exe), args, env)


@dev_app.command("reset")
def dev_reset(
    only: Annotated[
        str,
        typer.Option(
            "--only",
            help="server: the langgraph dev threads; sim: the simulation stacks' state; all",
        ),
    ] = "all",
    delete: Annotated[
        bool, typer.Option("--delete", help="delete instead of moving to var/reset/")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="do not ask for confirmation")] = False,
) -> None:
    """Start the dev stack fresh: clear the langgraph dev server's threads and runs
    (.langgraph_api, which grows with every robot report) and the simulation stacks' robot
    checkpoints and logs. Stop the stack first."""
    from a6g_devstack import reset as R

    if only not in ("all", *R.PARTS):
        raise typer.BadParameter("one of: all, server, sim", param_hint="--only")
    parts = list(R.PARTS) if only == "all" else [only]
    root = R.find_root()
    if live := R.running(parts):
        typer.echo("Stop these first (Ctrl+C in their terminals):", err=True)
        for who in live:
            typer.echo(f"  - {who}", err=True)
        raise typer.Exit(1)
    items = R.plan(root, parts)
    if not items:
        typer.echo("Nothing to reset.")
        return
    for item in items:
        typer.echo(f"  {item.path.relative_to(root)}  {R.human(item.bytes)}")
    action = "Delete" if delete else f"Move to {R.BACKUPS}/"
    if not yes and not typer.confirm(f"{action} these? The old chat threads go with them"):
        raise typer.Exit(1)
    dest = R.reset(root, items, delete)
    total = R.human(sum(i.bytes for i in items))
    if dest is None:
        typer.echo(f"Deleted ({total}).")
    else:
        typer.echo(f"Moved to {dest.relative_to(root)} ({total}); delete it when no longer needed.")
    typer.echo("Start the stack again: terminal 1, then 2, then 3; open a new chat.")


bench_app = typer.Typer(help="Benches for the KPIs (SPEC A3.8).", no_args_is_help=True)
app.add_typer(bench_app, name="bench")


def _record_start() -> None:
    """Rule 31: the commit a run starts from (its results record this one)."""
    if "A6G_RUN_GIT" not in os.environ:
        from a6g_devstack.manifest import current_git_state

        os.environ["A6G_RUN_GIT"] = json.dumps(current_git_state())


@app.callback()
def _start() -> None:
    _record_start()


def _manifest(cfg: Any, seeds: Any, time_factor: Any = None, **roles: Any) -> dict[str, Any]:
    """Rule 31: what produced a result (commit, command, config hashes, models, network)."""
    from a6g_devstack import manifest

    return manifest.build(
        cfg, seeds=seeds, models=manifest.models_of(cfg, **roles), time_factor=time_factor
    )


def _sim_factor() -> str:
    import os

    return f"up to {float(os.environ.get('A6G_SIM_REALTIME_FACTOR', '50')):g}x"


@bench_app.command("kpi11")
def bench_kpi11(
    seeds: Annotated[int, typer.Option("--seeds", help="seeded runs")] = 20,
    model: Annotated[
        bool, typer.Option("--model", help="the cloud coordinator model of models.yaml")
    ] = False,
    robot_model: Annotated[
        bool,
        typer.Option("--robot-model", help="robots deliberate with onboard_vlm, as in the stacks"),
    ] = False,
    out: Annotated[Path, typer.Option("--out", help="results directory")] = Path("bench/results"),
    config_dir: ConfigDirOpt = None,
) -> None:
    """UC2-KPI-11: posture change to emergency -> reconciled envelopes (p50, p95)."""
    import asyncio

    from a6g_common.config import ModelRole
    from a6g_devstack.bench_kpi11 import run, write

    cfg = load_config(config_dir, fleet_file="sim/fleet.yaml")
    llm, label = None, "fake"
    if model:
        from a6g_common.models import chat_model

        llm = chat_model(ModelRole.COORDINATOR_LLM, cfg.models)
        label = cfg.models.roles[ModelRole.COORDINATOR_LLM].model
    robots = None
    if robot_model:
        from a6g_common.models import chat_model

        robots = chat_model(ModelRole.ONBOARD_VLM, cfg.models)
        label += "+robots"
    from langsmith import tracing_context

    with tracing_context(enabled=False):  # hundreds of runs: no LangSmith traces
        result = asyncio.run(
            run(cfg, seeds, model=llm, robot_model=robots, label=label.replace(":", "_"))
        )
    result["manifest"] = _manifest(
        cfg, seeds, coordinator_llm=llm, onboard_vlm=robots, time_factor=_sim_factor()
    )
    path = write(result, out)
    verdict = "meets" if result["meets_target"] else "misses"
    typer.echo(
        f"UC2-KPI-11 over {seeds} seeds ({label}): p50 {result['p50_s']} s, p95 "
        f"{result['p95_s']} s, max {result['max_s']} s ({verdict} the {result['target_p95_s']} "
        f"s target at p95); all robots answered: p50 {result['acknowledged_p50_s']} s, p95 "
        f"{result['acknowledged_p95_s']} s"
    )
    typer.echo(f"results: {path}")


@bench_app.command("kb")
def bench_kb(
    seeds: Annotated[int, typer.Option("--seeds", help="seeded trips for route compliance")] = 500,
    model: Annotated[
        bool, typer.Option("--model", help="decompose with the cloud coordinator model")
    ] = False,
    embedder: Annotated[
        str, typer.Option("--embedder", help="hashing (offline) or model (the embedder role)")
    ] = "hashing",
    out: Annotated[Path, typer.Option("--out", help="results directory")] = Path("bench/results"),
    config_dir: ConfigDirOpt = None,
) -> None:
    """UC2-KPI-10 / KVI-07: the knowledge server's contribution by ablation (--kb off):
    place resolution, route compliance and decomposition, on paired items."""
    import asyncio

    from a6g_common.config import ModelRole
    from a6g_devstack.bench_kb import run, summary, write

    if embedder not in ("hashing", "model"):
        raise typer.BadParameter("hashing or model", param_hint="--embedder")
    cfg = load_config(config_dir, fleet_file="sim/fleet.yaml")
    llm, label = None, "fake"
    if model:
        from a6g_common.models import chat_model

        llm = chat_model(ModelRole.COORDINATOR_LLM, cfg.models)
        label = cfg.models.roles[ModelRole.COORDINATOR_LLM].model
    emb = None
    if embedder == "model":
        from a6g_kb.serve import make_embedder

        emb = make_embedder(cfg)
        label += "+embedder"
    from langsmith import tracing_context

    with tracing_context(enabled=False):
        result = asyncio.run(
            run(cfg, seeds, model=llm, embedder=emb, label=label.replace(":", "_"))
        )
    result["manifest"] = _manifest(cfg, seeds, coordinator_llm=llm, embedder=emb)
    typer.echo(summary(result))
    typer.echo(f"results: {write(result, out)}")


@bench_app.command("decompose")
def bench_decompose(
    model: Annotated[
        bool, typer.Option("--model", help="decompose with the cloud coordinator model")
    ] = False,
    embedder: Annotated[
        str, typer.Option("--embedder", help="hashing (offline) or model (the embedder role)")
    ] = "hashing",
    out: Annotated[Path, typer.Option("--out", help="results directory")] = Path("bench/results"),
    config_dir: ConfigDirOpt = None,
) -> None:
    """UC2-KPI-9: share of bench/decompose/goals.yaml decomposed exactly (task kinds,
    targets and flow classes; paraphrases, ambiguous goals, unknown places, injections)."""
    import asyncio

    from a6g_common.config import ModelRole
    from a6g_devstack.bench_decompose import run, summary, write

    if embedder not in ("hashing", "model"):
        raise typer.BadParameter("hashing or model", param_hint="--embedder")
    cfg = load_config(config_dir, fleet_file="sim/fleet.yaml")
    llm, label = None, "fake"
    if model:
        from a6g_common.models import chat_model

        llm = chat_model(ModelRole.COORDINATOR_LLM, cfg.models)
        label = cfg.models.roles[ModelRole.COORDINATOR_LLM].model
    emb = None
    if embedder == "model":
        from a6g_kb.serve import make_embedder

        emb = make_embedder(cfg)
        label += "+embedder"
    from langsmith import tracing_context

    with tracing_context(enabled=False):
        result = asyncio.run(run(cfg, model=llm, embedder=emb, label=label.replace(":", "_")))
    result["manifest"] = _manifest(cfg, None, coordinator_llm=llm, embedder=emb)
    typer.echo(summary(result))
    typer.echo(f"results: {write(result, out)}")


@bench_app.command("g10")
def bench_g10(
    seeds: Annotated[int, typer.Option("--seeds", help="seeded runs")] = 3,
    out: Annotated[Path, typer.Option("--out", help="runs directory")] = Path("var/runs/g10"),
    checkpoint_meter: Annotated[
        bool,
        typer.Option("--checkpoint-meter", help="record checkpoint bytes and latency (A4 N2)"),
    ] = False,
    persistence: Annotated[
        str, typer.Option("--persistence", help="coordinator checkpointer: sqlite | postgres")
    ] = "sqlite",
    pg_dsn: Annotated[
        str | None, typer.Option("--pg-dsn", help="Postgres DSN for --persistence postgres")
    ] = None,
    config_dir: ConfigDirOpt = None,
) -> None:
    """Scenario G10 (SPEC A3.11): the D2.1 story in one run per seed (2D simulator, fake
    models), its checks, and the KPI report of each run (kpi.md in the run directory)."""
    import asyncio

    from a6g_devstack import kpi_report, scenario_g10

    cfg = load_config(config_dir, fleet_file="sim/fleet.yaml")
    stack_kw: dict[str, Any] = {"checkpoint_meter": checkpoint_meter}
    if persistence == "postgres":
        stack_kw.update(coordinator_persistence="postgres", pg_dsn=pg_dsn)
    failed = 0
    for seed in range(seeds):
        run_dir = out / f"seed{seed}"
        result = asyncio.run(scenario_g10.run(cfg, run_dir, seed=seed, **stack_kw))
        oks = {k: v["ok"] for k, v in result["checks"].items() if isinstance(v, dict) and "ok" in v}
        good = result["error"] is None and all(oks.values())
        failed += not good
        typer.echo(f"seed {seed}: {'ok' if good else 'FAILED'} {result['error'] or oks}")
        if result["error"] is None:
            rep = kpi_report.report(run_dir, cfg)
            rep["manifest"] = _manifest(cfg, seed, time_factor=_sim_factor())
            kpi_report.write(rep, run_dir)
    typer.echo(f"runs and KPI reports in {out}")
    if failed:
        raise typer.Exit(code=1)


dataset_app = typer.Typer(help="Dataset DS09 (SPEC A3.8).", no_args_is_help=True)
app.add_typer(dataset_app, name="dataset")


@dataset_app.command("export")
def dataset_export(
    run: Annotated[Path, typer.Option("--run", help="a run directory")],
    out: Annotated[Path, typer.Option("--out", help="export directory")],
    mission: Annotated[str | None, typer.Option("--mission", help="Mission id")] = None,
    with_images: Annotated[
        bool, typer.Option("--with-images", help="images without a person in view")
    ] = False,
    config_dir: ConfigDirOpt = None,
) -> None:
    """DS09: telemetry, mission record, perception, failures, KPIs and metadata as JSON
    with a schema; never interaction content (rule 28)."""
    from a6g_devstack.dataset import export

    cfg = load_config(config_dir, fleet_file="sim/fleet.yaml")
    result = export(run, out, cfg, mission, with_images)
    typer.echo(f"DS09 in {result['out']}: {', '.join(result['files'])}")


@bench_app.command("baselines")
def bench_baselines(
    seeds: Annotated[int, typer.Option("--seeds", help="seeded runs per cell")] = 10,
    fleets: Annotated[
        str, typer.Option("--fleets", help="fleet sizes, comma-separated (3, 5, 8)")
    ] = "3,5,8",
    outage_s: Annotated[
        str,
        typer.Option("--outage-s", help="coordinator outage lengths in s, comma-separated; inf"),
    ] = "60,180,300,600,inf",
    horizon_s: Annotated[
        float, typer.Option("--horizon-s", help="mission horizon (simulated s)")
    ] = 3600.0,
    configs: Annotated[
        str, typer.Option("--configs", help="P,B2,B1,B0 (comma-separated)")
    ] = "P,B2,B1,B0",
    jobs: Annotated[int, typer.Option("--jobs", help="runs in parallel (processes)")] = 1,
    csv_out: Annotated[Path, typer.Option("--csv", help="per-run CSV for the paper figure")] = Path(
        "paper/fig_outage.csv"
    ),
    out: Annotated[Path, typer.Option("--out", help="results directory")] = Path("bench/results"),
    runs: Annotated[Path, typer.Option("--runs", help="run directories")] = Path(
        "var/runs/n1_outage"
    ),
    config_dir: ConfigDirOpt = None,
) -> None:
    """B0-B2 against P (SPEC A3.8) and the outage sweep (SPEC A4 N1): six inspections, a
    coordinator crash, a robot failure during the outage, a restart after each outage
    length; UC2-KPI-2, -5, -6, -8, time to resume, duplicates, traffic, processing time."""
    import asyncio

    from a6g_devstack.bench_baselines import ARMS, LABEL, run, summary, write, write_csv

    cfg = load_config(config_dir, fleet_file="sim/fleet.yaml")
    sizes = tuple(int(x) for x in fleets.split(","))
    outages = tuple(float(x) for x in outage_s.split(","))
    by_label = {v: k for k, v in LABEL.items()}
    arms = tuple(by_label[c.strip()] for c in configs.split(",") if c.strip() in by_label) or ARMS
    result = asyncio.run(
        run(
            cfg,
            runs,
            seeds=seeds,
            fleets=sizes,
            arms=arms,
            outages=outages,
            horizon_s=horizon_s,
            jobs=jobs,
        )
    )
    result["manifest"] = _manifest(cfg, seeds, time_factor="up to 200x")
    typer.echo(summary(result))
    typer.echo(f"results: {write(result, out)}")
    typer.echo(f"figure data: {write_csv(result, csv_out)}")


kpi_app = typer.Typer(help="KPI reports (SPEC A3.8).", no_args_is_help=True)
app.add_typer(kpi_app, name="kpi")


@kpi_app.command("report")
def kpi_report_cmd(
    run: Annotated[Path, typer.Option("--run", help="a run directory (a6g bench g10)")],
    results: Annotated[
        Path, typer.Option("--results", help="bench results (KPI-9, 10, 11, 15)")
    ] = Path("bench/results"),
    config_dir: ConfigDirOpt = None,
) -> None:
    """The UC2 KPI table of one run: kpi.json and kpi.md in the run directory."""
    from a6g_devstack import kpi_report

    cfg = load_config(config_dir, fleet_file="sim/fleet.yaml")
    rep = kpi_report.report(run, cfg, results)
    rep["manifest"] = _manifest(cfg, None, time_factor=_sim_factor())
    kpi_report.write(rep, run)
    typer.echo(kpi_report.markdown(rep))


@bench_app.command("orders")
def bench_orders(
    modes: Annotated[
        str, typer.Option("--modes", help="rules_first,model_first (comma-separated)")
    ] = "rules_first,model_first",
    out: Annotated[Path, typer.Option("--out", help="results directory")] = Path("bench/results"),
    config_dir: ConfigDirOpt = None,
) -> None:
    """Unseen orders (bench/orders/unseen.yaml): rules-first against model-first
    understanding with the coordinator model: share understood and latency per order."""
    import asyncio

    from a6g_common.config import ModelRole
    from a6g_common.models import chat_model
    from a6g_devstack.bench_orders import run, summary, write
    from langsmith import tracing_context

    cfg = load_config(config_dir, fleet_file="sim/fleet.yaml")
    llm = chat_model(ModelRole.COORDINATOR_LLM, cfg.models)
    label = cfg.models.roles[ModelRole.COORDINATOR_LLM].model.replace(":", "_")
    with tracing_context(enabled=False):
        result = asyncio.run(run(cfg, llm, label, tuple(modes.split(","))))
    result["manifest"] = _manifest(
        cfg, None, coordinator_llm=llm, time_factor="world paused (who-does-what)"
    )
    typer.echo(summary(result))
    typer.echo(f"results: {write(result, out)}")


@bench_app.command("overhead")
def bench_overhead(
    g10: Annotated[Path, typer.Option("--g10", help="G10 run directories (seed*)")] = Path(
        "var/runs/g10_n2"
    ),
    n1: Annotated[Path, typer.Option("--n1", help="N1 run directories")] = Path(
        "var/runs/n1_outage"
    ),
    results: Annotated[Path, typer.Option("--results", help="bench results (model calls)")] = Path(
        "bench/results"
    ),
    persistence: Annotated[
        str, typer.Option("--persistence", help="coordinator checkpointer of the G10 runs")
    ] = "postgres",
    csv_out: Annotated[Path, typer.Option("--csv", help="paper table")] = Path(
        "paper/table_overhead.csv"
    ),
    out: Annotated[Path, typer.Option("--out", help="results directory")] = Path("bench/results"),
    config_dir: ConfigDirOpt = None,
) -> None:
    """Agent traffic and the overhead of the twin as memory (SPEC A4 N2): per-robot bytes
    per posture and fleet, per-pattern sizes, model calls, checkpoint bytes and latency."""
    from a6g_devstack.bench_overhead import report, write, write_csv

    cfg = load_config(config_dir, fleet_file="sim/fleet.yaml")
    g10_dirs = sorted(d for d in g10.glob("seed*") if (d / "comms.jsonl").exists())
    n1_dirs = sorted(d for d in n1.glob("P_f*_o60_s*") if (d / "comms.jsonl").exists())
    model_results = sorted(results.glob("e5_kpi11_*.json")) + sorted(
        results.glob("orders_unseen_*.json")
    )
    result = report(g10_dirs, n1_dirs, model_results, persistence)
    result["manifest"] = _manifest(cfg, None, time_factor="as recorded by the runs")
    typer.echo(f"G10 runs {len(g10_dirs)}, N1 runs {len(n1_dirs)}")
    for p, row in result["patterns_g10"].items():
        typer.echo(
            f"{p:<4} {row['messages']:>7} msgs  payload {row['payload_mean']:>7} B (p95 "
            f"{row['payload_p95']})  wire {row['wire_mean']:>7} B  nl {row['nl_share']:.1%}"
        )
    typer.echo(f"results: {write(result, out)}")
    typer.echo(f"table: {write_csv(result, csv_out)}")


@bench_app.command("retrieval")
def bench_retrieval(
    seeds: Annotated[int, typer.Option("--seeds", help="seeded scenarios")] = 50,
    embedder: Annotated[
        str, typer.Option("--embedder", help="hashing (offline) or model (the embedder role)")
    ] = "hashing",
    out: Annotated[Path, typer.Option("--out", help="results directory")] = Path("bench/results"),
    config_dir: ConfigDirOpt = None,
) -> None:
    """UC2-KPI-15: precision of three retrievals against simulator ground truth
    (observations near a pose, statuses and notices for a route, hand-over context)."""
    import asyncio

    from a6g_devstack.bench_retrieval import run, summary, write

    if embedder not in ("hashing", "model"):
        raise typer.BadParameter("hashing or model", param_hint="--embedder")
    cfg = load_config(config_dir, fleet_file="sim/fleet.yaml")
    emb, label = None, "fake"
    if embedder == "model":
        from a6g_kb.serve import make_embedder

        emb, label = make_embedder(cfg), "embedder"
    result = asyncio.run(run(cfg, seeds, embedder=emb, label=label))
    result["manifest"] = _manifest(cfg, seeds, embedder=emb)
    typer.echo(summary(result))
    typer.echo(f"results: {write(result, out)}")


@campus_app.command("edit")
def campus_edit(
    campus: Annotated[Path, typer.Option("--campus", help="Campus file to edit")] = Path(
        "config/campus/upv_vera_cpi.yaml"
    ),
    port: Annotated[int, typer.Option("--port", help="Local port of the editor page")] = 8350,
) -> None:
    """Edit the patrol zones and move POIs, staging and recovery points in a browser."""
    import uvicorn
    from a6g_campus_tools.editor import CampusEditor, build_app

    editor = CampusEditor(campus)
    typer.echo(f"Campus editor for {campus}: http://127.0.0.1:{port}  (Ctrl+C to stop)")
    typer.echo("Nothing is written until you press Save; then `a6g campus build` and restart.")
    uvicorn.run(build_app(editor), host="127.0.0.1", port=port, log_level="warning")


@campus_app.command("zones")
def campus_zones(
    config_dir: ConfigDirOpt = None,
    preview: Annotated[
        Path | None, typer.Option("--preview", help="write a map of the split (HTML)")
    ] = None,
) -> None:
    """The patrol zones derived from the footpaths: length, paths, survey route."""
    import json
    import math

    from a6g_devstack.view import PAGE, campus_geometry

    cfg = load_config(config_dir)
    c = cfg.campus
    if not c.footpaths_only:
        typer.echo("patrol_zones.mode is polygons: the sectors are as drawn in the campus file")
    for s in c.sectors:
        walk = sum(
            math.dist((a.x, a.y), (b.x, b.y))
            for a, b in zip(s.survey_waypoints, s.survey_waypoints[1:], strict=False)
        )
        length = 0.0
        if c.route_graph is not None:
            geo = {e.id: e.geometry for e in c.route_graph.edges}
            length = sum(
                sum(math.dist(a, b) for a, b in zip(geo[e], geo[e][1:], strict=False))
                for e in s.path_edges
            )
        typer.echo(
            f"{s.sector_id:10} {s.name:18} paths {length:6.0f} m ({len(s.path_edges)} edges), "
            f"survey {walk:6.0f} m, {len(s.survey_waypoints)} waypoints"
        )
        if s.path_edges:
            typer.echo(f"{'':10} edges: {', '.join(s.path_edges)}")
    if preview is not None:
        data = {"campus": campus_geometry(cfg), "state": {"robots": {}, "statuses": []}}
        html = PAGE.replace("__TITLE__", f"{c.campus_id}: patrol zones").replace(
            "/*__SNAPSHOT__*/", f"window.__SNAPSHOT__ = {json.dumps(data)};"
        )
        preview.write_text(html)
        typer.echo(f"preview: {preview}")


# --------------------------------------------------------------------------- simulator control


def _sim(path: str, payload: dict | None = None) -> None:
    url = os.environ.get(SIM_API_ENV, "http://127.0.0.1:8300").rstrip("/") + path
    try:
        with _http_client() as client:
            response = client.post(url, json=payload or {})
    except httpx.HTTPError as exc:
        typer.echo(f"cannot reach the simulation stack at {url}: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(response.text)
    if response.status_code != 200:
        raise typer.Exit(code=1)


@inject_app.command("robot_fail")
def inject_robot_fail(robot_id: str) -> None:
    """The robot dies (its agent goes silent; the broker announces offline)."""
    _sim("/sim/inject/robot_fail", {"robot_id": robot_id})


@inject_app.command("coordinator_kill")
def inject_coordinator_kill() -> None:
    """Kill the coordinator (in-process mode) or explain how to kill langgraph dev."""
    _sim("/sim/inject/coordinator_kill")


@inject_app.command("link_loss")
def inject_link_loss(robot_id: str, seconds: float) -> None:
    _sim("/sim/inject/link_loss", {"robot_id": robot_id, "seconds": seconds})


@inject_app.command("anomaly")
def inject_anomaly(poi_id: str, label: str) -> None:
    _sim("/sim/inject/anomaly", {"poi_id": poi_id, "label": label})


@inject_app.command("weather_alert")
def inject_weather_alert() -> None:
    """Emulated rain: posture emergency + exposed POIs (D2.1)."""
    _sim("/sim/inject/weather_alert")


@sim_app.command("say")
def sim_say(
    robot_id: str,
    text: str,
    lang: Annotated[
        str | None, typer.Option(help="es | ca | en (default: detected from the text)")
    ] = None,
    end: Annotated[bool, typer.Option("--end", help="the person ends the interaction")] = False,
) -> None:
    """A person speaks to the robot (transcribed text, SPEC A3.4); prints its answer.

    Example: a6g sim say go2_01 "¿Me llevas a la parada del bus? Voy en silla de ruedas."
    then confirm with: a6g sim say go2_01 "sí". The robot's later speech (route, arrival)
    appears in the a6g-sim-stack console."""
    _sim("/sim/say", {"robot_id": robot_id, "text": text, "lang": lang, "end": end})


@sim_app.command("add_robot")
def sim_add_robot(manifest: Path) -> None:
    """Hot-add a robot from the staging area."""
    _sim("/sim/add_robot", {"manifest": str(manifest.resolve())})


if __name__ == "__main__":  # pragma: no cover
    app()
