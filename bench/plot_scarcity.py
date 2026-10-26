"""Draft plot of the G13 scarcity sweep (SPEC A4.7), for sanity only: the paper figure is
drawn from ``paper/fig_scarcity.csv``.

    uv run python bench/plot_scarcity.py [--csv paper/fig_scarcity.csv] [--out bench/results]

Per fleet size: posts with usable video and with usable sensing against the cell's uplink
capacity, for ``reconcile`` and ``reconcile_off``.
"""

import argparse
import csv
import statistics
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

COLOURS = {"reconcile": "#2a78d6", "reconcile_off": "#eb6834", "reconcile_relocate": "#1baf7a"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", type=Path, default=Path("paper/fig_scarcity.csv"))
    ap.add_argument("--out", type=Path, default=Path("bench/results"))
    a = ap.parse_args()
    rows = list(csv.DictReader(a.csv.open()))
    fleets = sorted({int(r["fleet"]) for r in rows})
    fig, axes = plt.subplots(1, len(fleets), figsize=(5 * len(fleets), 3.5), squeeze=False)
    for j, f in enumerate(fleets):
        ax = axes[0][j]
        for v, col in COLOURS.items():
            cell = defaultdict(list)
            for r in rows:
                if int(r["fleet"]) == f and r["variant"] == v:
                    cell[float(r["capacity_mbps"])].append(r)
            xs = sorted(cell)
            if not xs:
                continue
            video = [statistics.median(float(r["posts_video_usable"]) for r in cell[x]) for x in xs]
            sensing = [
                statistics.median(float(r["posts_sensing_usable"]) for r in cell[x]) for x in xs
            ]
            ax.plot(xs, video, marker="o", color=col, label=f"{v}: video usable")
            ax.plot(
                xs, sensing, marker="s", linestyle="--", color=col, label=f"{v}: sensing usable"
            )
        ax.set_xscale("log", base=2)
        ax.set_title(f"fleet {f}")
        ax.set_xlabel("uplink capacity of the cell (Mbps)")
        ax.set_ylabel("posts (median)")
    axes[0][0].legend(fontsize=7)
    fig.tight_layout()
    a.out.mkdir(parents=True, exist_ok=True)
    fig.savefig(a.out / "n4_scarcity_draft.png", dpi=120)
    print(a.out / "n4_scarcity_draft.png")


if __name__ == "__main__":
    main()
