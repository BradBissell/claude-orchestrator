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


def test_is_stale_tmux_ref_classifies_recoverable_failures() -> None:
    """Re-discovery should retry on NO_TMUX_INFO and on FAILED-with-can't-find;
    not on TMUX_MISSING / SESSION_NOT_FOUND / unrelated FAILED reasons."""
    from claude_orchestrator.tmux.navigator import JumpOutcome, JumpResult

    assert tui_app._is_stale_tmux_ref(JumpOutcome(JumpResult.NO_TMUX_INFO, ""))
    assert tui_app._is_stale_tmux_ref(
        JumpOutcome(JumpResult.FAILED, "select-window failed: can't find pane: %16")
    )
    assert tui_app._is_stale_tmux_ref(
        JumpOutcome(JumpResult.FAILED, "select-window failed: can't find window: claude")
    )
    # Not recoverable via re-discovery:
    assert not tui_app._is_stale_tmux_ref(JumpOutcome(JumpResult.OK, ""))
    assert not tui_app._is_stale_tmux_ref(JumpOutcome(JumpResult.TMUX_MISSING, "no tmux"))
    assert not tui_app._is_stale_tmux_ref(
        JumpOutcome(JumpResult.SESSION_NOT_FOUND, "tmux session 'work' no longer exists")
    )
    assert not tui_app._is_stale_tmux_ref(
        JumpOutcome(JumpResult.FAILED, "tmux subprocess timed out")
    )


