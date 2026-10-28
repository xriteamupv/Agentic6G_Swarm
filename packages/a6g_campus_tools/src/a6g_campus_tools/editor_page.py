# ruff: noqa: E501  (the embedded HTML/JS page is kept as written)
"""The page of ``a6g campus edit`` (self-contained: no external assets)."""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Campus zone editor</title>
<style>
  :root {
    --bg: #f6f7f5; --panel: #ffffff; --ink: #1d2320; --muted: #66706b; --line: #d8ddd9;
    --ground: #eceee9; --building: #c4beb3; --building-edge: #9a9386; --pilot: #d9a400;
    --shadow: 200,60,60; --path: #9b7b52; --accent: #2f6fb0; --warn: #c24141;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #141816; --panel: #1c211f; --ink: #e6ebe8; --muted: #9aa5a0; --line: #2d3431;
      --ground: #1a1f1d; --building: #4a4741; --building-edge: #6b665d; --pilot: #e0b021;
      --shadow: 230,90,90; --path: #b8946a; --accent: #6aa5e6; --warn: #e86a6a;
    }
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; background: var(--bg); color: var(--ink);
    font: 14px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; }
  .app { display: grid; grid-template-columns: 1fr 340px; height: 100%; }
  @media (max-width: 860px) { .app { grid-template-columns: 1fr; grid-template-rows: 62vh auto; } }
  .map { position: relative; overflow: hidden; }
  canvas { display: block; width: 100%; height: 100%; }
  .hud { position: absolute; left: 12px; top: 10px; right: 12px; display: flex; flex-wrap: wrap; gap: 6px; }
  .tool { border: 1px solid var(--line); background: var(--panel); color: var(--ink); border-radius: 8px;
    padding: 6px 10px; font: inherit; font-size: 13px; cursor: pointer; display: inline-flex; align-items: center; gap: 6px; }
  .tool.on { outline: 2px solid var(--accent); outline-offset: 0; }
  .sw { width: 12px; height: 12px; border-radius: 3px; display: inline-block; }
  .tip { position: absolute; left: 12px; bottom: 10px; background: var(--panel); border: 1px solid var(--line);
    border-radius: 8px; padding: 6px 10px; font-size: 12px; color: var(--muted); max-width: calc(100% - 24px); }
  aside { background: var(--panel); border-left: 1px solid var(--line); overflow-y: auto; padding: 14px 16px; }
  h1 { font-size: 15px; margin: 0 0 2px; }
  h2 { font-size: 13px; margin: 16px 0 6px; color: var(--muted); font-weight: 600; text-transform: uppercase; letter-spacing: .04em; }
  .sub { color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }
  .zone { border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; margin-bottom: 8px; }
  .zone .top { display: flex; align-items: center; gap: 8px; font-weight: 600; }
  .zone .num { color: var(--muted); font-size: 12px; margin-top: 2px; font-variant-numeric: tabular-nums; }
  .bar { height: 6px; border-radius: 3px; background: var(--line); margin-top: 6px; overflow: hidden; }
  .bar > span { display: block; height: 100%; }
  .btns { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
  button.act { border: 1px solid var(--line); background: var(--panel); color: var(--ink); border-radius: 8px;
    padding: 7px 12px; font: inherit; cursor: pointer; }
  button.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
  button:disabled { opacity: .5; cursor: default; }
  .prob { color: var(--warn); font-size: 12px; margin: 3px 0; }
  .ok { color: #2f9a5b; font-size: 12px; }
  .msg { font-size: 12px; margin-top: 8px; min-height: 1.4em; }
  label.chk { font-size: 12px; color: var(--muted); display: flex; gap: 6px; align-items: center; margin-top: 6px; }
  ul.help { padding-left: 18px; margin: 4px 0; color: var(--muted); font-size: 12px; }
</style>
</head>
<body>
<div class="app">
  <div class="map">
    <canvas id="c" aria-label="Campus map: footpaths by zone and draggable points"></canvas>
    <div class="hud" id="tools"></div>
    <div class="tip" id="tip">Pick a brush, then click or drag along footpaths. Drag a point to move it. Wheel to zoom, drag empty ground to pan.</div>
  </div>
  <aside>
    <h1>Campus zone editor</h1>
    <div class="sub" id="file">loading…</div>
    <div class="btns">
      <button class="act primary" id="save" disabled>Save to campus file</button>
      <button class="act" id="revert" disabled>Discard changes</button>
      <button class="act" id="auto">Reset to automatic split</button>
    </div>
    <div class="msg" id="msg"></div>
    <h2>Zones</h2>
    <div id="zones"></div>
    <label class="chk"><input type="checkbox" id="routes" checked> show survey routes</label>
    <label class="chk"><input type="checkbox" id="ids"> show path ids</label>
    <h2>5G coverage shadows</h2>
    <div id="shadows"></div>
    <div class="btns" style="margin-top:4px"><button class="act" id="addshadow">Add a shadow</button></div>
    <h2>Checks</h2>
    <div id="problems"></div>
    <h2>How it works</h2>
    <ul class="help">
      <li>A footpath painted with a zone brush is fixed to that zone; the automatic split shares out the rest, balanced by length.</li>
      <li>Thick solid lines are fixed by you; lighter lines are the automatic split.</li>
      <li><b>Not patrolled</b>: robots may still walk it, no zone surveys it.</li>
      <li>POIs (circles), staging (square) and link recovery points (triangles) snap to the nearest footpath when dropped.</li>
      <li><b>5G shadows</b> brush: drag corners or the whole area, click a small circle on an edge to add a corner, right-click a corner to remove it. <i>Link lost</i> cuts the robot off; <i>degraded</i> slows its uplink.</li>
      <li>Nothing is written until you save; the previous file is kept as <code>.bak</code>.</li>
    </ul>
  </aside>
</div>
<script>
const ZONES = ["46,125,180", "150,80,190", "27,154,160", "200,120,30", "90,140,60"];
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const canvas = document.getElementById("c"), ctx = canvas.getContext("2d");
let S = null, view = null, brush = null, busy = false, drag = null, hover = null, painted = new Set();
const zoneIdx = (z) => S.zones.findIndex(x => x.id === z);
const zcol = (z, a = 1) => z && zoneIdx(z) >= 0 ? `rgba(${ZONES[zoneIdx(z) % ZONES.length]},${a})` : null;

const W = () => canvas.clientWidth, H = () => canvas.clientHeight;
const toPx = (x, y) => [W() / 2 + (x - view.cx) * view.k, H() / 2 - (y - view.cy) * view.k];
const toW = (u, v) => [view.cx + (u - W() / 2) / view.k, view.cy - (v - H() / 2) / view.k];

function fit() {
  const pts = S.pilot_area, xs = pts.map(p => p[0]), ys = pts.map(p => p[1]);
  const k = Math.min((W() - 60) / (Math.max(...xs) - Math.min(...xs)), (H() - 90) / (Math.max(...ys) - Math.min(...ys)));
  view = { k, cx: (Math.max(...xs) + Math.min(...xs)) / 2, cy: (Math.max(...ys) + Math.min(...ys)) / 2 };
}

function path(points, close) {
  ctx.beginPath();
  points.forEach((p, i) => { const [u, v] = toPx(p[0], p[1]); i ? ctx.lineTo(u, v) : ctx.moveTo(u, v); });
  if (close) ctx.closePath();
}

function label(text, x, y, color, dy) {
  const [u, v] = toPx(x, y);
  ctx.font = "11px system-ui, sans-serif"; ctx.textAlign = "center";
  ctx.lineWidth = 3; ctx.strokeStyle = css("--bg"); ctx.strokeText(text, u, v + dy);
  ctx.fillStyle = color; ctx.fillText(text, u, v + dy); ctx.lineWidth = 1;
}

function draw() {
  const dpr = window.devicePixelRatio || 1;
  if (canvas.width !== W() * dpr || canvas.height !== H() * dpr) { canvas.width = W() * dpr; canvas.height = H() * dpr; }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = css("--bg"); ctx.fillRect(0, 0, W(), H());
  if (!S || !view) return requestAnimationFrame(draw);
  path(S.pilot_area, true); ctx.fillStyle = css("--ground"); ctx.fill();
  const shadowMode = brush === "shadow";
  for (const sh of S.shadows) {
    const poly = drag && drag.shadow === sh.id ? drag.poly : sh.polygon;
    const rgb = sh.link_quality === "degraded" ? "217,113,28" : css("--shadow");
    path(poly, true); ctx.fillStyle = `rgba(${rgb},${shadowMode ? .22 : .12})`; ctx.fill();
    ctx.setLineDash([5, 4]); ctx.strokeStyle = `rgb(${rgb})`; ctx.lineWidth = shadowMode ? 2 : 1; ctx.stroke(); ctx.setLineDash([]); ctx.lineWidth = 1;
    const c = poly.reduce((a, p) => [a[0] + p[0] / poly.length, a[1] + p[1] / poly.length], [0, 0]);
    label(`${sh.id} (${sh.link_quality})`, c[0], c[1], `rgb(${rgb})`, 4);
  }
  for (const b of S.buildings) { path(b, true); ctx.fillStyle = css("--building"); ctx.fill(); ctx.strokeStyle = css("--building-edge"); ctx.stroke(); }
  for (const st of S.sites || []) {  // the gNB site (visual only)
    const [u, v] = toPx(st.x, st.y); ctx.fillStyle = "#e41a1c";
    ctx.beginPath(); ctx.moveTo(u, v - 10); ctx.lineTo(u - 8, v + 7); ctx.lineTo(u + 8, v + 7); ctx.closePath(); ctx.fill();
    label(st.name, st.x, st.y, "#e41a1c", 20);
  }
  path(S.pilot_area, true); ctx.setLineDash([8, 5]); ctx.lineWidth = 2; ctx.strokeStyle = css("--pilot"); ctx.stroke(); ctx.setLineDash([]); ctx.lineWidth = 1;
  const showIds = document.getElementById("ids").checked;
  for (const e of S.edges) {
    const manual = e.manual, z = e.zone;
    const isHover = hover && hover.base === e.base;
    if (e.source === "connector") { path(e.pts); ctx.strokeStyle = css("--path"); ctx.setLineDash([2, 3]); ctx.stroke(); ctx.setLineDash([]); continue; }
    if (manual === "none" || (!z && e.source !== "open")) {
      path(e.pts); ctx.strokeStyle = css("--path"); ctx.lineWidth = 3; ctx.setLineDash([6, 5]); ctx.stroke(); ctx.setLineDash([]);
    } else if (z) {
      path(e.pts); ctx.strokeStyle = zcol(z, manual ? 0.95 : 0.45); ctx.lineWidth = manual ? 9 : 7; ctx.lineCap = "round"; ctx.stroke();
      path(e.pts); ctx.strokeStyle = css("--path"); ctx.lineWidth = 1.5; ctx.stroke();
    }
    if (isHover) { path(e.pts); ctx.strokeStyle = css("--ink"); ctx.lineWidth = 2; ctx.setLineDash([4, 3]); ctx.stroke(); ctx.setLineDash([]); }
    ctx.lineWidth = 1; ctx.lineCap = "butt";
    if (showIds) { const m = e.pts[Math.floor(e.pts.length / 2)]; label(e.base, m[0], m[1], css("--muted"), -6); }
  }
  if (document.getElementById("routes").checked) {
    for (const z of S.zones) {
      if (z.waypoints.length < 2) continue;
      path(z.waypoints); ctx.strokeStyle = zcol(z.id, 1); ctx.lineWidth = 1.5; ctx.setLineDash([3, 4]); ctx.stroke(); ctx.setLineDash([]); ctx.lineWidth = 1;
      const [u, v] = toPx(...z.waypoints[0]); ctx.fillStyle = zcol(z.id, 1); ctx.beginPath(); ctx.arc(u, v, 4, 0, 7); ctx.fill();
    }
  }
  for (const p of S.points) {
    const pos = drag && drag.point === p ? drag.at : [p.x, p.y];
    const [u, v] = toPx(pos[0], pos[1]);
    const col = p.off_m > S.corridor_m ? css("--warn") : p.kind === "poi" ? css("--accent") : "#2f9a5b";
    ctx.fillStyle = col; ctx.strokeStyle = css("--panel"); ctx.lineWidth = 2; ctx.beginPath();
    if (p.kind === "staging") ctx.rect(u - 6, v - 6, 12, 12);
    else if (p.kind === "recovery") { ctx.moveTo(u, v - 8); ctx.lineTo(u - 7, v + 5); ctx.lineTo(u + 7, v + 5); ctx.closePath(); }
    else ctx.arc(u, v, 6, 0, 7);
    ctx.fill(); ctx.stroke(); ctx.lineWidth = 1;
    label(p.name, pos[0], pos[1], col, p.kind === "staging" ? 20 : -11);
  }
  if (shadowMode) for (const sh of S.shadows) {
    const poly = drag && drag.shadow === sh.id ? drag.poly : sh.polygon;
    poly.forEach((p, i) => {
      const [u, v] = toPx(p[0], p[1]), q = poly[(i + 1) % poly.length], [mu, mv] = toPx((p[0] + q[0]) / 2, (p[1] + q[1]) / 2);
      ctx.fillStyle = css("--panel"); ctx.strokeStyle = css("--ink"); ctx.lineWidth = 1.5;
      ctx.fillRect(u - 5, v - 5, 10, 10); ctx.strokeRect(u - 5, v - 5, 10, 10);
      ctx.beginPath(); ctx.arc(mu, mv, 4, 0, 7); ctx.fill(); ctx.stroke(); ctx.lineWidth = 1;
    });
  }
  requestAnimationFrame(draw);
}

function inPoly(x, y, poly) {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i], [xj, yj] = poly[j];
    if ((yi > y) !== (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}
function shadowHit(u, v) {
  for (const sh of S.shadows) {
    for (let i = 0; i < sh.polygon.length; i++) {
      const p = sh.polygon[i], q = sh.polygon[(i + 1) % sh.polygon.length];
      const [pu, pv] = toPx(p[0], p[1]);
      if (Math.hypot(pu - u, pv - v) < 8) return { sh, vertex: i };
      const [mu, mv] = toPx((p[0] + q[0]) / 2, (p[1] + q[1]) / 2);
      if (Math.hypot(mu - u, mv - v) < 7) return { sh, insert: i + 1, at: [(p[0] + q[0]) / 2, (p[1] + q[1]) / 2] };
    }
  }
  const [x, y] = toW(u, v);
  const sh = S.shadows.find(s => inPoly(x, y, s.polygon));
  return sh ? { sh, body: true } : null;
}

function segDist(px, py, ax, ay, bx, by) {
  const dx = bx - ax, dy = by - ay, L = dx * dx + dy * dy;
  const t = L ? Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / L)) : 0;
  return Math.hypot(px - ax - t * dx, py - ay - t * dy);
}
function edgeAt(u, v) {
  let best = null, bd = 9;
  for (const e of S.edges) {
    if (e.source === "connector" || e.source === "open") continue;
    for (let i = 1; i < e.pts.length; i++) {
      const [au, av] = toPx(...e.pts[i - 1]), [bu, bv] = toPx(...e.pts[i]);
      const d = segDist(u, v, au, av, bu, bv);
      if (d < bd) { bd = d; best = e; }
    }
  }
  return best;
}
function pointAt(u, v) {
  return S.points.find(p => { const [pu, pv] = toPx(p.x, p.y); return Math.hypot(pu - u, pv - v) < 10; });
}

async function call(url, body) {
  busy = true; document.getElementById("msg").textContent = "updating…";
  try {
    const r = await fetch(url, { method: body === undefined ? "GET" : "POST", headers: { "content-type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) });
    const data = await r.json();
    if (!r.ok) { document.getElementById("msg").innerHTML = `<span class="prob">${data.error}</span>`; return; }
    S = data; if (!view) fit(); render();
    document.getElementById("msg").innerHTML = data.saved ? `<span class="ok">Saved. Previous file: ${data.saved}</span>` : "";
  } catch (e) { document.getElementById("msg").innerHTML = `<span class="prob">editor not reachable: ${e}</span>`; }
  finally { busy = false; }
}

function render() {
  document.getElementById("file").textContent = `${S.campus_id} · ${S.file}${S.dirty ? " · unsaved changes" : ""}`;
  document.getElementById("save").disabled = !S.dirty || S.problems.length > 0;
  document.getElementById("revert").disabled = !S.dirty;
  const max = Math.max(...S.zones.map(z => z.length_m), 1);
  document.getElementById("zones").innerHTML = S.zones.map(z => `
    <div class="zone"><div class="top"><span class="sw" style="background:${zcol(z.id)}"></span>${z.name || z.id}<span class="sub" style="margin-left:auto">${z.id}</span></div>
    <div class="num">paths ${z.length_m} m · survey ${z.survey_m} m · ${z.waypoints.length} waypoints · Go2 lap ≈ ${z.battery_pct}% battery</div>
    <div class="bar"><span style="width:${100 * z.length_m / max}%;background:${zcol(z.id)}"></span></div></div>`).join("");
  document.getElementById("shadows").innerHTML = S.shadows.map(sh => `
    <div class="zone"><div class="top">${sh.id}<span style="margin-left:auto"></span>
      <select data-q="${sh.id}"><option value="lost"${sh.link_quality === "lost" ? " selected" : ""}>link lost</option>
      <option value="degraded"${sh.link_quality === "degraded" ? " selected" : ""}>degraded</option></select>
      <button class="act" data-del="${sh.id}" style="padding:3px 8px">Delete</button></div>
      <div class="num">${sh.polygon.length} corners</div></div>`).join("") || `<div class="sub">No shadows.</div>`;
  document.querySelectorAll("[data-q]").forEach(el => el.onchange = () => call("/api/shadow/quality", { id: el.dataset.q, link_quality: el.value }));
  document.querySelectorAll("[data-del]").forEach(el => el.onclick = () => { if (confirm(`Delete ${el.dataset.del}?`)) call("/api/shadow/delete", { id: el.dataset.del }); });
  document.getElementById("problems").innerHTML = S.problems.length
    ? S.problems.map(p => `<div class="prob">• ${p}</div>`).join("") + `<div class="sub">Fix these to save.</div>`
    : `<div class="ok">All checks pass.</div>`;
  const tools = [...S.zones.map(z => ({ id: z.id, name: z.name || z.id, col: zcol(z.id) })),
    { id: "none", name: "Not patrolled", col: css("--path") }, { id: "auto", name: "Automatic", col: "transparent" },
    { id: "shadow", name: "5G shadows", col: `rgba(${css("--shadow")},.5)` }];
  document.getElementById("tools").innerHTML = tools.map(t =>
    `<button class="tool${brush === t.id ? " on" : ""}" data-b="${t.id}"><span class="sw" style="background:${t.col};border:1px solid var(--line)"></span>${t.name}</button>`).join("");
  document.querySelectorAll(".tool").forEach(b => b.onclick = () => { brush = brush === b.dataset.b ? null : b.dataset.b; render(); });
}

canvas.addEventListener("mousedown", async (ev) => {
  if (!S || busy) return;
  const r = canvas.getBoundingClientRect(), u = ev.clientX - r.left, v = ev.clientY - r.top;
  if (brush === "shadow") {
    const h = shadowHit(u, v);
    if (h) {
      const poly = h.sh.polygon.map(p => [...p]);
      if (h.insert !== undefined) { poly.splice(h.insert, 0, [...h.at]); drag = { shadow: h.sh.id, poly, vertex: h.insert }; }
      else if (h.vertex !== undefined) drag = { shadow: h.sh.id, poly, vertex: h.vertex };
      else drag = { shadow: h.sh.id, poly, body: toW(u, v) };
      return;
    }
    drag = { pan: [ev.clientX, ev.clientY] };
    return;
  }
  const p = pointAt(u, v);
  if (p) { drag = { point: p, at: [p.x, p.y] }; return; }
  const e = brush && edgeAt(u, v);
  if (e) { drag = { paint: true }; painted = new Set([e.base]); await call("/api/assign", { edge: e.id, zone: brush }); return; }
  drag = { pan: [ev.clientX, ev.clientY] };
});
window.addEventListener("mousemove", async (ev) => {
  if (!S || !view) return;
  const r = canvas.getBoundingClientRect(), u = ev.clientX - r.left, v = ev.clientY - r.top;
  if (!drag && brush === "shadow") {
    const h = shadowHit(u, v);
    hover = null;
    document.getElementById("tip").textContent = !h ? "5G shadows: drag a corner or the inside of a shadow; click a small circle to add a corner; right-click a corner to remove it."
      : h.vertex !== undefined ? `${h.sh.id}: drag this corner · right-click to remove it`
      : h.insert !== undefined ? `${h.sh.id}: drag to add a corner here`
      : `${h.sh.id} (${h.sh.link_quality}): drag to move the whole area`;
    canvas.style.cursor = h ? (h.body ? "move" : "pointer") : "default";
    return;
  }
  if (!drag) {
    hover = brush ? edgeAt(u, v) : null;
    const p = pointAt(u, v), e = hover;
    document.getElementById("tip").textContent = p ? `${p.name} (${p.kind}) · ${p.off_m} m from a footpath · drag to move`
      : e ? `path ${e.base} · ${e.manual ? (e.manual === "none" ? "not patrolled" : "fixed to " + e.manual) : (e.zone ? "automatic: " + e.zone : "not patrolled")} · click to paint with “${brush}”`
      : "Pick a brush, then click or drag along footpaths. Drag a point to move it. Wheel to zoom, drag empty ground to pan.";
    canvas.style.cursor = p ? "grab" : e ? "crosshair" : "default";
    return;
  }
  if (drag.pan) { view.cx -= (ev.clientX - drag.pan[0]) / view.k; view.cy += (ev.clientY - drag.pan[1]) / view.k; drag.pan = [ev.clientX, ev.clientY]; }
  else if (drag.point) drag.at = toW(u, v);
  else if (drag.shadow) {
    const w = toW(u, v);
    if (drag.vertex !== undefined) drag.poly[drag.vertex] = w;
    else { const dx = w[0] - drag.body[0], dy = w[1] - drag.body[1]; drag.poly = drag.poly.map(p => [p[0] + dx, p[1] + dy]); drag.body = w; }
  }
  else if (drag.paint && !busy) {
    const e = edgeAt(u, v);
    if (e && !painted.has(e.base)) { painted.add(e.base); await call("/api/assign", { edge: e.id, zone: brush }); }
  }
});
window.addEventListener("mouseup", async () => {
  const d = drag; drag = null;
  if (d && d.point) await call("/api/move", { kind: d.point.kind, id: d.point.id, x: d.at[0], y: d.at[1] });
  if (d && d.shadow) await call("/api/shadow", { id: d.shadow, polygon: d.poly });
});
canvas.addEventListener("wheel", (ev) => {
  if (!view) return; ev.preventDefault();
  const r = canvas.getBoundingClientRect(), mx = ev.clientX - r.left, my = ev.clientY - r.top;
  const [wx, wy] = toW(mx, my); view.k *= Math.exp(-ev.deltaY * 0.0015);
  view.cx = wx - (mx - W() / 2) / view.k; view.cy = wy + (my - H() / 2) / view.k;
}, { passive: false });
canvas.addEventListener("dblclick", fit);
canvas.addEventListener("contextmenu", async (ev) => {
  if (brush !== "shadow" || !S) return;
  const r = canvas.getBoundingClientRect(), h = shadowHit(ev.clientX - r.left, ev.clientY - r.top);
  if (!h || h.vertex === undefined) return;
  ev.preventDefault();
  if (h.sh.polygon.length <= 3) { document.getElementById("msg").innerHTML = `<span class="prob">a shadow needs at least 3 corners</span>`; return; }
  const poly = h.sh.polygon.filter((_, i) => i !== h.vertex);
  await call("/api/shadow", { id: h.sh.id, polygon: poly });
});
function addShadow() {
  const n = S.shadows.length + 1, ids = new Set(S.shadows.map(s => s.id));
  let k = n; while (ids.has(`shadow_${k}`)) k++;
  const id = prompt("Id of the new shadow (lower case, digits, _):", `shadow_${k}`);
  if (!id) return;
  const h = 15, c = [view.cx, view.cy];
  call("/api/shadow", { id, polygon: [[c[0] - h, c[1] - h], [c[0] + h, c[1] - h], [c[0] + h, c[1] + h], [c[0] - h, c[1] + h]] });
  brush = "shadow"; render();
}
window.addEventListener("resize", () => S && fit());
document.getElementById("save").onclick = () => call("/api/save", {});
document.getElementById("addshadow").onclick = () => S && addShadow();
document.getElementById("revert").onclick = () => call("/api/revert", {});
document.getElementById("auto").onclick = () => { if (confirm("Forget every fixed and excluded path and go back to the automatic split?")) call("/api/auto", {}); };
window.addEventListener("beforeunload", (ev) => { if (S && S.dirty) { ev.preventDefault(); ev.returnValue = ""; } });
call("/api/state"); requestAnimationFrame(draw);
</script>
</body>
</html>
"""
