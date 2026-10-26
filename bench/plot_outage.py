"""Draft plot of the N1 outage sweep (SPEC A4.4), for sanity only: the paper figure is drawn
from ``paper/fig_outage.csv``.

    uv run python bench/plot_outage.py [--csv paper/fig_outage.csv] [--out bench/results]

Per fleet size: share of missions completed and median time to resume against the
coordinator outage length, one line per configuration (P, B2, B1, B0).
"""

import argparse
import csv
import statistics
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

COLOURS = {"P": "#2a78d6", "B2": "#1baf7a", "B1": "#eb6834", "B0": "#898781"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", type=Path, default=Path("paper/fig_outage.csv"))
    ap.add_argument("--out", type=Path, default=Path("bench/results"))
    a = ap.parse_args()
    rows = list(csv.DictReader(a.csv.open()))
    fleets = sorted({int(r["fleet"]) for r in rows})
    order = ["60", "180", "300", "600", "inf"]
    fig, axes = plt.subplots(2, len(fleets), figsize=(4 * len(fleets), 6), squeeze=False)
    for j, f in enumerate(fleets):
        for cfg, col in COLOURS.items():
            cell = defaultdict(list)
            for r in rows:
                if int(r["fleet"]) == f and r["config"] == cfg:
                    cell[r["outage_s"]].append(r)
            xs = [o for o in order if o in cell]
            done = [sum(int(r["completed"]) for r in cell[o]) / len(cell[o]) for o in xs]
            resume = [
                statistics.median(
                    [float(r["time_to_resume_s"]) for r in cell[o] if r["time_to_resume_s"]]
                    or [float("nan")]
                )
                for o in xs
            ]
            axes[0][j].plot(xs, done, marker="o", color=col, label=cfg)
            axes[1][j].plot(xs, resume, marker="o", color=col, label=cfg)
        axes[0][j].set_title(f"fleet {f}")
        axes[0][j].set_ylabel("missions completed")
        axes[1][j].set_ylabel("time to resume (s, median)")
        axes[1][j].set_xlabel("coordinator outage (s)")
    axes[0][0].legend()
    fig.tight_layout()
    a.out.mkdir(parents=True, exist_ok=True)
    fig.savefig(a.out / "n1_outage_draft.png", dpi=120)
    print(a.out / "n1_outage_draft.png")


if __name__ == "__main__":
    main()