@pytest.mark.asyncio
async def test_jump_retries_with_rediscovery_on_stale_pane(
    populated_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: when a state file's recorded pane is dead but claude_pid
    is still alive in a new pane, action_jump should call enrich_state_files,
    re-read the state, and retry the jump — instead of just toasting the
    'can't find pane' error."""
    from claude_orchestrator.tmux.navigator import JumpOutcome, JumpResult

    jump_calls: list[AgentState] = []
    enrich_calls: list[Path] = []

    def fake_jump(agent: AgentState) -> JumpOutcome:
        jump_calls.append(agent)
        # First call returns the stale-pane failure; second call (after
        # enrich rewrote the file) succeeds.
        if len(jump_calls) == 1:
            return JumpOutcome(
                JumpResult.FAILED,
                "select-window failed: can't find pane: %16",
            )
        return JumpOutcome(JumpResult.OK, "")

    def fake_enrich(directory: Path) -> int:
        enrich_calls.append(directory)
        return 1  # pretend we updated one state file

    monkeypatch.setattr(tui_app, "jump_to", fake_jump)
    monkeypatch.setattr(tui_app, "enrich_state_files", fake_enrich)

    app = CcoApp(manager=StateManager(populated_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        app.action_jump()
        await pilot.pause()

    assert len(jump_calls) == 2, "should retry jump_to once after enrich rewrites state"
    assert len(enrich_calls) == 1, "should call enrich_state_files exactly once"


@pytest.mark.asyncio
async def test_jump_does_not_rediscover_on_unrecoverable_failures(
    populated_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A FAILED outcome that isn't a 'can't find ...' tmux error must not
    trigger a re-discovery — that would just produce a noisy retry loop."""
    from claude_orchestrator.tmux.navigator import JumpOutcome, JumpResult

    jump_calls: list[AgentState] = []
    enrich_calls: list[Path] = []

    def fake_jump(agent: AgentState) -> JumpOutcome:
        jump_calls.append(agent)
        return JumpOutcome(JumpResult.FAILED, "tmux subprocess timed out")

    def fake_enrich(directory: Path) -> int:
        enrich_calls.append(directory)
        return 0

    monkeypatch.setattr(tui_app, "jump_to", fake_jump)
    monkeypatch.setattr(tui_app, "enrich_state_files", fake_enrich)

    app = CcoApp(manager=StateManager(populated_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        app.action_jump()
        await pilot.pause()

    assert len(jump_calls) == 1, "no retry expected for non-tmux-ref failures"
    assert enrich_calls == [], "enrich must not run for unrecoverable failures"


@pytest.mark.asyncio
async def test_summarize_action_writes_to_store_and_repaints(
    populated_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pressing `s` triggers a (mocked) summarizer and persists the result."""
    summary_dir = tmp_path / "summaries"
    monkeypatch.setattr("claude_orchestrator.summary_store.summary_dir", lambda: summary_dir)
    monkeypatch.setattr(
        "claude_orchestrator.tui.app.summarize_transcript",
        lambda _path: "stubbed summary",
    )

    app = CcoApp(manager=StateManager(populated_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        # Reset the store's directory: the populated_dir fixture set CCO_STATE_DIR,
        # but SummaryStore was constructed before our monkeypatch took effect.
        from claude_orchestrator.summary_store import SummaryStore

        app._summaries = SummaryStore()
        app.action_summarize()
        await app.workers.wait_for_complete()
        await pilot.pause()

    sid = app._sid_by_row[0]
    saved = (summary_dir / f"{sid}.json").read_text()
    assert "stubbed summary" in saved


@pytest.mark.asyncio
async def test_summarize_action_with_no_selection_toasts(
    populated_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("claude_orchestrator.summary_store.summary_dir", lambda: tmp_path / "s")
    app = CcoApp(manager=StateManager(populated_dir))
    captured: list[str] = []
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        # Capture toast messages via the public-ish helper rather than poking
        # at Static internals (which differ across Textual versions).
        monkeypatch.setattr(app, "_set_toast", lambda text: captured.append(text))
        app._sid_by_row = []
        app.action_summarize()
        await pilot.pause()
    assert any("no row" in msg for msg in captured)


@pytest.mark.asyncio
async def test_reorder_path_preserves_listitem_widgets(populated_dir: Path) -> None:
    """When only the sort order changes, rows are reordered in place.

    The original ListItem widgets must survive across the refresh so the
    ListView doesn't get cleared+rebuilt (which produces a visible flash).
    """
    app = CcoApp(manager=StateManager(populated_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        original_items = dict(app._items_by_sid)
        original_sids = list(app._sid_by_row)
        assert len(original_items) == 3

        # Bump beta-id's last_event_time so it sorts to the top, changing
        # the order without changing the set of session_ids.
        _write_state(
            populated_dir,
            "beta-id",
            project_name="beta",
            tool_count=2,
            last_event_time="2030-01-01T00:00:00Z",
        )
        app._refresh_table()
        await pilot.pause()

        assert app._sid_by_row[0] == "beta-id"
        assert set(app._sid_by_row) == set(original_sids)
        # Same widget objects — proves move_child path was taken, not rebuild.
        for sid, item in original_items.items():
            assert app._items_by_sid[sid] is item


@pytest.mark.asyncio
async def test_dead_sessions_are_hidden_from_dashboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_orchestrator.constants import AgentStatus

    sd = tmp_path / "sessions"
    sd.mkdir()
    monkeypatch.setenv("CCO_STATE_DIR", str(sd))
    _write_state(sd, "live", project_name="live")
    _write_state(sd, "ghost", project_name="ghost", status=AgentStatus.DEAD)

    app = CcoApp(manager=StateManager(sd))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        assert app._sid_by_row == ["live"]
        assert "ghost" not in app._items_by_sid


@pytest.mark.asyncio
async def test_next_attention_jumps_to_perm_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_orchestrator.constants import AgentStatus

    sd = tmp_path / "sessions"
    sd.mkdir()
    monkeypatch.setenv("CCO_STATE_DIR", str(sd))
    # 4 sessions; only the third needs attention.
    _write_state(sd, "a", project_name="a", status=AgentStatus.IDLE)
    _write_state(sd, "b", project_name="b", status=AgentStatus.WORKING)
    _write_state(sd, "c", project_name="c", status=AgentStatus.WAITING_PERMISSION)
    _write_state(sd, "d", project_name="d", status=AgentStatus.IDLE)

    app = CcoApp(manager=StateManager(sd))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        from textual.widgets import ListView

        list_view = app.query_one(ListView)
        list_view.index = 0
        await pilot.press("n")
        await pilot.pause()
        # Row index of 'c' in _sid_by_row.
        target_idx = app._sid_by_row.index("c")
        assert list_view.index == target_idx


@pytest.mark.asyncio
async def test_next_attention_wraps_when_past_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_orchestrator.constants import AgentStatus

    sd = tmp_path / "sessions"
    sd.mkdir()
    monkeypatch.setenv("CCO_STATE_DIR", str(sd))
    _write_state(sd, "a", project_name="a", status=AgentStatus.WAITING_PERMISSION)
    _write_state(sd, "b", project_name="b", status=AgentStatus.IDLE)

    app = CcoApp(manager=StateManager(sd))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        from textual.widgets import ListView

        list_view = app.query_one(ListView)
        # Park cursor past the only attention row to force wrap.
        list_view.index = len(app._sid_by_row) - 1
        await pilot.press("n")
        await pilot.pause()
        target_idx = app._sid_by_row.index("a")
        assert list_view.index == target_idx


def test_agent_matches_filter_substrings() -> None:
    from claude_orchestrator.tui.app import _agent_matches_filter

    a = AgentState(
        session_id="abc-123",
        cwd="/home/me/projects/marketplace",
        started_at="2026-04-29T10:00:00Z",
        project_name="marketplace",
        last_summary="rewriting the checkout flow",
    )
    assert _agent_matches_filter(a, "market")
    assert _agent_matches_filter(a, "CHECKOUT")  # case-insensitive
    assert _agent_matches_filter(a, "abc")  # session_id matches
    assert not _agent_matches_filter(a, "nope")


@pytest.mark.asyncio
async def test_filter_action_hides_non_matching_rows(populated_dir: Path) -> None:
    app = CcoApp(manager=StateManager(populated_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        assert len(app._sid_by_row) == 3

        app._filter = "alpha"
        app._refresh_table()
        await pilot.pause()
        assert app._sid_by_row == ["alpha-id"]

        # Clearing the filter restores all rows.
        app.action_clear_filter()
        await pilot.pause()
        assert set(app._sid_by_row) == {"alpha-id", "beta-id", "gamma-id"}


def test_summary_line_shows_cap_when_configured() -> None:
    """When account.weekly_cap_tokens is set, summary line shows used/cap and %."""
    from claude_orchestrator.state.models import StatusSummary
    from claude_orchestrator.tui.activity import ActivitySampler
    from claude_orchestrator.tui.app import _render_summary_line
    from claude_orchestrator.tui.tokens import TokenTracker

    summary = StatusSummary(working=1)
    sampler = ActivitySampler()
    tokens = TokenTracker()
    # No agents → total_tokens=0, but cap rendering should still apply.
    line = _render_summary_line(summary, [], sampler, tokens, weekly_cap=1_000_000)
    assert "/ 1.0M" in line
    assert "0%" in line


def test_summary_line_omits_cap_when_unset() -> None:
    from claude_orchestrator.state.models import StatusSummary
    from claude_orchestrator.tui.app import _render_summary_line

    line = _render_summary_line(StatusSummary(), [], None, None, weekly_cap=None)
    # No "/<cap>" suffix when cap unset; the "[/]" in markup is a closing tag
    # not a slash separator.
    assert " / " not in line
    assert "tokens:" in line
