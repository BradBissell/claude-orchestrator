"""Tests for the per-widget pieces of the TUI layout.

Covers what the existing test_tui.py doesn't: rendering of the HeaderBar
counter strip and the sparkline glyph mapping. Layout-level smoke tests
(app composes without crashing, every AgentStatus renders) live here too,
since they exercise the new widget tree.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from claude_orchestrator.constants import AgentStatus
from claude_orchestrator.state.manager import StateManager
from claude_orchestrator.state.models import AgentState, StatusSummary
from claude_orchestrator.tui.app import CcoApp
from claude_orchestrator.tui.widgets.header_bar import format_header
from claude_orchestrator.tui.widgets.session_row import (
    _SPARK_GLYPHS,
    _SPARK_WIDTH,
    render_sparkline,
)

# ---- render_sparkline -----------------------------------------------------


def test_sparkline_empty_returns_placeholder_of_fixed_width() -> None:
    out = render_sparkline([])
    assert len(out) == _SPARK_WIDTH
    # Placeholder must NOT use spark glyphs — that would be a lie about activity.
    assert not any(c in out for c in _SPARK_GLYPHS)


def test_sparkline_full_range_maps_to_extremes() -> None:
    out = render_sparkline([0.0, 1.0])
    assert out.endswith(_SPARK_GLYPHS[0] + _SPARK_GLYPHS[-1])


def test_sparkline_clamps_out_of_range_values() -> None:
    out = render_sparkline([-2.0, 5.0])
    assert out.endswith(_SPARK_GLYPHS[0] + _SPARK_GLYPHS[-1])


def test_sparkline_truncates_to_width() -> None:
    samples = [i / 32 for i in range(64)]
    out = render_sparkline(samples)
    assert len(out) == _SPARK_WIDTH


def test_sparkline_left_pads_short_input_to_width() -> None:
    out = render_sparkline([0.5, 0.5])
    assert len(out) == _SPARK_WIDTH


# ---- layout smoke ---------------------------------------------------------


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
def all_statuses_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """One state file per AgentStatus value."""
    sd = tmp_path / "sessions"
    sd.mkdir()
    monkeypatch.setenv("CCO_STATE_DIR", str(sd))
    for i, status in enumerate(AgentStatus):
        _write_state(sd, f"sid-{i}", status=status, project_name=f"proj-{status.value}")
    return sd


@pytest.mark.asyncio
async def test_app_renders_every_status_without_crashing(all_statuses_dir: Path) -> None:
    """Mount the app against one row per AgentStatus; no exception means the
    SessionRow rich markup is well-formed for every color/symbol combination.

    DEAD sessions are hidden from the dashboard, so the rendered count is
    one less than the total number of statuses.
    """
    app = CcoApp(manager=StateManager(all_statuses_dir))
    async with app.run_test() as pilot:  # type: ignore[arg-type]
        await pilot.pause()
        assert len(app._sid_by_row) == len(list(AgentStatus)) - 1
        dead_sid = next(f"sid-{i}" for i, s in enumerate(AgentStatus) if s is AgentStatus.DEAD)
        assert dead_sid not in app._sid_by_row


def test_header_bar_format_includes_every_label() -> None:
    summary = StatusSummary(
        working=1, idle=1, waiting_permission=1, waiting_answer=1, error=1, dead=1
    )
    rendered = format_header(summary)
    for label in ("PERM", "WAIT", "ERR", "WORK", "IDLE", "DEAD", "TOTAL"):
        assert label in rendered
    # TOTAL count reflects the sum.
    assert "TOTAL 6" in rendered


def test_header_bar_format_dims_zero_buckets() -> None:
    """Zero-count buckets render in [dim] so the eye skips them."""
    summary = StatusSummary(working=2)
    rendered = format_header(summary)
    assert "[dim]PERM 0[/]" in rendered
    assert "[bold #3fb950]WORK[/] [bold]2[/]" in rendered


def test_status_summary_from_agents_counts_buckets() -> None:
    agents = [
        AgentState(
            session_id=f"sid-{i}",
            cwd="/tmp/x",
            started_at="2026-04-29T10:00:00Z",
            status=status,
        )
        for i, status in enumerate(AgentStatus)
    ]
    summary = StatusSummary.from_agents(agents)
    assert summary.total == len(list(AgentStatus))
    assert summary.attention == 3  # PERM + WAIT + ERROR


def test_is_heartbeat_stale_flags_working_with_old_event() -> None:
    from datetime import UTC, datetime, timedelta

    from claude_orchestrator.constants import AgentStatus
    from claude_orchestrator.state.models import AgentState
    from claude_orchestrator.tui.widgets.session_row import is_heartbeat_stale

    old = (datetime.now(UTC) - timedelta(seconds=600)).isoformat().replace("+00:00", "Z")
    fresh = (datetime.now(UTC) - timedelta(seconds=2)).isoformat().replace("+00:00", "Z")
    base = dict(
        session_id="x",
        cwd="/tmp",
        started_at="2026-04-29T10:00:00Z",
        project_name="x",
    )
    stale_agent = AgentState(**base, status=AgentStatus.WORKING, last_event_time=old)
    fresh_agent = AgentState(**base, status=AgentStatus.WORKING, last_event_time=fresh)
    idle_agent = AgentState(**base, status=AgentStatus.IDLE, last_event_time=old)

    assert is_heartbeat_stale(stale_agent, threshold_sec=60)
    assert not is_heartbeat_stale(fresh_agent, threshold_sec=60)
    # IDLE never goes stale — no hook activity is normal.
    assert not is_heartbeat_stale(idle_agent, threshold_sec=60)


def test_is_heartbeat_stale_tolerates_garbage_timestamp() -> None:
    from claude_orchestrator.constants import AgentStatus
    from claude_orchestrator.state.models import AgentState
    from claude_orchestrator.tui.widgets.session_row import is_heartbeat_stale

    a = AgentState(
        session_id="x",
        cwd="/tmp",
        started_at="2026-04-29T10:00:00Z",
        status=AgentStatus.WORKING,
        last_event_time="not-a-date",
    )
    assert not is_heartbeat_stale(a)
