"""End-to-end CLI tests for list/status/tmux-widget."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_orchestrator.cli import main
from claude_orchestrator.constants import AgentStatus
from claude_orchestrator.state.models import AgentState


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    sd = tmp_path / "sessions"
    sd.mkdir(parents=True)
    monkeypatch.setenv("CCO_STATE_DIR", str(sd))
    return sd


def _write(directory: Path, sid: str, **kwargs: object) -> None:
    base = {
        "session_id": sid,
        "cwd": "/tmp/x",
        "started_at": "2026-04-29T10:00:00Z",
        "last_event_time": "2026-04-29T10:00:00Z",
    }
    base.update(kwargs)
    state = AgentState(**base)  # type: ignore[arg-type]
    (directory / f"{sid}.json").write_text(state.to_json())


def test_list_empty_says_so(state_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "No active sessions" in out


def test_list_renders_session_rows(state_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(state_dir, "abc12345", status=AgentStatus.WORKING, project_name="myproj", tool_count=7)
    _write(
        state_dir,
        "def67890",
        status=AgentStatus.WAITING_PERMISSION,
        project_name="otherproj",
    )
    rc = main(["list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "myproj" in out
    assert "otherproj" in out
    assert "abc12345"[:8] in out
    assert "WORK" in out
    assert "PERM" in out


def test_status_empty(state_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "total:0" in out


def test_status_with_attention(state_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(state_dir, "a", status=AgentStatus.WAITING_PERMISSION)
    _write(state_dir, "b", status=AgentStatus.WORKING)
    _write(state_dir, "c", status=AgentStatus.ERROR)
    rc = main(["status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "P:1" in out
    assert "W:1" in out
    assert "E:1" in out
    assert "total:3" in out


def test_tmux_widget_empty_outputs_dot(state_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["tmux-widget"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "cco" in out
    # Empty state shows dim dot.
    assert "·" in out


def test_tmux_widget_includes_color_escapes(
    state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(state_dir, "a", status=AgentStatus.WORKING)
    _write(state_dir, "b", status=AgentStatus.WAITING_PERMISSION)
    rc = main(["tmux-widget"])
    out = capsys.readouterr().out
    assert rc == 0
    # tmux-style color tags
    assert "#[fg=" in out
    assert "PERM:1" in out
    assert "W:1" in out


def test_unknown_subcommand_still_returns_two(capsys: pytest.CaptureFixture[str]) -> None:
    # argparse calls sys.exit(2) on an invalid subcommand choice.
    with pytest.raises(SystemExit) as exc:
        main(["bogus-cmd-doesnt-exist"])
    assert exc.value.code == 2
    err = capsys.readouterr().err.lower()
    assert "invalid choice" in err or "unrecognized" in err or "bogus" in err
