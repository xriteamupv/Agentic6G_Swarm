"""``a6g network``: coverage levels from the network twin (SPEC A4.6)."""

import json
from pathlib import Path
from typing import Annotated

import typer

network_app = typer.Typer(help="Campus 5G cell, coverage levels (SPEC A4.6).", no_args_is_help=True)

LEVEL_COLOUR = {"good": "#1b9e77", "cell_edge": "#e6ab02", "degraded": "#d95f02", "lost": "#7570b3"}


def _cfg(config_dir: Path | None, source: str | None = None):
    import os

    from a6g_common.config import load_config

    env = dict(os.environ)
    if source:
        env["A6G_COVERAGE_SOURCE"] = source
    return load_config(config_dir, env=env, fleet_file="sim/fleet.yaml")


def preview_html(cfg, cmap, calibration: dict | None) -> str:
    """A self-contained SVG page: buildings, footpaths coloured by level, the velodrome loop
    with its predicted levels, the gNB site, POIs and recovery points."""
    from a6g_common import coverage as cv

    c = cfg.campus
    pts = [(s["x"], s["y"]) for s in cmap.samples] + [tuple(p) for o in c.obstacles for p in o]
    for site in c.network_sites:
        pts.append((site["x"], site["y"]))
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    x0, x1, y0, y1 = min(xs) - 10, max(xs) + 10, min(ys) - 10, max(ys) + 10
    w, h = 900, int(900 * (y1 - y0) / (x1 - x0))

    def sx(x: float) -> float:
        return round((x - x0) / (x1 - x0) * w, 1)

    def sy(y: float) -> float:
        return round(h - (y - y0) / (y1 - y0) * h, 1)

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}">'
    ]
    out.append(f'<rect width="{w}" height="{h}" fill="#fafafa"/>')
    for o in c.obstacles:
        d = " ".join(f"{sx(x)},{sy(y)}" for x, y in o)
        out.append(f'<polygon points="{d}" fill="#d9d9d9" stroke="#bdbdbd"/>')
    for sh in c.link_shadows:
        d = " ".join(f"{sx(x)},{sy(y)}" for x, y in sh)
        out.append(f'<polygon points="{d}" fill="none" stroke="#7570b3" stroke-dasharray="4 3"/>')
    step = float(cmap.meta.get("grid_step_m") or 0.0) * (c.scale or 1.0)
    cell = max(1.0, step / (x1 - x0) * w)
    for s in cmap.area_samples:  # the rest of the pilot area, shaded
        out.append(
            f'<rect x="{sx(s["x"]) - cell / 2}" y="{sy(s["y"]) - cell / 2}" width="{cell}" '
            f'height="{cell}" fill="{LEVEL_COLOUR[s["level"]]}" fill-opacity="0.3">'
            f"<title>RSRP {s['rsrp']} dBm, SINR {s['sinr']} dB, {s['level']}</title></rect>"
        )
    if calibration is not None and c.calibration_loops:
        model = cv.radio_model(c, cfg.network, calibration["offset_db"])
        for x, y in cv.loop_samples(c, cfg.network.coverage.sample_step_m):
            rsrp, los = model.rsrp(x, y)
            sinr = cv.sinr_from_rsrp(cfg.network, rsrp)
            lvl = cv.level_of(cfg.network, sinr, rsrp)
            out.append(
                f'<rect x="{sx(x) - 2.5}" y="{sy(y) - 2.5}" width="5" height="5" '
                f'fill="{LEVEL_COLOUR[lvl]}" '
                f'stroke="#000" stroke-width="0.4"><title>velodrome: RSRP {rsrp:.1f} dBm, SINR '
                f"{sinr:.1f} dB ({'LOS' if los else 'NLOS'})</title></rect>"
            )
    for p in c.pois:
        out.append(
            f'<circle cx="{sx(p.pose.x)}" cy="{sy(p.pose.y)}" r="5" fill="none" stroke="#000">'
            f"<title>{p.poi_id}</title></circle>"
        )
    for r in c.link_recovery_points:
        out.append(
            f'<rect x="{sx(r.pose.x) - 4}" y="{sy(r.pose.y) - 4}" width="8" height="8" fill="none" '
            f'stroke="#1f78b4"><title>{r.point_id}</title></rect>'
        )
    for site in c.network_sites:
        X, Y = sx(site["x"]), sy(site["y"])
        out.append(
            f'<polygon points="{X},{Y - 9} {X - 7},{Y + 6} {X + 7},{Y + 6}" fill="#e41a1c"/>'
            f'<text x="{X + 9}" y="{Y + 4}" font-size="12" font-family="sans-serif">'
            f"{site.get('name') or site['site_id']}</text>"
        )
    out.append("</svg>")
    legend = " ".join(
        f'<span style="display:inline-block;margin-right:12px"><span style="display:inline-block;'
        f'width:12px;height:12px;background:{col}"></span> {lvl}</span>'
        for lvl, col in LEVEL_COLOUR.items()
    )
    meta = json.dumps(cmap.meta, indent=1, default=str)
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Coverage preview</title></head>"
        "<body style='font-family:sans-serif;margin:16px'>"
        f"<h3>Coverage levels ({cmap.meta.get('source')}): emulated, not measured</h3>"
        f"<p>{legend} &nbsp; shaded: the pilot area outdoors (2 m cells; "
        "hover for RSRP and SINR); black-edged squares on the velodrome loop: predicted "
        "levels to compare with UPV's plots; dashed: placeholder shadows</p>"
        + "".join(out)
        + f"<pre>{meta}</pre></body></html>"
    )


