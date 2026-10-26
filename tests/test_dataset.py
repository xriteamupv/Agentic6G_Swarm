"""DS09 export (SPEC A3.8): JSON with a schema; interaction content never reaches it
(rule 28); images are excluded by default and, with ``--with-images``, only those
without a person in view are exported."""

import json
from pathlib import Path

from a6g_common.config import load_config
from a6g_devstack.dataset import export
from a6g_observability.decision_log import DecisionLog, make_decision


def _run(tmp_path: Path) -> Path:
    run = tmp_path / "run"
    (run / "images").mkdir(parents=True)
    log = DecisionLog(run / "decisions.jsonl")
    for oid, person in (("o_bag", False), ("o_person", True)):
        (run / "images" / f"{oid}.jpg").write_bytes(b"jpg")
        log.record(
            make_decision(
                "go2_01.agent",
                "escalate",
                mission_id="m1",
                output={
                    "obs_id": oid,
                    "label": "unattended backpack" if not person else "someone sitting",
                    "confidence": 0.8,
                    "evidence": {"image_uri": f"file://images/{oid}.jpg", "image_sha256": "x"},
                    "person_present": person,
                },
            )
        )
    log.record(
        make_decision(
            "go2_01.agent",
            "fallback",
            mission_id="m1",
            output={"interaction_id": "int_1", "destination": "poi_bus_stop"},
            policy={"rule": "human_interaction"},
        )
    )
    log.record(
        make_decision(
            "coordinator",
            "forget",
            mission_id="m1",
            output={
                "interaction_id": "int_1",
                "outcome": "arrived",
                "deleted": True,
                "declared_constraint": "uses a wheelchair",  # must never be exported
            },
        )
    )
    (run / "telemetry.jsonl").write_text(
        json.dumps({"stamp": "t", "robot_id": "go2_01", "x": 0, "y": 0}) + "\n"
    )
    return run


def test_ds09_never_exports_interaction_content_or_people(tmp_path: Path) -> None:
    cfg = load_config("config", env={}, fleet_file="sim/fleet.yaml")
    run = _run(tmp_path)
    plain = export(run, tmp_path / "plain", cfg, mission_id="m1")
    assert {"schema.json", "metadata.json", "telemetry.json", "mission.json"} <= set(plain["files"])
    assert not (tmp_path / "plain" / "images").exists()  # images: off by default
    text = "".join(p.read_text() for p in (tmp_path / "plain").glob("*.json"))
    assert "wheelchair" not in text and "int_1" not in text and "poi_bus_stop" not in text
    with_images = export(run, tmp_path / "img", cfg, mission_id="m1", with_images=True)
    assert with_images["images"] == ["o_bag.jpg"]  # never the one with a person in view
    meta = json.loads((tmp_path / "img" / "metadata.json").read_text())
    assert meta["images"] == ["o_bag.jpg"] and "SNS JU" in meta["template"]
