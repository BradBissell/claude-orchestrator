"""Tests for tmux navigation. Heavy use of monkeypatch on `subprocess.run`
so we don't depend on a real tmux server during unit tests."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from claude_orchestrator.constants import AgentStatus
from claude_orchestrator.state.models import AgentState
from claude_orchestrator.tmux import navigator
from claude_orchestrator.tmux.navigator import JumpResult, jump_to


def _agent(**overrides: Any) -> AgentState:
    base = {
        "session_id": "s1",
        "cwd": "/tmp/x",
        "started_at": "2026-04-29T10:00:00Z",
        "status": AgentStatus.WORKING,
        "tmux_session": "work",
        "tmux_window": "claude",
        "tmux_pane": "%1",
    }
    base.update(overrides)
    return AgentState(**base)


@pytest.fixture
def fake_tmux_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(navigator, "has_tmux", lambda: True)


def test_jump_returns_no_tmux_info_when_state_lacks_pane(
    fake_tmux_present: None,
) -> None:
    a = _agent(tmux_session=None, tmux_window=None, tmux_pane=None)
    outcome = jump_to(a)
    assert outcome.result is JumpResult.NO_TMUX_INFO
    assert not outcome.ok


def test_jump_returns_tmux_missing_when_tmux_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(navigator, "has_tmux", lambda: False)
    outcome = jump_to(_agent())
    assert outcome.result is JumpResult.TMUX_MISSING


def test_jump_returns_session_not_found(
    fake_tmux_present: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(args: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        # has-session returns non-zero (session doesn't exist).
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="no")

    monkeypatch.setattr(subprocess, "run", fake_run)
    outcome = jump_to(_agent())
    assert outcome.result is JumpResult.SESSION_NOT_FOUND


def test_jump_ok_with_session_window_and_pane(
    fake_tmux_present: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    outcome = jump_to(_agent(tmux_session="work", tmux_window="claude", tmux_pane="%1"))

    assert outcome.ok
    # has-session, select-window, select-pane in that order.
    assert any("has-session" in c for c in calls)
    assert any("select-window" in c and "work:claude" in c for c in calls)
    assert any("select-pane" in c and "%1" in c for c in calls)


def test_jump_ok_when_pane_missing_still_selects_window(
    fake_tmux_present: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    outcome = jump_to(_agent(tmux_pane=None))
    assert outcome.ok
    assert any("select-window" in c for c in calls)
    assert not any("select-pane" in c for c in calls)


def test_jump_failed_when_select_window_errors(
    fake_tmux_present: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(args: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        if "has-session" in args:
            return subprocess.CompletedProcess(args, 0, "", "")
        if "select-window" in args:
            return subprocess.CompletedProcess(args, 1, "", "boom")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    outcome = jump_to(_agent())
    assert outcome.result is JumpResult.FAILED
    assert "boom" in outcome.detail
