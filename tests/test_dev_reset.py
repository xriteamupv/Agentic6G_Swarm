"""``a6g dev reset``: the dev stack's accumulated state is cleared only while it is stopped."""

from pathlib import Path

import pytest
from a6g_cli.main import app
from a6g_devstack import reset as R
from typer.testing import CliRunner


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    (tmp_path / "langgraph.json").write_text("{}")
    (tmp_path / ".langgraph_api").mkdir()
    (tmp_path / ".langgraph_api" / ".langgraph_checkpoint.1.pckl").write_bytes(b"x" * 2048)
    (tmp_path / "var" / "sim_stack").mkdir(parents=True)
    (tmp_path / "var" / "sim_stack" / "comms.jsonl").write_text("{}\n")
    (tmp_path / "config").mkdir()  # untouched
    monkeypatch.chdir(tmp_path / "var")  # found from a subdirectory too
    monkeypatch.setattr(R, "port_in_use", lambda port, host="127.0.0.1": False)
    return tmp_path


def test_refuses_while_the_stack_runs(repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(R, "port_in_use", lambda port, host="127.0.0.1": port == 2024)
    result = CliRunner().invoke(app, ["dev", "reset", "--yes"])
    assert result.exit_code == 1
    assert "langgraph dev (terminal 2)" in result.output
    assert (repo / ".langgraph_api").exists() and (repo / "var" / "sim_stack").exists()


def test_moves_the_state_aside(repo: Path) -> None:
    result = CliRunner().invoke(app, ["dev", "reset", "--yes"])
    assert result.exit_code == 0, result.output
    assert not (repo / ".langgraph_api").exists() and not (repo / "var" / "sim_stack").exists()
    (backup,) = (repo / "var" / "reset").iterdir()
    assert (backup / ".langgraph_api" / ".langgraph_checkpoint.1.pckl").stat().st_size == 2048
    assert (backup / "var__sim_stack" / "comms.jsonl").exists()
    assert (repo / "config").exists()


def test_only_the_server_and_delete(repo: Path) -> None:
    result = CliRunner().invoke(app, ["dev", "reset", "--only", "server", "--delete", "--yes"])
    assert result.exit_code == 0, result.output
    assert not (repo / ".langgraph_api").exists()
    assert (repo / "var" / "sim_stack" / "comms.jsonl").exists()  # the sim stays
    assert not (repo / "var" / "reset").exists()


def test_asks_first(repo: Path) -> None:
    result = CliRunner().invoke(app, ["dev", "reset"], input="n\n")
    assert result.exit_code == 1
    assert ".langgraph_api" in result.output and (repo / ".langgraph_api").exists()


def test_each_stack_run_starts_a_fresh_message_log(tmp_path: Path, monkeypatch) -> None:
    """The comms log does not grow across runs: the last run is kept as .prev."""
    from a6g_cli.comms_report import default_logs
    from a6g_observability.comms import fresh_log

    log = tmp_path / "var" / "sim_stack" / "comms.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text('{"run": 1}\n')
    assert fresh_log(log) == log and not log.exists()
    prev = log.with_name("comms.prev.jsonl")
    assert prev.read_text() == '{"run": 1}\n'
    log.write_text('{"run": 2}\n')
    fresh_log(log)  # only one previous run is kept
    assert prev.read_text() == '{"run": 2}\n' and not log.exists()
    fresh_log(log)  # nothing logged yet: the kept run stays
    assert prev.read_text() == '{"run": 2}\n'
    monkeypatch.chdir(tmp_path)
    assert Path("var/sim_stack/comms.prev.jsonl") in default_logs(tmp_path / "none")


def test_serve_starts_the_coordinator_on_a_clean_slate(repo: Path, monkeypatch) -> None:
    """Each start deletes the last session's coordinator state and turns file persistence
    off for the session (gigabytes of saved state stalled every model call)."""
    import os

    seen = {}

    def fake_exec(path, args, env):
        seen.update(path=path, args=args, env=env)

    monkeypatch.setattr(os, "execve", fake_exec)
    result = CliRunner().invoke(app, ["dev", "serve", "--port", "2099"])
    assert result.exit_code == 0, result.output
    assert not (repo / ".langgraph_api").exists()
    assert (repo / "var" / "sim_stack").exists()  # only the coordinator's state
    assert seen["args"][1:] == ["dev", "--no-browser", "--no-reload", "--port", "2099"]
    assert seen["env"]["LANGGRAPH_DISABLE_FILE_PERSISTENCE"] == "true"
