# ruff: noqa: E501, RUF001  (the embedded HTML/JS page is kept as written)
"""Live campus view for the simulated stacks: ``http://127.0.0.1:8300/view``.

A self-contained page (no external assets) that draws the campus once from
``/sim/view/campus`` and polls ``/sim/view/state`` a few times a second: robots with their
heading, footprint, trail, current task, hold / link-recovery state, link quality and
failures; the footpath graph, restrictive statuses in red and the routes of people being
guided (SPEC A3.3-A3.4). Served by both ``a6g-sim-stack`` (2D simulator) and ``a6g-gz-stack`` (Gazebo).
"""

from collections.abc import Awaitable, Callable
from typing import Any

from a6g_common.config import A6GConfig
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route


def campus_geometry(cfg: A6GConfig) -> dict[str, Any]:
    c = cfg.campus
    edges = {e.id: e.geometry for e in (c.route_graph.edges if c.route_graph else [])}
    return {
        "campus_id": c.campus_id,
        "scale": c.scale,
        "pilot_area": c.pilot_area,
        "buildings": c.obstacles,
        "staging": {"x": c.staging.x, "y": c.staging.y, "area": c.staging_area},
        "sectors": [
            {
                "id": s.sector_id,
                "name": s.name,
                "polygon": s.polygon,
                "waypoints": [(w.x, w.y) for w in s.survey_waypoints],
                # Footpath zones: the paths the zone covers (drawn in the zone's colour).
                "paths": [edges[e] for e in s.path_edges if e in edges],
            }
            for s in c.sectors
        ],
        "pois": [
            {"id": p.poi_id, "name": p.name, "x": p.pose.x, "y": p.pose.y, "exposed": p.exposed}
            for p in c.pois
        ],
        "shadows": c.link_shadows,
        "degraded": c.degraded_shadows,
        "recovery": [
            {"id": r.point_id, "name": r.label, "x": r.pose.x, "y": r.pose.y}
            for r in c.link_recovery_points
        ],
        "robots": {
            rid: {
                "platform": m.platform.value,
                "length": m.footprint.length_m,
                "width": m.footprint.width_m,
            }
            for rid, m in cfg.manifests.items()
        },
        # The people-route graph (SPEC A3.3.1): OSM footways, manual items, open-space links.
        "footpaths": [
            {"id": e.id, "source": e.source, "stairs": e.stairs, "pts": e.geometry}
            for e in (c.route_graph.edges if c.route_graph else [])
        ],
    }


def status_overlay(kb_service: Any) -> list[dict[str, Any]]:
    """Restrictive statuses in force (rule 27), drawn red: an edge as its polyline, a node
    or POI as a point."""
    if kb_service is None:
        return []
    campus = kb_service.campus
    graph = campus.route_graph
    nodes = {n.id: n for n in graph.nodes} if graph else {}
    edges = {e.id: e for e in graph.edges} if graph else {}
    pois = {p.poi_id: p.pose for p in campus.pois}
    out = []
    try:
        restricted = kb_service.statuses.restricted(kb_service.now())
    except Exception:  # a view must never break the stack
        return []
    for subject, reports in sorted(restricted.items()):
        item: dict[str, Any] = {"subject": subject, "status": reports[-1].status}
        if subject in edges:
            item["pts"] = edges[subject].geometry
        elif subject in nodes:
            item["x"], item["y"] = nodes[subject].x, nodes[subject].y
        elif subject in pois:
            item["x"], item["y"] = pois[subject].x, pois[subject].y
        else:
            continue
        out.append(item)
    return out


def view_routes(
    cfg: A6GConfig, state: Callable[[], Awaitable[dict[str, Any]]], title: str
) -> list[Route]:
    async def page(request: Request) -> HTMLResponse:
        html = PAGE.replace("__TITLE__", title)
        if request.query_params.get("static"):  # snapshot: data inline, drawn on load
            import json

            data = json.dumps({"campus": campus_geometry(cfg), "state": await state()})
            html = html.replace("/*__SNAPSHOT__*/", f"window.__SNAPSHOT__ = {data};")
        return HTMLResponse(html)

    async def campus(request: Request) -> JSONResponse:
        return JSONResponse(campus_geometry(cfg))

    async def live(request: Request) -> JSONResponse:
        return JSONResponse(await state())

    return [
        Route("/view", page),
        Route("/sim/view/campus", campus),
        Route("/sim/view/state", live),
    ]


