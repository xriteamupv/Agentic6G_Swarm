"""Campus map for the paper: footpaths coloured by the radio-DT coverage level, the gNB site,
the staging area (the only link-recovery point), the three exposed posts with their level
and SINR, and the relocation of each post by ``reconcile_relocate`` at 4 Mbps (fleet 5).

    uv run python paper/plot_campus_map.py [--runs var/runs/n4_scarcity] [--wide]

Reads the same campus and coverage files as the simulator. The relocation viewpoints come
from the scarcity runs when present (and are written to ``paper/relocation_viewpoints.csv``);
otherwise from that CSV. Writes ``paper/fig_campus_map.pdf`` and ``.png`` (600 dpi), or
``fig_campus_map_wide.*`` with ``--wide`` (double column).
"""

import argparse
import csv
import json
import math
from itertools import pairwise
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from a6g_common.config import load_config
from a6g_common.coverage import CoverageMap, map_path
from matplotlib.collections import LineCollection
from matplotlib.lines import Line2D
from matplotlib.patches import FancyArrowPatch, Polygon, Rectangle

HERE = Path(__file__).resolve().parent
#: Coverage levels: the live view's hues (green, yellow, orange, purple), stepped so that
#: every adjacent pair stays apart for colour-vision deficiencies (validated palette).
LEVEL_COLOR = {"good": "#1B9E77", "medium": "#CFAE00", "low": "#D55E00", "lost": "#8E63B8"}
LEVELS = ("good", "medium", "low", "lost")
INK, MUTED, BUILDING = "#222222", "#666666", "#D9D9D9"
BOX = {"boxstyle": "square,pad=0.1", "fc": "white", "ec": "none", "alpha": 0.85}


def db(v: float) -> str:
    return f"{v:.1f}".replace("-", "\u2212") + " dB"


SHORT = {
    "poi_library_door": "Building 9B door",
    "poi_bike_rack": "Bike rack",
    "poi_bus_stop": "Bus stop",
}


