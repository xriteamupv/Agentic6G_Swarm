"""Paper exports and the result manifests."""

import json
from pathlib import Path

from a6g_common.config import load_config
from a6g_devstack import bench_protocols, manifest, paper_export


def _cfg():
    return load_config("config", env={}, fleet_file="sim/fleet.yaml")


def test_a_manifest_says_what_produced_a_result() -> None:
    m = manifest.build(_cfg(), seeds=3, models=manifest.models_of(_cfg(), coordinator_llm=None))
    for key in (
        "commit",
        "dirty",
        "command",
        "config_sha256",
        "seeds",
        "models",
        "mode",
        "coverage_source",
        "host",
    ):
        assert key in m
    assert m["models"]["coordinator_llm"] == {"model": "fake", "fidelity": "offline"}
    assert m["mode"] == "always_grant" and m["coverage_source"] == "radio_dt"
    assert "network.yaml" in m["config_sha256"]


def test_median_and_iqr() -> None:
    assert paper_export.med_iqr([1, 2, 3, 4, 5], 0) == "3 [2, 4]"
    assert paper_export.med_iqr([], 0) == "n/a"


def test_results_from_another_commit_are_flagged(tmp_path: Path) -> None:
    data = {"manifest": {"commit": "a" * 40, "models": {}}}
    line = paper_export.manifest_line("x", tmp_path / "x.json", data, "b" * 40)
    assert "possibly stale" in line
    assert "no manifest" in paper_export.manifest_line("x", tmp_path / "x.json", {}, "b" * 40)
    same = paper_export.manifest_line("x", tmp_path / "x.json", data, "a" * 40)
    assert "stale" not in same


def test_export_writes_results_and_cost_table(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(paper_export, "RESULTS", tmp_path / "results")
    (tmp_path / "results").mkdir()
    out = paper_export.export(tmp_path / "paper", _cfg())
    assert Path(out["written"]["RESULTS.md"]).exists()
    assert "table_cost.csv" in out["written"]
    assert "No result yet" in (tmp_path / "paper" / "RESULTS.md").read_text()


def test_protocol_matrix_and_payload_sizes(tmp_path: Path) -> None:
    spec = bench_protocols.load_spec(Path("config/bench/protocols.yaml"))
    blocks = bench_protocols.matrix(spec, reduced=False)
    ids = [b["id"] for b in blocks]
    assert len(ids) == len(set(ids))
    # one packet-capture check per binding
    assert sum(1 for b in blocks if b.get("capture")) == len(spec["bindings"])
    reduced = bench_protocols.matrix(spec, reduced=True)
    assert {b["pattern"] for b in reduced} <= {"P1", "P3", "P6", "outage"}
    log = tmp_path / "comms.jsonl"
    log.write_text(
        "\n".join(
            json.dumps(
                {
                    "direction": "send",
                    "pattern": "P1",
                    "schema": "envelope",
                    "payload_bytes": n,
                    "nl_bytes": 60,
                }
            )
            for n in (700, 800, 900)
        )
    )
    sizes = bench_protocols.payload_sizes([log], seed=1, n=20)
    assert set(sizes["P1"]) <= {700, 800, 900} and set(sizes["P1_nl"]) == {60}
    assert sizes["P2"] == [bench_protocols.DEFAULTS["P2"]] * 20  # not in the log: default
