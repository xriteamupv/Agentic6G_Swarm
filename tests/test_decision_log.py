from datetime import UTC, datetime

from a6g_cli.main import app
from a6g_common.clock import ManualClock
from a6g_common.ids import SequentialIds
from a6g_common.schemas import DecisionKind
from a6g_observability import DecisionLog, make_decision
from typer.testing import CliRunner


def test_decision_log_records_and_filters_by_mission(tmp_path) -> None:
    clock = ManualClock(datetime(2026, 10, 5, 12, tzinfo=UTC))
    ids = SequentialIds()
    log = DecisionLog(tmp_path / "decisions.jsonl")
    for mission, kind in [("m1", "allocate"), ("m2", "allocate"), ("m1", "accept")]:
        log.record(
            make_decision(
                "coordinator",
                kind,
                mission_id=mission,
                output={"task_id": "t1"},
                policy={"rule": "hungarian"},
                clock=clock,
                ids=ids,
            )
        )
        clock.advance(1)

    m1 = log.read(mission_id="m1")
    assert [d.kind for d in m1] == [DecisionKind.ALLOCATE, DecisionKind.ACCEPT]
    assert [d.decision_id for d in m1] == ["dec_0001", "dec_0003"]
    assert len(log.read()) == 3

    result = CliRunner().invoke(app, ["decisions", "--mission", "m1", "--log", str(log.path)])
    assert result.exit_code == 0, result.output
    lines = result.stdout.strip().splitlines()
    assert len(lines) == 2
    assert "allocate" in lines[0] and "accept" in lines[1]
    assert 'policy={"rule":"hungarian"}' in lines[0]


def test_decisions_cli_reports_empty(tmp_path) -> None:
    result = CliRunner().invoke(
        app, ["decisions", "--mission", "nope", "--log", str(tmp_path / "x.jsonl")]
    )
    assert result.exit_code == 1


def _chain(tmp_path, n: int = 5):
    from a6g_observability.decision_log import DecisionLog, make_decision

    log = DecisionLog(tmp_path / "chain.jsonl")
    for i in range(n):
        log.record(make_decision("coordinator", "allocate", mission_id="m1", output={"i": i}))
    return log.path


def test_the_log_is_tamper_evident(tmp_path) -> None:
    """Verification fails on a modified, deleted, reordered or inserted record."""
    import json

    from a6g_observability.decision_log import verify

    path = _chain(tmp_path)
    assert verify(path) == {
        "ok": True,
        "records": 5,
        "mission_records": 5,
        "legacy": 0,
        "problems": [],
    }
    good = path.read_text().splitlines()

    def broken(lines: list[str]) -> bool:
        path.write_text("\n".join(lines) + "\n")
        return not verify(path)["ok"]

    edited = json.loads(good[2])
    edited["output"] = {"i": 99}
    assert broken([*good[:2], json.dumps(edited), *good[3:]])  # modified
    assert broken([*good[:2], *good[3:]])  # deleted
    assert broken([good[0], good[2], good[1], *good[3:]])  # reordered
    legacy = {k: v for k, v in json.loads(good[1]).items() if k not in ("hash", "prev_hash")}
    assert broken([*good[:3], json.dumps(legacy), *good[3:]])  # an unchained one inserted
    path.write_text("\n".join(good) + "\n")
    result = CliRunner().invoke(app, ["decisions", "verify", "--log", str(path)])
    assert result.exit_code == 0 and "OK: 5 records" in result.output


def test_concurrent_writers_keep_one_chain(tmp_path) -> None:
    import threading

    from a6g_observability.decision_log import DecisionLog, make_decision, verify

    logs = [DecisionLog(tmp_path / "shared.jsonl") for _ in range(4)]  # 4 "processes"

    def write(log) -> None:
        for _ in range(25):
            log.record(make_decision("robot", "accept", output={}))

    threads = [threading.Thread(target=write, args=(log,)) for log in logs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    result = verify(tmp_path / "shared.jsonl")
    assert result["ok"] and result["records"] == 100, result["problems"][:3]
