"""Tests for StateManager scan + sort + corruption tolerance."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_orchestrator.constants import AgentStatus
from claude_orchestrator.state.manager import StateManager
from claude_orchestrator.state.models import AgentState


def _write_state(directory: Path, sid: str, **kwargs: object) -> Path:
    base = {
        "session_id": sid,
        "cwd": "/tmp/x",
        "started_at": "2026-04-29T10:00:00Z",
    }
    base.update(kwargs)
    state = AgentState(**base)  # type: ignore[arg-type]
    p = directory / f"{sid}.json"
    p.write_text(state.to_json())
    return p


def test_scan_empty_dir_returns_empty_list(tmp_path: Path) -> None:
    assert StateManager(tmp_path).scan() == []


def test_scan_missing_dir_returns_empty_list(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    assert StateManager(missing).scan() == []


def test_scan_returns_all_state_files(tmp_path: Path) -> None:
    _write_state(tmp_path, "a", status=AgentStatus.WORKING)
    _write_state(tmp_path, "b", status=AgentStatus.IDLE)
    _write_state(tmp_path, "c", status=AgentStatus.WAITING_PERMISSION)
    agents = StateManager(tmp_path).scan()
    assert {a.session_id for a in agents} == {"a", "b", "c"}


def test_scan_sorts_by_last_event_time_desc(tmp_path: Path) -> None:
    _write_state(tmp_path, "old", last_event_time="2026-04-29T08:00:00Z")
    _write_state(tmp_path, "new", last_event_time="2026-04-29T20:00:00Z")
    _write_state(tmp_path, "mid", last_event_time="2026-04-29T12:00:00Z")
    agents = StateManager(tmp_path).scan()
    assert [a.session_id for a in agents] == ["new", "mid", "old"]


def test_scan_skips_corrupt_files(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    _write_state(tmp_path, "good", status=AgentStatus.WORKING)
    (tmp_path / "broken.json").write_text("{not json")
    (tmp_path / "wrong-schema.json").write_text(
        '{"schema_version": 999, "session_id": "x", "cwd": "/", "started_at": "now"}'
    )
    agents = StateManager(tmp_path).scan()
    assert {a.session_id for a in agents} == {"good"}
    assert any("Skipping corrupt state file" in r.message for r in caplog.records)


def test_scan_skips_atomic_tempfiles(tmp_path: Path) -> None:
    _write_state(tmp_path, "real", status=AgentStatus.IDLE)
    # Simulate an in-flight atomic write.
    (tmp_path / ".tmp.abc123.json").write_text("not_real_yet")
    agents = StateManager(tmp_path).scan()
    assert [a.session_id for a in agents] == ["real"]


def test_get_summary_aggregates(tmp_path: Path) -> None:
    _write_state(tmp_path, "a", status=AgentStatus.WORKING)
    _write_state(tmp_path, "b", status=AgentStatus.WORKING)
    _write_state(tmp_path, "c", status=AgentStatus.WAITING_PERMISSION)
    summary = StateManager(tmp_path).get_summary()
    assert summary.working == 2
    assert summary.waiting_permission == 1
    assert summary.total == 3
