"""Figure for UC2-KPI-10 / KVI-07 (the --kb off ablation, SPEC A3.8, stage E6).

Reads the latest ``bench/results/e6_kpi10_<label>_*.json`` of the offline run (``fake``)
and of the run with the embedder role (``fake+embedder``), and writes
``bench/results/e6_kpi10.pdf`` (for the paper) and ``.png``:

    uv run python bench/plot_kpi10.py [--results bench/results] [--out bench/results]

Three bars per measure: without the knowledge server, with it (hashing embedder,
offline) and with it (the embedder model). Routes do not depend on the embedder, so
their two server bars are equal. Place resolution "without" is the coordinator's word
overlap; the robot's alias match (0.70) is in the caption of the paper figure.
"""

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

# Validated categorical slots 1-3 (light surface #fcfcfb); ink and grid tokens.
OFF, HASHING, MODEL = "#eb6834", "#2a78d6", "#1baf7a"
SURFACE, INK, INK2, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9"


def latest(results: Path, label: str) -> dict:
    files = sorted(results.glob(f"e6_kpi10_{label}_*.json"))
    if not files:
        raise SystemExit(f"no e6_kpi10_{label}_*.json in {results}: run `a6g bench kb` first")
    return json.loads(files[-1].read_text())


def values(r: dict) -> tuple[list[float], list[float]]:
    """(server, off) per measure, in the figure's order."""
    p, d, rt = r["places"]["accuracy"], r["decomposition"]["accuracy"], r["routes"]
    server = [
        p["kb"],
        rt["as_mapped"]["compliant_rate"]["kb"],
        rt["with_steps"]["compliant_rate"]["kb"],
        d["kb"],
    ]
    off = [
        p["overlap"],
        rt["as_mapped"]["compliant_rate"]["off"],
        rt["with_steps"]["compliant_rate"]["off"],
        d["off"],
    ]
    return server, off


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--results", type=Path, default=Path("bench/results"))
    ap.add_argument("--out", type=Path, default=Path("bench/results"))
    args = ap.parse_args()
    offline, embedded = latest(args.results, "fake"), latest(args.results, "fake+embedder")
    hashing, off = values(offline)
    model, _ = values(embedded)
    groups = [
        f"Place resolution\n({offline['places']['items']} phrases)",
        f"Route compliance\ncampus as mapped\n({offline['routes']['as_mapped']['trips']} trips)",
        f"Route compliance\nwith G11 steps\n({offline['routes']['with_steps']['trips']} trips)",
        f"Decomposition\n({offline['decomposition']['goals']} orders)",
    ]

    plt.rcParams.update({"font.size": 9, "font.family": "DejaVu Sans"})
    fig, ax = plt.subplots(figsize=(7.0, 3.4), dpi=200)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    width = 0.26
    series = [
        ("Without the knowledge server", off, OFF),
        ("With it, hashing embedder (offline)", hashing, HASHING),
        ("With it, embedder model", model, MODEL),
    ]
    for k, (name, vals, color) in enumerate(series):
        xs = [i + (k - 1) * width for i in range(len(groups))]
        bars = ax.bar(
            xs, vals, width, label=name, color=color, edgecolor=SURFACE, linewidth=1.5, zorder=3
        )
        for bar, v in zip(bars, vals, strict=True):
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                v + 0.015,
                f"{v:.2f}",
                ha="center",
                va="bottom",
                fontsize=7,
                color=INK2,
            )
    ax.set_xticks(range(len(groups)), groups, color=INK)
    ax.set_ylim(0, 1.12)
    ax.set_yticks([0, 0.25, 0.5, 0.75, 1.0])
    ax.set_ylabel("Share correct / compliant", color=INK2)
    ax.tick_params(axis="both", colors=MUTED, length=0)
    for label in ax.get_xticklabels():
        label.set_color(INK)
    ax.grid(axis="y", color=GRID, linewidth=0.6, zorder=0)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.legend(
        loc="upper center",
        bbox_to_anchor=(0.5, 1.16),
        ncol=3,
        frameon=False,
        fontsize=8,
        labelcolor=INK2,
        handlelength=1.2,
    )
    fig.tight_layout()
    args.out.mkdir(parents=True, exist_ok=True)
    for ext in ("pdf", "png"):
        path = args.out / f"e6_kpi10.{ext}"
        fig.savefig(path, facecolor=SURFACE, bbox_inches="tight")
        print(path)


if __name__ == "__main__":
    main()