def viewpoints(runs: Path, out_csv: Path) -> list[dict]:
    """Relocations of reconcile_relocate, 4 Mbps, fleet 5 (the seeds agree; seed 0 kept)."""
    rows: list[dict] = []
    for d in sorted(runs.glob("reconcile_relocate_c4_f5_s*")):
        dec = d / "decisions.jsonl"
        if not dec.exists():
            continue
        for line in dec.read_text().splitlines():
            x = json.loads(line)
            if (x.get("policy") or {}).get("rule") != "relocate_for_coverage":
                continue
            o = x["output"]
            rows.append(
                {
                    "seed": int(d.name.rsplit("_s", 1)[1]),
                    "task_id": o["task_id"],
                    "poi_id": o["task_id"].split("-")[-1],
                    "x": o["pose"]["x"],
                    "y": o["pose"]["y"],
                    "level": o["level"],
                    "from_level": o.get("from_level"),
                    "sinr_db": o.get("sinr_db"),
                    "from_sinr_db": o.get("from_sinr_db"),
                    "distance_m": o["distance_m"],
                }
            )
    if rows:
        with out_csv.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    elif out_csv.exists():
        with out_csv.open() as f:
            rows = [
                {
                    k: (float(v) if k in ("x", "y", "sinr_db", "from_sinr_db", "distance_m") else v)
                    for k, v in r.items()
                }
                for r in csv.DictReader(f)
            ]
    seed0 = [r for r in rows if int(r["seed"]) == min(int(q["seed"]) for q in rows)] if rows else []
    return seed0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=Path, default=Path("var/runs/n4_scarcity"))
    ap.add_argument("--wide", action="store_true", help="double-column version")
    args = ap.parse_args()
    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    campus = cfg.campus
    cmap = CoverageMap.load(map_path(cfg))
    samples = cmap.path_samples

    def level_at(x: float, y: float) -> str:
        s = min(samples, key=lambda s: (s["x"] - x) ** 2 + (s["y"] - y) ** 2)
        return s["level"]

    plt.rcParams.update(
        {"font.size": 8, "font.family": "DejaVu Sans", "axes.linewidth": 0.6, "pdf.fonttype": 42}
    )
    width_mm, height_mm = (181, 110) if args.wide else (88, 72)
    fig = plt.figure(figsize=(width_mm / 25.4, height_mm / 25.4))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_aspect("equal")
    ax.axis("off")

    # pilot area and buildings (OSM footprints; trees are left out)
    pilot = campus.pilot_area
    ax.add_patch(Polygon(pilot, closed=True, fill=False, ec=MUTED, lw=0.6, ls=(0, (3, 2))))
    heights = campus.obstacle_heights or []
    for poly, h in zip(campus.obstacles, heights, strict=False):
        if h is not None:
            ax.add_patch(Polygon(poly, closed=True, fc=BUILDING, ec="#BDBDBD", lw=0.3))

    # footpaths, each 2 m piece coloured by the nearest coverage sample; a dark casing keeps
    # the light yellow readable on white
    segs: dict[str, list] = {lv: [] for lv in LEVELS}
    for e in campus.route_graph.edges:
        pts = list(e.geometry)
        for (x0, y0), (x1, y1) in pairwise(pts):
            n = max(1, int(math.hypot(x1 - x0, y1 - y0) // 2))
            for i in range(n):
                a = (x0 + (x1 - x0) * i / n, y0 + (y1 - y0) * i / n)
                b = (x0 + (x1 - x0) * (i + 1) / n, y0 + (y1 - y0) * (i + 1) / n)
                segs[level_at((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)].append([a, b])
    lw = 2.2 if not args.wide else 2.6
    allsegs = [s for lv in LEVELS for s in segs[lv]]
    ax.add_collection(LineCollection(allsegs, colors=INK, linewidths=lw + 0.9, capstyle="round"))
    for lv in LEVELS:
        ax.add_collection(
            LineCollection(segs[lv], colors=LEVEL_COLOR[lv], linewidths=lw, capstyle="round")
        )

    # staging area: the only link-recovery point
    st = campus.staging_area
    xs, ys = [p[0] for p in st], [p[1] for p in st]
    ax.add_patch(
        Rectangle(
            (min(xs), min(ys)),
            max(xs) - min(xs),
            max(ys) - min(ys),
            fill=False,
            ec=INK,
            lw=0.9,
            ls=(0, (2, 1.2)),
            zorder=4,
        )
    )
    ax.annotate(
        "Staging\n(recovery point)",
        (min(xs), (min(ys) + max(ys)) / 2),
        xytext=(-3, 0),
        textcoords="offset points",
        ha="right",
        va="center",
        color=INK,
        bbox=BOX,
    )

    # gNB site
    site = campus.network_sites[0]
    ax.plot(site["x"], site["y"], marker="^", ms=7, mfc=INK, mec="white", mew=0.6, zorder=6)
    ax.annotate(
        "gNB (n40)",
        (site["x"], site["y"]),
        xytext=(5, -2),
        textcoords="offset points",
        ha="left",
        va="top",
        color=INK,
        bbox=BOX,
    )

    # exposed posts with level and SINR at the post, and their relocation
    vps = {r["poi_id"]: r for r in viewpoints(args.runs, HERE / "relocation_viewpoints.csv")}
    offsets = {
        "poi_library_door": (-4, 17, "right"),
        "poi_bike_rack": (-4, 6, "right"),
        "poi_bus_stop": (7, -1, "left"),
    }
    for p in campus.pois:
        if p.poi_id not in SHORT:
            continue
        s = cmap.nearest(p.pose.x, p.pose.y)
        x, y = p.pose.x, p.pose.y
        ax.plot(x, y, marker="o", ms=6, mfc=LEVEL_COLOR[s["level"]], mec=INK, mew=0.8, zorder=7)
        dx, dy, ha = offsets[p.poi_id]
        vp = vps.get(p.poi_id)
        text = f"{SHORT[p.poi_id]}\n{s['level']}, {db(s['sinr'])}"
        if vp is not None:
            text += f"\n\u2192 {vp['level']}, {db(float(vp['sinr_db']))}"
        ax.annotate(
            text,
            (x, y),
            xytext=(dx, dy),
            textcoords="offset points",
            ha=ha,
            va="bottom" if dy > 0 else "top",
            color=INK,
            bbox=BOX,
            zorder=10,
        )
        if vp is not None:
            ax.add_patch(
                FancyArrowPatch(
                    (x, y),
                    (vp["x"], vp["y"]),
                    arrowstyle="-|>",
                    mutation_scale=7,
                    lw=0.9,
                    color=INK,
                    zorder=8,
                    shrinkA=3,
                    shrinkB=1,
                )
            )
            ax.plot(
                vp["x"],
                vp["y"],
                marker="D",
                ms=4.5,
                mfc=LEVEL_COLOR[vp["level"]],
                mec=INK,
                mew=0.6,
                zorder=9,
            )

    # extent: the pilot area with a margin
    px, py = [q[0] for q in pilot], [q[1] for q in pilot]
    m = 6
    ax.set_xlim(min(px) - m, max(px) + m)
    ax.set_ylim(min(py) - m, max(py) + m)

    # scale bar and north arrow (the campus frame is east-north, heading 0)
    x0, y0 = max(px) - 58, min(py) + 4
    ax.plot([x0, x0 + 50], [y0, y0], color=INK, lw=1.2, solid_capstyle="butt")
    ax.text(x0 + 25, y0 + 3, "50 m", ha="center", va="bottom", color=INK)
    nx, ny = max(px) - 8, max(py) - 30
    ax.add_patch(
        FancyArrowPatch(
            (nx, ny), (nx, ny + 22), arrowstyle="-|>", mutation_scale=8, lw=1.0, color=INK
        )
    )
    ax.text(nx, ny + 24, "N", ha="center", va="bottom", color=INK, fontweight="bold")

    # legend: coverage levels, then the markers
    handles = [Line2D([], [], color=LEVEL_COLOR[lv], lw=2.4, label=lv) for lv in LEVELS]
    handles += [
        Line2D([], [], ls="", marker="o", mfc="white", mec=INK, ms=5, label="exposed post"),
        Line2D([], [], ls="", marker="D", mfc="white", mec=INK, ms=3.5, label="relocated post"),
    ]
    ax.legend(
        handles=handles,
        loc="lower left",
        frameon=False,
        handlelength=1.4,
        handletextpad=0.4,
        borderaxespad=0.2,
        labelspacing=0.25,
        columnspacing=0.8,
        ncol=2,
        title="Footpath coverage",
        title_fontsize=8,
        alignment="left",
    )

    stem = HERE / ("fig_campus_map_wide" if args.wide else "fig_campus_map")
    fig.savefig(stem.with_suffix(".pdf"), metadata={"CreationDate": None})  # deterministic
    fig.savefig(stem.with_suffix(".png"), dpi=600)
    print(stem.with_suffix(".pdf"), stem.with_suffix(".png"))


if __name__ == "__main__":
    main()