@network_app.command("calibrate")
def calibrate_cmd(config_dir: Annotated[Path | None, typer.Option("--config-dir")] = None) -> None:
    """Fit the RSRP offset of the 38.901 model to UPV's velodrome plots, write the map."""
    from a6g_common import coverage as cv

    cfg = _cfg(config_dir, "model")
    cal = cv.calibrate(cfg.campus, cfg.network)
    cmap = cv.build_model_map(cfg.campus, cfg.network, cal)
    path = cv.map_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cmap.to_json(), separators=(",", ":")))
    typer.echo(json.dumps(cal, indent=1))
    if not cal["spread_ok"]:
        typer.echo(
            f"WARNING: the predicted RSRP spread on the loop ({cal['spread_db']} dB) is far "
            f"from the plotted {cal['plotted_spread_db']} dB; the radio DT map will replace it"
        )
    typer.echo(f"map: {path} ({len(cmap.samples)} samples)")


@network_app.command("import-coverage")
def import_cmd(
    rsrp: Annotated[Path, typer.Option("--rsrp", help="CSV lat,lon,cell_id,rsrp_dbm[,sinr_db]")],
    config_dir: Annotated[Path | None, typer.Option("--config-dir")] = None,
) -> None:
    """Import the RSRP map of UPV's ray-tracing radio twin, interpolated to the footpaths."""
    from a6g_common import coverage as cv
    from a6g_common.geo import Datum

    cfg = _cfg(config_dir, "radio_dt")
    d = cfg.campus.datum or {}
    datum = Datum(d["lat_deg"], d["lon_deg"], d.get("alt_m", 0.0), d.get("heading_deg", 0.0))
    scale = cfg.campus.scale or 1.0

    def geo(lat: float, lon: float) -> tuple[float, float]:
        x, y, _ = datum.to_enu(lat, lon)
        return x * scale, y * scale

    cmap = cv.build_radio_dt_map(cfg.campus, cfg.network, rsrp, geo)
    path = cv.map_path(cfg)
    path.write_text(json.dumps(cmap.to_json(), separators=(",", ":")))
    typer.echo(f"map: {path} ({len(cmap.samples)} samples, cells {cmap.meta['cells']})")
    typer.echo(json.dumps(cmap.summary(cfg.campus)["share_by_level"]))


@network_app.command("coverage")
def coverage_cmd(
    preview: Annotated[Path | None, typer.Option("--preview", help="write an HTML preview")] = None,
    source: Annotated[str | None, typer.Option("--source", help="model | radio_dt")] = None,
    config_dir: Annotated[Path | None, typer.Option("--config-dir")] = None,
) -> None:
    """Share of footpath length per level and sector; the level at each POI, the staging
    area and each link-recovery point; overlap with the old shadows."""
    from a6g_common import coverage as cv

    cfg = _cfg(config_dir, source or "model")
    cmap = cv.load_or_build(cfg)
    summary = cmap.summary(cfg.campus)
    typer.echo(f"source: {cmap.meta.get('label') or cmap.meta.get('source')}")
    typer.echo(json.dumps(summary, indent=1))
    if summary["mostly_good"]:
        typer.echo(
            "More than 80 % of the footpaths are `good`: scarcity on this campus is a matter of "
            "capacity, not coverage (the relocation variant of N4 is skipped)."
        )
    if not summary["exposed_below_good"]:
        typer.echo("Every exposed POI is `good`: the relocate_for_coverage variant does not apply.")
    if preview is not None:
        cal = cmap.meta.get("calibration")
        preview.write_text(preview_html(cfg, cmap, cal))
        typer.echo(f"preview: {preview}")
