"""Textual TUI dashboard for cco.

Live table of every Claude Code session, refreshed from the state dir every
500ms. Arrow keys navigate; Enter jumps the user's tmux client to the
selected session's pane; q quits.

Keep this file under ~400 lines — split widgets out the moment it grows.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.widgets import DataTable, Footer, Header, Static

from claude_orchestrator.constants import STATUS_DISPLAY, AgentStatus
from claude_orchestrator.state.manager import StateManager
from claude_orchestrator.state.models import AgentState
from claude_orchestrator.tmux.discover import enrich_state_files
from claude_orchestrator.tmux.navigator import JumpResult, jump_to

REFRESH_INTERVAL = 0.5  # seconds


class StatusToast(Static):
    """One-line ephemeral message at the bottom (jump result, errors, etc.)."""

    DEFAULT_CSS = """
    StatusToast {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    """


class CcoApp(App[int]):
    """Live dashboard for Claude Code sessions."""

    CSS = """
    Container#main {
        height: 100%;
    }
    DataTable {
        height: 1fr;
    }
    """

    TITLE = "claude-orchestrator"
    SUB_TITLE = "live session dashboard"

    BINDINGS: ClassVar[Sequence[Binding]] = [  # type: ignore[assignment]
        Binding("q", "quit", "quit"),
        Binding("ctrl+c", "quit", "quit", show=False),
        # priority=True: DataTable's default Enter handler can swallow events
        # before app-level bindings see them. Also backstopped by
        # on_data_table_row_selected below.
        Binding("enter", "jump", "jump to selected session", priority=True),
        Binding("r", "refresh", "refresh now"),
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
    ]

    def __init__(self, manager: StateManager | None = None) -> None:
        super().__init__()
        self._manager = manager or StateManager()
        self._sid_by_row: list[str] = []  # row index → session_id
        self._toast: StatusToast | None = None

    # ---- compose --------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="main"):
            table: DataTable[str] = DataTable(zebra_stripes=True, cursor_type="row")
            table.add_columns(
                "STATUS", "PROJECT", "AGE", "LAST EVENT", "TOOLS", "ERR", "CWD", "SID"
            )
            yield table
            self._toast = StatusToast("")
            yield self._toast
        yield Footer()

    # ---- lifecycle ------------------------------------------------------

    def on_mount(self) -> None:
        self._refresh_table()
        self.set_interval(REFRESH_INTERVAL, self._refresh_table)

    # ---- data -----------------------------------------------------------

    def _refresh_table(self) -> None:
        agents = self._manager.scan()

        table = self.query_one(DataTable)

        # Preserve cursor on the same session_id across refreshes.
        prev_sid: str | None = None
        if self._sid_by_row:
            try:
                prev_sid = self._sid_by_row[table.cursor_row]
            except (IndexError, AttributeError):
                prev_sid = None

        table.clear()
        self._sid_by_row = []

        for agent in agents:
            row_idx = self._add_row(table, agent)
            self._sid_by_row.append(agent.session_id)
            if prev_sid and agent.session_id == prev_sid:
                table.move_cursor(row=row_idx)

        self.sub_title = f"{len(agents)} session(s)"

    def _add_row(self, table: DataTable[str], agent: AgentState) -> int:
        symbol, label, color = STATUS_DISPLAY[agent.status]
        status_cell = f"[{color}]{symbol} {label}[/]"
        err_cell = f"[red]{agent.error_count}[/]" if agent.error_count else "0"
        return (
            table.row_count
            if table.add_row(
                status_cell,
                agent.project_name or "-",
                _human_age(agent.last_event_time),
                agent.last_event,
                str(agent.tool_count),
                err_cell,
                agent.cwd,
                agent.session_id[:8],
            )
            is None
            else table.row_count - 1
        )

    # ---- actions --------------------------------------------------------

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Backup path: DataTable emits RowSelected on Enter even when the
        priority binding handles it. Idempotent — both paths invoke the same
        action; whichever fires first wins because action_jump is cheap."""
        del event  # unused
        self.action_jump()

    def action_refresh(self) -> None:
        self._refresh_table()
        self._set_toast("refreshed")

    def action_jump(self) -> None:
        table = self.query_one(DataTable)
        try:
            sid = self._sid_by_row[table.cursor_row]
        except (IndexError, AttributeError):
            self._set_toast("no row selected")
            return

        agent = next(
            (a for a in self._manager.scan() if a.session_id == sid),
            None,
        )
        if agent is None:
            self._set_toast(f"session {sid[:8]} disappeared between refreshes")
            return

        outcome = jump_to(agent)
        if outcome.ok:
            self._set_toast(f"→ jumped to {agent.project_name or sid[:8]}")
            return

        # If tmux info was missing, try to discover it from /proc + tmux
        # right now. Sessions started before `cco init` (or before the hook
        # captured tmux env) won't have it on file until next event.
        if outcome.result is JumpResult.NO_TMUX_INFO:
            self._set_toast("looking up tmux pane…")
            updated = enrich_state_files(self._manager.directory)
            if updated:
                refreshed = next(
                    (a for a in self._manager.scan() if a.session_id == sid),
                    None,
                )
                if refreshed is not None:
                    outcome = jump_to(refreshed)
                    if outcome.ok:
                        self._refresh_table()
                        self._set_toast(
                            f"→ jumped to {refreshed.project_name or sid[:8]} (auto-discovered)"
                        )
                        return
        self._set_toast(_jump_error(outcome.result, outcome.detail))

    # ---- utilities ------------------------------------------------------

    def _set_toast(self, text: str) -> None:
        if self._toast is not None:
            self._toast.update(text)


# ---------------------------------------------------------------------------
# helpers (extracted so we can unit-test without spinning up Textual)
# ---------------------------------------------------------------------------


def _human_age(iso_ts: str) -> str:
    if not iso_ts:
        return "-"
    try:
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except ValueError:
        return "?"
    delta = max(0, int((datetime.now(UTC) - ts).total_seconds()))
    if delta < 60:
        return f"{delta}s"
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86400:
        return f"{delta // 3600}h"
    return f"{delta // 86400}d"


def _jump_error(result: JumpResult, detail: str) -> str:
    pretty = {
        JumpResult.NO_TMUX_INFO: "session not in tmux — can't jump",
        JumpResult.SESSION_NOT_FOUND: "tmux session gone (closed?)",
        JumpResult.TMUX_MISSING: "tmux not installed",
        JumpResult.FAILED: "jump failed",
    }
    base = pretty.get(result, "jump failed")
    return f"{base}: {detail}" if detail else base


def _silence_unused_status(_: AgentStatus) -> None:
    """Touch AgentStatus so import isn't flagged. Removed once a feature uses it."""
    return None


def run() -> int:
    """Entry point invoked by `cco tui`. Returns the exit code."""
    return CcoApp().run() or 0
