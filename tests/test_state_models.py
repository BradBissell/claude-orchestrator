"""Tests for AgentState and StatusSummary models."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_orchestrator.constants import AgentStatus
from claude_orchestrator.state.models import AgentState, Notification, StatusSummary


def _minimal_state(**overrides: object) -> AgentState:
    base = {
        "session_id": "abc-123",
        "cwd": "/tmp/x",
        "started_at": "2026-04-29T10:00:00Z",
    }
    base.update(overrides)
    return AgentState(**base)  # type: ignore[arg-type]


def test_to_json_roundtrip() -> None:
    s = _minimal_state(status=AgentStatus.WORKING, project_name="x", tool_count=3)
    raw = s.to_json()
    data = json.loads(raw)
    assert data["session_id"] == "abc-123"
    assert data["status"] == "WORKING"
    assert data["schema_version"] == 1
    assert data["tool_count"] == 3


def test_from_json_file(tmp_path: Path) -> None:
    s = _minimal_state(status=AgentStatus.WAITING_PERMISSION)
    p = tmp_path / "abc-123.json"
    p.write_text(s.to_json())
    loaded = AgentState.from_json_file(p)
    assert loaded.session_id == "abc-123"
    assert loaded.status is AgentStatus.WAITING_PERMISSION


def test_missing_schema_version_rejected() -> None:
    with pytest.raises(ValueError, match="schema_version"):
        AgentState.from_dict(
            {
                "session_id": "x",
                "cwd": "/",
                "started_at": "2026-04-29T10:00:00Z",
            }
        )


def test_unknown_schema_version_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported schema_version"):
        AgentState.from_dict(
            {
                "schema_version": 999,
                "session_id": "x",
                "cwd": "/",
                "started_at": "2026-04-29T10:00:00Z",
            }
        )


def test_unknown_status_falls_back_to_idle() -> None:
    s = AgentState.from_dict(
        {
            "schema_version": 1,
            "session_id": "x",
            "cwd": "/",
            "started_at": "2026-04-29T10:00:00Z",
            "status": "GREMLIN",
        }
    )
    assert s.status is AgentStatus.IDLE


def test_notification_serialization_roundtrip() -> None:
    s = _minimal_state(
        status=AgentStatus.WAITING_PERMISSION,
        notification=Notification(type="permission", tool="Bash", redacted_summary="…"),
    )
    data = json.loads(s.to_json())
    assert data["notification"]["type"] == "permission"
    assert data["notification"]["tool"] == "Bash"


def test_status_summary_counts() -> None:
    agents = [
        _minimal_state(session_id="a", status=AgentStatus.WORKING),
        _minimal_state(session_id="b", status=AgentStatus.WORKING),
        _minimal_state(session_id="c", status=AgentStatus.WAITING_PERMISSION),
        _minimal_state(session_id="d", status=AgentStatus.IDLE),
        _minimal_state(session_id="e", status=AgentStatus.ERROR),
    ]
    summary = StatusSummary.from_agents(agents)
    assert summary.working == 2
    assert summary.idle == 1
    assert summary.waiting_permission == 1
    assert summary.error == 1
    assert summary.total == 5
    assert summary.attention == 2  # perm + error


def test_status_line_with_attention() -> None:
    agents = [
        _minimal_state(session_id="a", status=AgentStatus.WAITING_PERMISSION),
        _minimal_state(session_id="b", status=AgentStatus.WORKING),
    ]
    line = StatusSummary.from_agents(agents).status_line()
    assert "P:1" in line
    assert "W:1" in line


def test_status_line_empty() -> None:
    assert StatusSummary.from_agents([]).status_line() == "—"
