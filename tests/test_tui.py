"""Tests for the Textual TUI dashboard. Uses Textual's Pilot harness.

We don't try to assert pixel layout; just that the table populates from
StateManager and that key bindings invoke the right action.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from claude_orchestrator.state.manager import StateManager
from claude_orchestrator.state.models import AgentState
from claude_orchestrator.tui import app as tui_app
from claude_orchestrator.tui.app import CcoApp


def _write_state(directory: Path, sid: str, **overrides: Any) -> None:
    base = {
        "session_id": sid,
        "cwd": "/tmp/x",
        "started_at": "2026-04-29T10:00:00Z",
        "last_event_time": "2026-04-29T10:00:00Z",
    }
    base.update(overrides)
    state = AgentState(**base)
    (directory / f"{sid}.json").write_text(state.to_json())


@pytest.fixture
def populated_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    sd = tmp_path / "sessions"
    sd.mkdir()
    monkeypatch.setenv("CCO_STATE_DIR", str(sd))
    _write_state(sd, "alpha-id", project_name="alpha", tool_count=5)
    _write_state(sd, "beta-id", project_name="beta", tool_count=2)
    _write_state(sd, "gamma-id", project_name="gamma", tool_count=0)
    return sd


@pytest.mark.asyncio
async def test_app_populates_table_on_mount(populated_dir: Path) -> None:
    app = CcoApp(manager=StateManager(populated_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        # Three state files → three rows.
        assert len(app._sid_by_row) == 3
        assert set(app._sid_by_row) == {"alpha-id", "beta-id", "gamma-id"}


@pytest.mark.asyncio
async def test_app_quits_on_q(populated_dir: Path) -> None:
    app = CcoApp(manager=StateManager(populated_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
        assert not app.is_running


@pytest.mark.asyncio
async def test_app_refresh_on_r(populated_dir: Path) -> None:
    app = CcoApp(manager=StateManager(populated_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        before_rows = len(app._sid_by_row)
        # Add another session file mid-run.
        _write_state(populated_dir, "delta-id", project_name="delta")
        await pilot.press("r")
        await pilot.pause()
        assert len(app._sid_by_row) == before_rows + 1


@pytest.mark.asyncio
async def test_jump_action_invokes_navigator(
    populated_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pressing Enter should call jump_to() with the agent at the cursor."""
    from claude_orchestrator.tmux import navigator

    captured: list[AgentState] = []

    def fake_jump(agent: AgentState) -> Any:
        captured.append(agent)

        class _Outcome:
            ok = True
            result = navigator.JumpResult.OK
            detail = ""

        return _Outcome()

    monkeypatch.setattr(tui_app, "jump_to", fake_jump)

    app = CcoApp(manager=StateManager(populated_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        # Directly invoke the action — testing the binding wiring is
        # Textual's job; we just want to confirm the action calls jump_to.
        app.action_jump()
        await pilot.pause()

    assert len(captured) == 1, "action_jump should call jump_to exactly once"
    assert captured[0].session_id in {"alpha-id", "beta-id", "gamma-id"}


@pytest.mark.asyncio
async def test_enter_keybinding_invokes_jump(
    populated_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pressing Enter (NOT calling the action directly) must trigger jump_to.

    This is the regression test for the bug where DataTable swallowed Enter
    before our app-level binding fired.
    """
    from claude_orchestrator.tmux import navigator

    captured: list[AgentState] = []

    def fake_jump(agent: AgentState) -> Any:
        captured.append(agent)

        class _Outcome:
            ok = True
            result = navigator.JumpResult.OK
            detail = ""

        return _Outcome()

    monkeypatch.setattr(tui_app, "jump_to", fake_jump)

    app = CcoApp(manager=StateManager(populated_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

    assert len(captured) >= 1, "pressing Enter must call jump_to"


# ---- pure-function helpers (no Textual harness needed) -------------------


def test_human_age_renders_seconds() -> None:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    ts = (now - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = tui_app._human_age(ts)
    assert out.endswith("s") or out.endswith("m")  # rounding-tolerant


def test_human_age_handles_empty() -> None:
    assert tui_app._human_age("") == "-"


def test_human_age_handles_garbage() -> None:
    assert tui_app._human_age("not a date") == "?"


def test_jump_error_has_useful_message() -> None:
    from claude_orchestrator.tmux.navigator import JumpResult

    assert "tmux" in tui_app._jump_error(JumpResult.NO_TMUX_INFO, "")
    assert "tmux" in tui_app._jump_error(JumpResult.TMUX_MISSING, "")