async def robot_activity(agent: Any) -> dict[str, Any]:
    if agent is None:
        return {"task": None, "agent": False}
    try:
        return {**await agent.activity(), "agent": True}
    except Exception:  # a view must never break the stack
        return {"task": None, "agent": True}


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root {
    --bg: #f6f7f5; --panel: #ffffff; --ink: #1d2320; --muted: #66706b; --line: #d8ddd9;
    --ground: #eceee9; --building: #b9b3a8; --building-edge: #8f887c; --pilot: #d9a400;
    --sector: 46,125,180; --shadow: 200,60,60; --poi: #2f6fb0; --exposed: #d9711c;
    --recovery: #2f9a5b; --staging: 47,154,91; --path: #9b7b52;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #141816; --panel: #1c211f; --ink: #e6ebe8; --muted: #9aa5a0; --line: #2d3431;
      --ground: #1a1f1d; --building: #4a4741; --building-edge: #6b665d; --pilot: #e0b021;
      --sector: 90,160,220; --shadow: 230,90,90; --poi: #6aa5e6; --exposed: #f0954a;
      --recovery: #4cc282; --staging: 76,194,130; --path: #b8946a;
    }
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; background: var(--bg); color: var(--ink);
    font: 14px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; }
  .app { display: grid; grid-template-columns: 1fr 320px; height: 100%; }
  @media (max-width: 800px) { .app { grid-template-columns: 1fr; grid-template-rows: 60vh auto; } }
  .map { position: relative; overflow: hidden; }
  canvas { display: block; width: 100%; height: 100%; cursor: grab; }
  canvas:active { cursor: grabbing; }
  .hud { position: absolute; left: 12px; top: 10px; background: var(--panel);
    border: 1px solid var(--line); border-radius: 8px; padding: 6px 10px; font-size: 12px; }
  .hud b { font-variant-numeric: tabular-nums; }
  aside { background: var(--panel); border-left: 1px solid var(--line); overflow-y: auto;
    padding: 14px 16px; }
  h1 { font-size: 15px; margin: 0 0 2px; }
  .sub { color: var(--muted); font-size: 12px; margin-bottom: 12px; }
  .robot { border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; margin-bottom: 8px; }
  .robot .top { display: flex; align-items: center; gap: 8px; }
  .dot { width: 10px; height: 10px; border-radius: 50%; flex: none; }
  .id { font-weight: 600; }
  .tag { margin-left: auto; font-size: 11px; padding: 1px 7px; border-radius: 999px;
    border: 1px solid var(--line); color: var(--muted); }
  .tag.lost { color: #c24141; border-color: #c24141; }
  .tag.failed { background: #c24141; color: #fff; border-color: #c24141; }
  .tag.moving { color: var(--recovery); border-color: var(--recovery); }
  .line { color: var(--muted); font-size: 12px; margin-top: 3px; overflow-wrap: anywhere; }
  .legend { margin-top: 14px; font-size: 12px; color: var(--muted); display: grid; gap: 4px; }
  .legend span { display: inline-block; width: 14px; height: 10px; margin-right: 6px;
    vertical-align: -1px; border-radius: 2px; }
  .err { color: #c24141; font-size: 12px; }
</style>
</head>
<body>
<div class="app">
  <div class="map">
    <canvas id="c" aria-label="Campus map with live robot positions"></canvas>
    <div class="hud">sim time <b id="t">–</b> · <span id="fps">–</span> · drag to pan, wheel to zoom, double-click to fit</div>
  </div>
  <aside>
    <h1>__TITLE__</h1>
    <div class="sub" id="sub">connecting…</div>
    <div id="robots"></div>
    <div class="legend">
      <div><span style="background:var(--building)"></span>building (OSM)</div>
      <div><span style="background:rgba(var(--sector),.18);border:1px solid rgb(var(--sector))"></span>patrol zone (its footpaths, one colour each) · dots = survey waypoints</div>
      <div><span style="background:rgba(var(--shadow),.18);border:1px dashed rgb(var(--shadow))"></span>5G coverage shadow (link lost) · <span style="background:rgba(217,113,28,.18);border:1px dashed rgb(217,113,28)"></span>degraded</div>
      <div><span style="background:var(--poi)"></span>point of interest · <span style="background:var(--exposed)"></span>exposed</div>
      <div><span style="background:var(--recovery)"></span>link recovery point · staging square</div>
      <div><span style="background:transparent;border:2px solid var(--pilot)"></span>pilot area</div>
      <div><label><input type="checkbox" id="paths" checked> <span style="background:var(--path)"></span>footpath (OSM) · dashed: open space · <span style="background:#c24141"></span>stairs</label></div>
      <div><span style="background:#e02424"></span>restricted (closed, obstructed, flooded…)</div>
      <div><span style="background:transparent;border:2px solid var(--ink)"></span>person's route (robot colour) · dotted: to meet them</div>
    </div>
    <div class="err" id="err"></div>
  </aside>
</div>
<script>
const COLORS = ["#2f6fb0", "#d9711c", "#8a4fbf", "#2f9a5b", "#c24141", "#1b9aa0", "#9c7a1a"];
const ZONES = ["46,125,180", "150,80,190", "27,154,160", "200,120,30", "90,140,60"];
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const canvas = document.getElementById("c"), ctx = canvas.getContext("2d");
let campus = null, state = null, view = null, trails = {}, colors = {}, frames = 0, last = performance.now();

function fit() {
  if (!campus) return;
  const pts = [...campus.pilot_area, ...campus.sectors.flatMap(s => s.polygon)];
  const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]);
  const w = canvas.clientWidth, h = canvas.clientHeight, pad = 30;
  const sx = (w - 2 * pad) / (Math.max(...xs) - Math.min(...xs) || 1);
  const sy = (h - 2 * pad) / (Math.max(...ys) - Math.min(...ys) || 1);
  const k = Math.min(sx, sy);
  view = { k, cx: (Math.max(...xs) + Math.min(...xs)) / 2, cy: (Math.max(...ys) + Math.min(...ys)) / 2 };
}
const W = () => canvas.clientWidth, H = () => canvas.clientHeight;
const toPx = (x, y) => [W() / 2 + (x - view.cx) * view.k, H() / 2 - (y - view.cy) * view.k];

function poly(points, fill, stroke, dash) {
  if (!points || points.length < 2) return;
  ctx.beginPath();
  points.forEach((p, i) => { const [u, v] = toPx(p[0], p[1]); i ? ctx.lineTo(u, v) : ctx.moveTo(u, v); });
  ctx.closePath();
  if (fill) { ctx.fillStyle = fill; ctx.fill(); }
  if (stroke) { ctx.setLineDash(dash || []); ctx.strokeStyle = stroke; ctx.stroke(); ctx.setLineDash([]); }
}

function label(text, x, y, color, dy = -8) {
  const [u, v] = toPx(x, y);
  ctx.font = "11px system-ui, sans-serif"; ctx.textAlign = "center";
  ctx.lineWidth = 3; ctx.strokeStyle = css("--bg"); ctx.strokeText(text, u, v + dy);
  ctx.fillStyle = color; ctx.fillText(text, u, v + dy); ctx.lineWidth = 1;
}

function draw() {
  frames++;
  const dpr = window.devicePixelRatio || 1;
  if (canvas.width !== W() * dpr || canvas.height !== H() * dpr) {
    canvas.width = W() * dpr; canvas.height = H() * dpr; if (!view) fit();
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = css("--bg"); ctx.fillRect(0, 0, W(), H());
  if (campus && view) {
    poly(campus.pilot_area, css("--ground"), null);
    const sector = css("--sector"), shadow = css("--shadow"), staging = css("--staging");
    campus.sectors.forEach((s, i) => {
      if (s.paths && s.paths.length) {  // a footpath zone: its paths, in its own colour
        const col = ZONES[i % ZONES.length];
        poly(s.polygon, `rgba(${col},.05)`, null);
        for (const p of s.paths) line(p, `rgb(${col})`, 7, null, 0.35);
        ctx.fillStyle = `rgb(${col})`;
        for (const [x, y] of s.waypoints) { const [u, v] = toPx(x, y); ctx.beginPath(); ctx.arc(u, v, 2.5, 0, 7); ctx.fill(); }
        const m = s.paths[Math.floor(s.paths.length / 2)], q = m[Math.floor(m.length / 2)];
        label(s.name || s.id, q[0], q[1], `rgb(${col})`, -10);
        return;
      }
      poly(s.polygon, `rgba(${sector},.13)`, `rgb(${sector})`);
      ctx.fillStyle = `rgb(${sector})`;
      for (const [x, y] of s.waypoints) { const [u, v] = toPx(x, y); ctx.beginPath(); ctx.arc(u, v, 2.5, 0, 7); ctx.fill(); }
      const c = s.polygon.reduce((a, p) => [a[0] + p[0] / s.polygon.length, a[1] + p[1] / s.polygon.length], [0, 0]);
      label(s.name || s.id, c[0], c[1], `rgb(${sector})`, 4);
    });
    for (const sh of campus.shadows) poly(sh, `rgba(${shadow},.15)`, `rgb(${shadow})`, [5, 4]);
    for (const sh of campus.degraded || []) poly(sh, "rgba(217,113,28,.12)", "rgb(217,113,28)", [5, 4]);
    ctx.lineWidth = 1;
    for (const b of campus.buildings) poly(b, css("--building"), css("--building-edge"));
    if (document.getElementById("paths").checked) drawPaths();
    ctx.lineWidth = 2; poly(campus.pilot_area, null, css("--pilot"), [8, 5]); ctx.lineWidth = 1;
    if (campus.staging.area) poly(campus.staging.area, `rgba(${staging},.10)`, `rgb(${staging})`, [3, 3]);
    for (const r of campus.recovery) {
      const [u, v] = toPx(r.x, r.y); ctx.fillStyle = css("--recovery");
      ctx.beginPath(); ctx.moveTo(u, v - 7); ctx.lineTo(u - 6, v + 5); ctx.lineTo(u + 6, v + 5); ctx.closePath(); ctx.fill();
      label(r.name || r.id.replace("lrp_", ""), r.x, r.y, css("--recovery"), 17);
    }
    for (const p of campus.pois) {
      const [u, v] = toPx(p.x, p.y), col = p.exposed ? css("--exposed") : css("--poi");
      ctx.fillStyle = col; ctx.beginPath(); ctx.arc(u, v, 5, 0, 7); ctx.fill();
      label(p.name || p.id, p.x, p.y, col, -9);
    }
    if (state) { drawStatuses(); drawEscorts(); drawRobots(); }
  }
  requestAnimationFrame(draw);
}

function line(points, color, width, dash, alpha = 1) {
  if (!points || points.length < 2) return;
  ctx.globalAlpha = alpha; ctx.strokeStyle = color; ctx.lineWidth = width; ctx.setLineDash(dash || []);
  ctx.beginPath(); points.forEach((p, i) => { const [u, v] = toPx(p[0], p[1]); i ? ctx.lineTo(u, v) : ctx.moveTo(u, v); });
  ctx.stroke(); ctx.setLineDash([]); ctx.lineWidth = 1; ctx.globalAlpha = 1;
}

function drawPaths() {
  const path = css("--path");
  for (const e of campus.footpaths || []) {
    if (e.stairs) line(e.pts, "#c24141", 2, [4, 3], 0.9);
    else if (e.source === "open") line(e.pts, path, 1, [2, 4], 0.45);
    else if (e.source === "connector") line(e.pts, path, 1, [1, 3], 0.6);
    else line(e.pts, path, 2, null, 0.85);
  }
}

function drawStatuses() {
  for (const s of state.statuses || []) {
    if (s.pts) line(s.pts, "#e02424", 5, null, 0.85);
    let x = s.x, y = s.y;
    if (s.pts && s.pts.length) { const m = s.pts[Math.floor(s.pts.length / 2)]; x = m[0]; y = m[1]; }
    if (x === undefined) continue;
    const [u, v] = toPx(x, y);
    ctx.strokeStyle = "#e02424"; ctx.lineWidth = 3;
    ctx.beginPath(); ctx.moveTo(u - 7, v - 7); ctx.lineTo(u + 7, v + 7); ctx.moveTo(u + 7, v - 7); ctx.lineTo(u - 7, v + 7); ctx.stroke();
    ctx.lineWidth = 1;
    label(s.status.replace(/_/g, " "), x, y, "#e02424", 20);
  }
}

function drawEscorts() {
  for (const [rid, r] of Object.entries(state.robots)) {
    const e = (r.activity || {}).escort;
    if (!e) continue;
    const col = colors[rid];
    line(e.approach, col, 2, [2, 4], 0.8);
    line(e.route, col, 4, [10, 5], 0.9);
    const end = e.route[e.route.length - 1];
    if (end) {
      const [u, v] = toPx(end[0], end[1]);
      ctx.strokeStyle = col; ctx.lineWidth = 3; ctx.beginPath(); ctx.arc(u, v, 8, 0, 7); ctx.stroke(); ctx.lineWidth = 1;
      label("person's destination", end[0], end[1], col, 22);
    }
  }
}

function drawRobots() {
  for (const [rid, r] of Object.entries(state.robots)) {
    if (r.x === undefined) continue;
    const col = colors[rid], geo = campus.robots[rid] || { length: 0.7, width: 0.4 };
    const t = trails[rid] || [];
    if (t.length > 1) {
      ctx.strokeStyle = col; ctx.globalAlpha = 0.45; ctx.lineWidth = 2;
      ctx.beginPath(); t.forEach((p, i) => { const [u, v] = toPx(p[0], p[1]); i ? ctx.lineTo(u, v) : ctx.moveTo(u, v); });
      ctx.stroke(); ctx.globalAlpha = 1; ctx.lineWidth = 1;
    }
    const [u, v] = toPx(r.x, r.y), yaw = r.yaw || 0;
    const L = Math.max(geo.length * view.k, 12), Wd = Math.max(geo.width * view.k, 7);
    ctx.save(); ctx.translate(u, v); ctx.rotate(-yaw);
    ctx.fillStyle = r.failed ? "#888" : col; ctx.strokeStyle = css("--panel"); ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.rect(-L / 2, -Wd / 2, L, Wd); ctx.fill(); ctx.stroke();
    ctx.fillStyle = css("--panel"); ctx.beginPath();
    ctx.moveTo(L / 2, 0); ctx.lineTo(L / 2 - Math.min(6, L / 3), -Wd / 3); ctx.lineTo(L / 2 - Math.min(6, L / 3), Wd / 3); ctx.closePath(); ctx.fill();
    ctx.restore();
    if (r.link === "lost") { ctx.strokeStyle = "#c24141"; ctx.setLineDash([3, 3]); ctx.beginPath(); ctx.arc(u, v, L * 0.8, 0, 7); ctx.stroke(); ctx.setLineDash([]); }
    if (r.failed) { ctx.strokeStyle = "#c24141"; ctx.lineWidth = 2.5; ctx.beginPath(); ctx.moveTo(u - 8, v - 8); ctx.lineTo(u + 8, v + 8); ctx.moveTo(u + 8, v - 8); ctx.lineTo(u - 8, v + 8); ctx.stroke(); ctx.lineWidth = 1; }
    label(rid, r.x, r.y, col, -L / 2 - 6);
  }
}

function shortTask(a, r) {
  const t = a.task;
  if (!t) {  // "idle" only when waiting for the coordinator's orders
    if (a.recharge === "going") return "going to charge";
    if (a.recharge === "charging") return "charging at the staging area";
    if (a.holding) return "holding position";
    if (a.agent === false || r.failed) return "—";
    return "idle: waiting for orders";
  }
  if (t === "__link_recovery__") return "seeking coverage";
  if (t === "__return_home__") return "returning home";
  return t.replace(/^(rou|int|eme)-/, "$1 · ").replace(/_/g, " ");
}

function panel() {
  const box = document.getElementById("robots");
  box.innerHTML = Object.entries(state.robots).map(([rid, r]) => {
    const a = r.activity || {}, plat = (campus.robots[rid] || {}).platform || "";
    let tag = "", cls = "";
    if (r.failed) { tag = "failed"; cls = "failed"; }
    else if (r.link === "lost") { tag = "link lost"; cls = "lost"; }
    else if (r.moving) { tag = "moving"; cls = "moving"; }
    else tag = "still";
    const extra = [a.holding ? "holding" : "", a.recovery ? `recovery: ${a.recovery}` : "",
      a.queued ? `${a.queued} queued report(s)` : "", a.coordinator === false ? "coordinator unreachable" : "",
      a.escort ? (a.escort.approach.length ? "going to meet a person" : "guiding a person") : "",
      r.battery_pct !== undefined && r.battery_pct !== null ? `battery ${Math.round(r.battery_pct)}%` : "",
      r.localization ? r.localization : ""].filter(Boolean).join(" · ");
    return `<div class="robot"><div class="top"><span class="dot" style="background:${colors[rid]}"></span>
      <span class="id">${rid}</span><span class="line" style="margin:0">${plat}</span>
      <span class="tag ${cls}">${tag}</span></div>
      <div class="line">${shortTask(a, r)}</div>${(a.queue || []).length ? `<div class="line queue">next: ${a.queue.map(q => shortTask({ task: q }, r)).join(" → ")}</div>` : ""}${extra ? `<div class="line">${extra}</div>` : ""}</div>`;
  }).join("");
}

async function poll() {
  try {
    const r = await fetch("/sim/view/state", { cache: "no-store" });
    state = await r.json();
    for (const [rid, rb] of Object.entries(state.robots)) {
      if (!(rid in colors)) colors[rid] = COLORS[Object.keys(colors).length % COLORS.length];
      if (rb.x === undefined) continue;
      const t = trails[rid] || (trails[rid] = []), p = t[t.length - 1];
      if (!p || Math.hypot(p[0] - rb.x, p[1] - rb.y) > 0.05 * (campus ? campus.scale : 1)) t.push([rb.x, rb.y]);
      if (t.length > 600) t.shift();
    }
    document.getElementById("t").textContent = state.sim_time_label || "–";
    document.getElementById("sub").textContent =
      `${campus.campus_id}${campus.scale !== 1 ? ` · scale ${campus.scale}` : ""} · ${Object.keys(state.robots).length} robots`;
    document.getElementById("err").textContent = "";
    panel();
  } catch (e) {
    document.getElementById("err").textContent = "stack not reachable: " + e;
  }
  const now = performance.now();
  if (now - last > 1000) { document.getElementById("fps").textContent = `${Math.round(frames * 1000 / (now - last))} fps`; frames = 0; last = now; }
  setTimeout(poll, 300);
}

let drag = null;
canvas.addEventListener("mousedown", e => { drag = [e.clientX, e.clientY]; });
window.addEventListener("mouseup", () => { drag = null; });
window.addEventListener("mousemove", e => {
  if (!drag || !view) return;
  view.cx -= (e.clientX - drag[0]) / view.k; view.cy += (e.clientY - drag[1]) / view.k; drag = [e.clientX, e.clientY];
});
canvas.addEventListener("wheel", e => {
  if (!view) return; e.preventDefault();
  const rect = canvas.getBoundingClientRect(), mx = e.clientX - rect.left, my = e.clientY - rect.top;
  const wx = view.cx + (mx - W() / 2) / view.k, wy = view.cy - (my - H() / 2) / view.k;
  view.k *= Math.exp(-e.deltaY * 0.0015);
  view.cx = wx - (mx - W() / 2) / view.k; view.cy = wy + (my - H() / 2) / view.k;
}, { passive: false });
canvas.addEventListener("dblclick", fit);
window.addEventListener("resize", fit);

/*__SNAPSHOT__*/
if (window.__SNAPSHOT__) {  // ?static=1: one frame from inline data (screenshots, reports)
  campus = window.__SNAPSHOT__.campus; state = window.__SNAPSHOT__.state;
  for (const rid of Object.keys(state.robots)) colors[rid] = COLORS[Object.keys(colors).length % COLORS.length];
  document.getElementById("t").textContent = state.sim_time_label || "–";
  document.getElementById("sub").textContent = `${campus.campus_id}${campus.scale !== 1 ? ` · scale ${campus.scale}` : ""} · snapshot`;
  panel(); fit(); draw();
} else {
  fetch("/sim/view/campus").then(r => r.json()).then(c => { campus = c; fit(); poll(); requestAnimationFrame(draw); })
    .catch(e => { document.getElementById("err").textContent = "cannot load campus: " + e; });
}
</script>
</body>
</html>
"""
