"""Figure for UC2-KPI-15 (retrieval precision, SPEC A3.8, stage E6).

Reads the latest ``bench/results/e6_kpi15_fake_*.json`` (offline) and
``e6_kpi15_embedder_*.json`` (the embedder model) and writes ``bench/results/e6_kpi15.pdf``
(for the paper) and ``.png``:

    uv run python bench/plot_kpi15.py [--results bench/results] [--out bench/results]

Three panels on one 0-1 scale: (a) observations near a pose, R-normalised P@k, ranked by
distance only, by the query with the hashing embedder, and with the embedder model;
(b) statuses and notices a route reports, precision and recall, reporting every status in
force (the router before 2026-10-20) and only those that shaped the route; (c) the
interaction context retrieved at a hand-over, precision@1, with the store keeping every
entry, losing 10 % during the escort, and losing 10 % after the first robot left.
"""

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

# Validated categorical slots 1-3 (light surface #fcfcfb); ink and grid tokens.
BASE, HASHING, MODEL = "#eb6834", "#2a78d6", "#1baf7a"
SURFACE, INK, INK2, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9"


def latest(results: Path, label: str) -> dict:
    files = sorted(results.glob(f"e6_kpi15_{label}_*.json"))
    if not files:
        raise SystemExit(f"no e6_kpi15_{label}_*.json in {results}: run `a6g bench retrieval`")
    return json.loads(files[-1].read_text())


def bars(ax, groups: list[str], series: list[tuple[str, list[float], str]]) -> None:
    n = len(series)
    width = 0.8 / n
    for k, (name, vals, color) in enumerate(series):
        xs = [i + (k - (n - 1) / 2) * width for i in range(len(groups))]
        drawn = ax.bar(
            xs, vals, width, label=name, color=color, edgecolor=SURFACE, linewidth=1.5, zorder=3
        )
        for bar, v in zip(drawn, vals, strict=True):
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                v + 0.015,
                f"{v:.2f}",
                ha="center",
                va="bottom",
                fontsize=5.8,
                color=INK2,
            )
    ax.set_xticks(range(len(groups)), groups)
    ax.set_ylim(0, 1.12)
    ax.set_yticks([0, 0.25, 0.5, 0.75, 1.0])
    ax.set_facecolor(SURFACE)
    ax.tick_params(axis="both", colors=MUTED, length=0, labelsize=7.5)
    for label in ax.get_xticklabels():
        label.set_color(INK)
    ax.grid(axis="y", color=GRID, linewidth=0.6, zorder=0)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    if n > 1:  # one series: the panel title names it
        ax.legend(
            loc="lower center",
            bbox_to_anchor=(0.5, 1.02),
            frameon=False,
            fontsize=7,
            labelcolor=INK2,
            handlelength=1.0,
            ncol=1,
        )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--results", type=Path, default=Path("bench/results"))
    ap.add_argument("--out", type=Path, default=Path("bench/results"))
    args = ap.parse_args()
    offline, model = latest(args.results, "fake"), latest(args.results, "embedder")

    plt.rcParams.update({"font.size": 8, "font.family": "DejaVu Sans"})
    fig, (a, b, c) = plt.subplots(
        1,
        3,
        figsize=(7.6, 3.2),
        dpi=200,
        sharey=True,
        gridspec_kw={"width_ratios": [3.4, 2.2, 2.6]},
    )
    fig.patch.set_facecolor(SURFACE)

    obs, obs_m = offline["observations"], model["observations"]
    ks = ["1", "3", "5"]
    bars(
        a,
        [f"P@{k}" for k in ks],
        [
            ("Distance only", [obs["r_normalised_p_at_k"]["distance_only"][k] for k in ks], BASE),
            (
                "Query, hashing embedder",
                [obs["r_normalised_p_at_k"]["query"][k] for k in ks],
                HASHING,
            ),
            (
                "Query, embedder model",
                [obs_m["r_normalised_p_at_k"]["query"][k] for k in ks],
                MODEL,
            ),
        ],
    )
    a.set_title(
        f"(a) Observations near a pose\n{obs['queries']} queries", fontsize=8, color=INK, pad=42
    )
    a.set_ylabel("Precision", color=INK2)

    rc = offline["route_context"]
    bars(
        b,
        ["Precision", "Recall"],
        [
            (
                "Every status in force",
                [rc["all_in_force"]["precision"], rc["all_in_force"]["recall"]],
                BASE,
            ),
            (
                "Only what shaped the route",
                [rc["router"]["precision"], rc["router"]["recall"]],
                HASHING,
            ),
        ],
    )
    b.set_title(
        f"(b) Statuses and notices on a route\n{rc['routes']} routes",
        fontsize=8,
        color=INK,
        pad=42,
    )

    h = offline["handover"]
    conds = [
        ("store_kept", "None lost"),
        ("store_lost_10pct_during_escort", "10 % lost\nduring\nthe escort"),
        ("store_lost_10pct_after_leaving", "10 % lost\nafter B\nleft"),
    ]
    bars(
        c,
        [label for _, label in conds],
        [("Precision@1", [h[k]["precision_at_1"] for k, _ in conds], HASHING)],
    )
    c.set_title(
        f"(c) Hand-over context, P@1\n{h['store_kept']['handovers']} each, 0 leaks",
        fontsize=8,
        color=INK,
        pad=42,
    )

    fig.tight_layout()
    args.out.mkdir(parents=True, exist_ok=True)
    for ext in ("pdf", "png"):
        path = args.out / f"e6_kpi15.{ext}"
        fig.savefig(path, facecolor=SURFACE, bbox_inches="tight")
        print(path)


if __name__ == "__main__":
    main()
