"""Textual TUI dashboard for cco.

Live list of every Claude Code session, refreshed from the state dir every
500ms. j/k or arrows navigate; Enter jumps the user's tmux client to the
selected session's pane; q quits.

Layout (matches docs/stitch-handoff.md):

  Header (Textual)
  HeaderBar (PERM/WAIT/ERR/WORK/IDLE/DEAD counters)
  Sessions (ListView of SessionRow cards, 2 lines each)
  Summary line (active count / token total / aggregate spark)
  StatusToast (ephemeral last-action message)
  Footer (key hints)

Keep this file under ~400 lines — split widgets out the moment it grows.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.widgets import Footer, Header, Input, ListItem, ListView, Static

from claude_orchestrator.account import AccountConfig, load_account_config
from claude_orchestrator.constants import AgentStatus
from claude_orchestrator.state.manager import StateManager
from claude_orchestrator.state.models import AgentState, StatusSummary
from claude_orchestrator.state.reconciler import reconcile
from claude_orchestrator.summarizer import summarize_transcript
from claude_orchestrator.summary_store import SummaryStore
from claude_orchestrator.tmux.discover import enrich_state_files
from claude_orchestrator.tmux.navigator import JumpResult, jump_to, kill_session
from claude_orchestrator.tui.activity import ActivitySampler
from claude_orchestrator.tui.tokens import TokenTracker, format_tokens, transcript_path
from claude_orchestrator.tui.widgets import HeaderBar, SessionRow
from claude_orchestrator.tui.widgets.session_row import render_sparkline

REFRESH_INTERVAL = 0.5  # seconds
RECONCILE_INTERVAL = 30.0  # how often to sweep dead state files / reset stuck waits
KILL_CONFIRM_WINDOW_SEC = 3.0  # second-press window to actually fire the kill


class StatusToast(Static):
    """One-line ephemeral message at the bottom (jump result, errors, etc.)."""


class CcoApp(App[int]):
    """Live dashboard for Claude Code sessions."""

    CSS_PATH = Path(__file__).parent / "theme.tcss"

    TITLE = "claude-orchestrator"
    SUB_TITLE = "live session dashboard"

    BINDINGS: ClassVar[Sequence[Binding]] = [  # type: ignore[assignment]
        Binding("q", "quit", "quit"),
        Binding("ctrl+c", "quit", "quit", show=False),
        # priority=True so the ListView's own Enter handler doesn't swallow it.
        # Backstopped by on_list_view_selected below.
        Binding("enter", "jump", "jump to selected session", priority=True),
        Binding("r", "refresh", "refresh now"),
        Binding("x", "kill", "kill selected session"),
        Binding("s", "summarize", "summarize selected session"),
        Binding("n", "next_attention", "jump cursor to next PERM/WAIT/ERR row"),
        Binding("slash", "filter", "filter sessions by substring"),
        Binding("escape", "clear_filter", "clear filter", show=False),
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
    ]

    def __init__(self, manager: StateManager | None = None) -> None:
        super().__init__()
        self._manager = manager or StateManager()
        self._sid_by_row: list[str] = []  # row index → session_id
        self._rows_by_sid: dict[str, SessionRow] = {}  # for in-place updates
        self._items_by_sid: dict[str, ListItem] = {}  # for in-place reorder
        self._toast: StatusToast | None = None
        self._header_bar: HeaderBar | None = None
        self._summary_line: Static | None = None
        self._activity = ActivitySampler()
        self._tokens = TokenTracker()
        self._account: AccountConfig = load_account_config()
        self._summaries = SummaryStore()
        self._summarizing: set[str] = set()  # in-flight session_ids
        self._kill_armed_sid: str | None = None
        self._kill_armed_at: float = 0.0
        self._filter: str = ""  # case-insensitive substring filter; "" = show all
        self._filter_input: Input | None = None

    # ---- compose --------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="main"):
            self._header_bar = HeaderBar()
            yield self._header_bar
            yield ListView(id="session-list")
            self._summary_line = Static("", id="summary-line")
            yield self._summary_line
            self._filter_input = Input(
                placeholder="filter (esc to clear)…", id="filter-input"
            )
            self._filter_input.display = False
            yield self._filter_input
            self._toast = StatusToast("")
            yield self._toast
        yield Footer()

    # ---- lifecycle ------------------------------------------------------

    def on_mount(self) -> None:
        self._refresh_table()
        self.set_interval(REFRESH_INTERVAL, self._refresh_table)
        # Reconcile less often — disk I/O cost, and the state it cleans up
        # accumulates slowly. Run once at startup so a freshly opened TUI
        # immediately reflects the cleaned-up world.
        self._reconcile_now()
        self.set_interval(RECONCILE_INTERVAL, self._reconcile_now)

    def _reconcile_now(self) -> None:
        """Sweep orphaned state files and reset stuck WAITING_* statuses.

        Best-effort: any failure is swallowed so a transient permission or
        FS issue doesn't crash the dashboard.
        """
        with contextlib.suppress(OSError):
            reconcile(self._manager.directory)

    # ---- data -----------------------------------------------------------

    def _refresh_table(self) -> None:
        # DEAD sessions are hidden from the dashboard. The on-disk state file
        # is left alone; scan() will mark it DEAD again next tick if the
        # claude_pid is still missing, and a future cleanup pass can sweep it.
        agents = [a for a in self._manager.scan() if a.status != AgentStatus.DEAD]
        if self._filter:
            agents = [a for a in agents if _agent_matches_filter(a, self._filter)]

        # Sample CPU activity for every live session before rendering, so the
        # SessionRow gets fresh sparkline data this tick. Prune buffers for
        # pids that vanished so dead sessions don't leak memory.
        live_pids = [a.claude_pid for a in agents if a.claude_pid is not None]
        for pid in live_pids:
            self._activity.sample(pid)
        self._activity.prune(live_pids)

        list_view = self.query_one(ListView)
        new_sids = [a.session_id for a in agents]

        # Path 1 — identical order. Just refresh row text in place.
        if new_sids == self._sid_by_row and self._rows_by_sid:
            self._update_rows(agents)
            self._maybe_summarize_new(agents)
            self._update_chrome(agents)
            return

        # Path 2 — same set, different order. Refresh text and reorder rows
        # via move_child so the widget tree isn't torn down. This is the
        # dominant refresh case: sort-by-last_event_time means any hook fire
        # rearranges the list, which used to fall through to the cold path's
        # clear()+rebuild and produce a visible flash every few seconds.
        new_set = set(new_sids)
        prev_set = set(self._sid_by_row)
        if (
            self._rows_by_sid
            and new_set == prev_set
            and len(new_sids) == len(self._sid_by_row)
        ):
            cursor_sid = self._cursor_sid(list_view)
            self._update_rows(agents)
            self._reorder_list_view(list_view, new_sids)
            self._sid_by_row = list(new_sids)
            if cursor_sid is not None:
                with contextlib.suppress(ValueError):
                    list_view.index = new_sids.index(cursor_sid)
            self._maybe_summarize_new(agents)
            self._update_chrome(agents)
            return

        # Cold path — sessions added or removed. Preserve cursor by sid.
        prev_sid: str | None = self._cursor_sid(list_view)

        list_view.clear()
        self._sid_by_row = []
        self._rows_by_sid = {}
        self._items_by_sid = {}

        cursor_target = 0
        for i, agent in enumerate(agents):
            row = SessionRow()
            row.update_agent(
                agent,
                samples=self._activity.samples_for(agent.claude_pid),
                summary=self._summaries.get(agent.session_id),
                tokens=self._tokens.total_for(agent),
            )
            item = ListItem(row)
            list_view.append(item)
            self._sid_by_row.append(agent.session_id)
            self._rows_by_sid[agent.session_id] = row
            self._items_by_sid[agent.session_id] = item
            if prev_sid and agent.session_id == prev_sid:
                cursor_target = i

        if self._sid_by_row:
            list_view.index = cursor_target

        self._maybe_summarize_new(agents)
        self._update_chrome(agents)

    def _update_rows(self, agents: list[AgentState]) -> None:
        """Refresh in-place row content for every cached SessionRow."""
        for agent in agents:
            row = self._rows_by_sid.get(agent.session_id)
            if row is not None:
                row.update_agent(
                    agent,
                    samples=self._activity.samples_for(agent.claude_pid),
                    summary=self._summaries.get(agent.session_id),
                )

    def _reorder_list_view(self, list_view: ListView, target_order: list[str]) -> None:
        """Reorder ListView children to match target_order without rebuilding.

        Uses Widget.move_child to swap items into place. Children that are
        already in their target position are skipped.
        """
        for target_idx, sid in enumerate(target_order):
            item = self._items_by_sid.get(sid)
            if item is None:
                continue
            try:
                current_idx = list_view.children.index(item)
            except (ValueError, AttributeError):
                continue
            if current_idx == target_idx:
                continue
            list_view.move_child(item, before=target_idx)

    def _cursor_sid(self, list_view: ListView) -> str | None:
        """Resolve ListView's highlighted index to a session_id (or None)."""
        try:
            idx = list_view.index
        except AttributeError:
            return None
        if idx is None:
            return None
        try:
            return self._sid_by_row[idx]
        except IndexError:
            return None

    def _maybe_summarize_new(self, agents: list[AgentState]) -> None:
        """Lazy-once: kick off a summary for any session we haven't summarized yet.

        Caller's responsibility to call after a refresh — this is cheap when
        all sessions are already cached or in-flight.
        """
        for agent in agents:
            sid = agent.session_id
            if sid in self._summarizing:
                continue
            if self._summaries.has(sid):
                continue
            self._summarize(sid, agent.cwd, manual=False)

    def _update_chrome(self, agents: list[AgentState]) -> None:
        """Refresh the parts outside the session list (header / summary / title).

        These are cheap text-only Static.update() calls; they don't flash.
        Memoize the rendered strings so identical-content ticks are no-ops —
        Textual still repaints on update() regardless.
        """
        summary = StatusSummary.from_agents(agents)
        if self._header_bar is not None:
            self._header_bar.update_summary(summary)
        if self._summary_line is not None:
            self._summary_line.update(
                _render_summary_line(
                    summary,
                    agents,
                    self._activity,
                    self._tokens,
                    weekly_cap=self._account.weekly_cap_tokens,
                )
            )
        self.sub_title = f"{len(agents)} session(s)"

    # ---- actions --------------------------------------------------------

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Backstop for Enter: ListView's own Selected event also fires the jump."""
        del event  # unused
        self.action_jump()

    def action_refresh(self) -> None:
        self._refresh_table()
        self._set_toast("refreshed")

    def action_filter(self) -> None:
        """Reveal the filter input and focus it. '/' enters this state."""
        if self._filter_input is None:
            return
        self._filter_input.display = True
        self._filter_input.value = self._filter
        self._filter_input.focus()

    def action_clear_filter(self) -> None:
        """Hide the filter input and reset the filter. Bound to escape."""
        self._filter = ""
        if self._filter_input is not None:
            self._filter_input.value = ""
            self._filter_input.display = False
        self.query_one(ListView).focus()
        self._refresh_table()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Live-filter as the user types in the filter input."""
        if self._filter_input is None or event.input is not self._filter_input:
            return
        self._filter = event.value
        self._refresh_table()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Pressing Enter in the filter input commits the filter and returns
        focus to the list."""
        if self._filter_input is None or event.input is not self._filter_input:
            return
        self.query_one(ListView).focus()

    def action_cursor_down(self) -> None:
        self.query_one(ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one(ListView).action_cursor_up()

    def action_next_attention(self) -> None:
        """Move cursor to the next row whose status needs attention.

        Cycles through ATTENTION_STATUSES (PERM / WAIT / ERR), wrapping.
        Looks first at rows strictly after the cursor; if none, wraps to top.
        """
        from claude_orchestrator.constants import ATTENTION_STATUSES

        list_view = self.query_one(ListView)
        # Iterate _sid_by_row (the dashboard's view of the world) rather than
        # scan() to stay in sync with what the user actually sees on screen.
        if not self._sid_by_row:
            self._set_toast("no sessions")
            return

        # Pull current statuses from the latest scan so we don't reuse a
        # stale snapshot; map sid → status for visible rows only.
        status_by_sid: dict[str, AgentStatus] = {
            a.session_id: a.status for a in self._manager.scan()
        }
        attention_indices = [
            i
            for i, sid in enumerate(self._sid_by_row)
            if status_by_sid.get(sid) in ATTENTION_STATUSES
        ]
        if not attention_indices:
            self._set_toast("no rows need attention")
            return

        cursor_idx = list_view.index if list_view.index is not None else -1
        target = next(
            (i for i in attention_indices if i > cursor_idx),
            attention_indices[0],
        )
        list_view.index = target
        target_sid = self._sid_by_row[target]
        target_status = status_by_sid.get(target_sid, AgentStatus.IDLE)
        self._set_toast(f"→ {target_sid[:8]} ({target_status.value})")

    def action_kill(self) -> None:
        """Two-press kill: first press arms, second within 3s fires.

        Killing tears down the claude process, the tmux window, and the
        on-disk state file. No undo — the confirmation window is the only
        guard against accidental presses.
        """
        list_view = self.query_one(ListView)
        idx = list_view.index
        try:
            sid = self._sid_by_row[idx] if idx is not None else None
        except (IndexError, AttributeError):
            sid = None
        if sid is None:
            self._set_toast("no row selected")
            return

        agent = next(
            (a for a in self._manager.scan() if a.session_id == sid),
            None,
        )
        if agent is None:
            self._set_toast(f"session {sid[:8]} disappeared between refreshes")
            self._kill_armed_sid = None
            return

        label = agent.project_name or sid[:8]
        now = time.monotonic()
        armed = (
            self._kill_armed_sid == sid
            and now - self._kill_armed_at < KILL_CONFIRM_WINDOW_SEC
        )

        if not armed:
            self._kill_armed_sid = sid
            self._kill_armed_at = now
            self._set_toast(f"press x again within 3s to kill {label}")
            return

        self._kill_armed_sid = None
        outcome = kill_session(agent, self._manager.directory)
        # Drop any cached summary so a future session reusing the sid doesn't
        # display stale text. Best-effort.
        self._summaries.delete(sid)
        if outcome.ok:
            note = f" ({outcome.detail})" if outcome.detail else ""
            self._set_toast(f"killed {label}{note}")
        else:
            self._set_toast(f"kill failed: {outcome.detail}")
        self._refresh_table()

    def action_summarize(self) -> None:
        """Force-refresh the summary for the highlighted session.

        Useful when the conversation has progressed beyond what was cached
        on first sight. Rate-limited via the in-flight set: pressing `s`
        repeatedly is a no-op while a previous call is still pending.
        """
        list_view = self.query_one(ListView)
        idx = list_view.index
        try:
            sid = self._sid_by_row[idx] if idx is not None else None
        except (IndexError, AttributeError):
            sid = None
        if sid is None:
            self._set_toast("no row selected")
            return
        agent = next(
            (a for a in self._manager.scan() if a.session_id == sid),
            None,
        )
        if agent is None:
            self._set_toast(f"session {sid[:8]} disappeared between refreshes")
            return
        if sid in self._summarizing:
            self._set_toast("already summarizing…")
            return
        self._summarize(sid, agent.cwd, manual=True)

    def action_jump(self) -> None:
        list_view = self.query_one(ListView)
        idx = list_view.index
        try:
            sid = self._sid_by_row[idx] if idx is not None else None
        except (IndexError, AttributeError):
            sid = None

        if sid is None:
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

        # Retry with re-discovery when the recorded tmux ref is stale:
        #   - NO_TMUX_INFO: state file never had tmux fields (started outside cco)
        #   - FAILED with "can't find {pane,window,session}": claude moved to a
        #     new pane (e.g. user closed the window and `claude --resume`d,
        #     leaving the recorded pane_id pointing at a dead pane).
        # enrich_state_files re-walks /proc + tmux and overwrites the state
        # file when it finds the live claude_pid in a different pane.
        if _is_stale_tmux_ref(outcome):
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

    @work(thread=True, exit_on_error=False, group="summarize")
    def _summarize(self, sid: str, cwd: str, manual: bool) -> None:
        """Background-thread worker: read transcript, ask Haiku, store result.

        Lazy-once policy: caller (refresh path) only schedules when there's
        no cached summary; manual=True force-refreshes via the `s` action.
        Either way we de-dupe via `self._summarizing` so simultaneous calls
        for the same sid collapse.
        """
        if sid in self._summarizing:
            return
        self._summarizing.add(sid)
        try:
            if manual:
                # Show progress toast on the UI thread.
                self.call_from_thread(self._set_toast, f"summarizing {sid[:8]}…")
            path = transcript_path(cwd, sid)
            text = summarize_transcript(path)
            self.call_from_thread(self._on_summary_done, sid, text, manual)
        finally:
            self._summarizing.discard(sid)

    def _on_summary_done(self, sid: str, text: str, manual: bool) -> None:
        """Main-thread callback: persist the result and repaint the row."""
        if text:
            self._summaries.set(sid, text)
            row = self._rows_by_sid.get(sid)
            if row is not None:
                # Re-render just this row in place — no flash, no full rebuild.
                agent = next(
                    (a for a in self._manager.scan() if a.session_id == sid),
                    None,
                )
                if agent is not None:
                    row.update_agent(
                        agent,
                        samples=self._activity.samples_for(agent.claude_pid),
                        summary=text,
                    )
            if manual:
                self._set_toast(f"summary updated for {sid[:8]}")
        elif manual:
            # Manual press deserves an explanation when summarization failed.
            self._set_toast(
                "summary unavailable — log in to Claude Code or set ANTHROPIC_API_KEY"
            )


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


def _is_stale_tmux_ref(outcome: object) -> bool:
    """Whether re-discovery is worth retrying for this jump failure.

    NO_TMUX_INFO means the state file never had tmux fields. FAILED with a
    'can't find …' tmux error means the recorded pane/window/session is gone
    but claude_pid may still be alive in a new pane (think `claude --resume`
    after the original window was closed). Both are recoverable via
    enrich_state_files; other failures aren't.
    """
    result = getattr(outcome, "result", None)
    if result is JumpResult.NO_TMUX_INFO:
        return True
    if result is JumpResult.FAILED:
        detail = (getattr(outcome, "detail", "") or "").lower()
        return "can't find" in detail
    return False


def _render_summary_line(
    summary: StatusSummary,
    agents: list[AgentState] | None = None,
    sampler: ActivitySampler | None = None,
    tokens: TokenTracker | None = None,
    weekly_cap: int | None = None,
) -> str:
    """Bottom strip: active-count + aggregate sparkline + token total + cap."""
    active = summary.working + summary.attention
    aggregate: list[float] = []
    if agents and sampler:
        # Aggregate spark = sum-of-fractions across live sessions, clamped to 1.
        # Stripe-aligns sample positions across sessions by index.
        per_session: list[list[float]] = [
            sampler.samples_for(a.claude_pid) for a in agents if a.claude_pid
        ]
        if per_session:
            width = max(len(s) for s in per_session)
            for col in range(width):
                total = 0.0
                for s in per_session:
                    if len(s) > col:
                        total += s[col]
                aggregate.append(min(total, 1.0))
    spark = render_sparkline(aggregate)
    total_tokens = tokens.total_across(agents) if agents and tokens else 0
    tok_text = format_tokens(total_tokens) if total_tokens else "—"
    # When the user has configured a weekly cap, render `<used> / <cap> (W%)`
    # with a color cue based on consumption — green/yellow/orange/red mirrors
    # the four-bucket pattern from ccboard. Red kicks in past 95% so the user
    # has explicit warning before the cap actually bites.
    if weekly_cap and weekly_cap > 0:
        pct = min(999, round(100 * total_tokens / weekly_cap))
        if pct >= 95:
            cap_color = "#f85149"
        elif pct >= 80:
            cap_color = "#ff8c00"
        elif pct >= 60:
            cap_color = "#EBCB8B"
        else:
            cap_color = "#A3BE8C"
        tok_segment = (
            f"[dim]tokens: {tok_text} / {format_tokens(weekly_cap)} "
            f"([{cap_color}]{pct}%[/])[/]"
        )
    else:
        tok_segment = f"[dim]tokens: {tok_text}[/]"
    return (
        f"[bold #00ffff]●[/] [bold]{active}[/] active   "
        f"[#00ffff]{spark}[/]   "
        f"{tok_segment}"
    )


def _agent_matches_filter(agent: AgentState, needle: str) -> bool:
    """Case-insensitive substring match across the user-visible fields."""
    n = needle.lower()
    haystack = " ".join(
        s.lower()
        for s in (
            agent.project_name,
            agent.cwd,
            agent.last_summary,
            agent.session_id,
            agent.last_event,
        )
        if s
    )
    return n in haystack


def run() -> int:
    """Entry point invoked by `cco tui`. Returns the exit code."""
    return CcoApp().run() or 0
