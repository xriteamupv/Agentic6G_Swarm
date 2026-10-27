"""UC2-KPI-10 / KVI-07 bench (SPEC A3.8, E6): the --kb off ablation runs unattended with
fake models, deterministically, on paired items."""

import asyncio

from a6g_common.config import load_config
from a6g_common.schemas import Posture
from a6g_coordinator.decompose import _target_key, decompose_goal, overlap_place
from a6g_devstack import bench_kb as B


def _cfg():
    return load_config("config", env={}, fleet_file="sim/fleet.yaml")


def test_the_ablation_reports_every_measure_and_is_deterministic() -> None:
    cfg = _cfg()
    first = asyncio.run(B.run(cfg, seeds=60))
    again = asyncio.run(B.run(cfg, seeds=60))
    for r in (first, again):
        r.pop("stamp")
    assert first == again  # same seeds, same items: same numbers
    assert first["fidelity"] == "offline, deterministic"
    places, routes, decomp = first["places"], first["routes"], first["decomposition"]
    assert places["items"] >= 60 and places["accuracy"]["kb"] > places["accuracy"]["overlap"]
    for r in routes.values():
        assert r["compliant_rate"]["kb"] == 1.0  # the server's routes always comply
        assert r["compliant_rate"]["off"] < 1.0
    assert routes["with_steps"]["off_violation_kinds"]  # the shortcut steps are taken off
    assert decomp["goals"] >= 40 and decomp["accuracy"]["kb"] >= decomp["accuracy"]["off"]
    assert "UC2-KPI-10" in B.summary(first)


def test_word_overlap_is_the_coordinators_fallback() -> None:
    campus = _cfg().campus
    assert overlap_place("the bus stop", campus) == ("poi", "poi_bus_stop", True)
    assert overlap_place("staging", campus) == ("place", "lrp_staging", True)
    assert overlap_place("the bus station", campus)[2] is False  # its closest name
    assert overlap_place("the gym", campus) is None


def test_an_order_to_an_unnamed_place_is_a_trip_not_a_patrol() -> None:
    """Found by the bench: "send a robot to the swimming pool" planned a campus patrol,
    and an accented destination ("puerta del edificio 8K") was cut short."""
    campus = _cfg().campus

    def plan(text: str, posture: Posture = Posture.ROUTINE) -> list:
        tasks, *_ = asyncio.run(decompose_goal(text, posture, campus, None))
        return [(t.kind.value, (_target_key(t.target, campus) or ("", ""))[1]) for t in tasks]

    assert plan("Send a robot to the swimming pool") == []  # unknown: nothing planned
    assert plan("Send a robot to the bus station") == [("go_to", "poi_bus_stop")]
    assert plan("Go to the staging") == [("go_to", "lrp_staging")]
    assert plan("Escort the visitor to the puerta del edificio 8K", Posture.INTERACTIVE) != [
        ("escort", "")
    ]
    assert plan("Patrol the campus") == [("survey_sector", s.sector_id) for s in campus.sectors]
