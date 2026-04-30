# Project Brief: claude-orchestrator

## Goal

Build a **Linux-native, Wayland-compatible** Claude Code session dashboard that
provides clorch's hook-driven real-time status tracking without the macOS-only
terminal-automation layer that breaks on Ubuntu+GNOME+ghostty. Treat clorch's
architecture as a reference implementation — fork the design, leave behind the
parts that don't work outside macOS.

## Target user (concrete)

- Linux engineer on **Ubuntu 24.04 + GNOME Shell 46 + Wayland + ghostty 1.3.1 + tmux**
- Runs **10–30 simultaneous Claude Code sessions** across worktrees and projects
- Already comfortable in tmux (has a 30-window tmux session today)
- Has a companion script `~/.local/bin/claude-resume-recent` that builds detached tmux sessions for resumed Claude sessions; the dashboard should integrate cleanly with that flow
- Wants to know **at a glance**: which sessions need attention right now (permission requested, error, waiting on user), which are still working, which went idle

## What clorch does well (keep)

The reference clorch source is checked into `reference/clorch/` (18 files, ~3700
lines). Read it before designing.

- **Hook-driven state machine** — Claude Code's settings.json hooks (`PreToolUse`,
  `PostToolUse`, `Notification`, `Stop`, `SessionStart`, etc.) are routed to a
  tiny shell handler that writes one JSON file per session into a state dir. The
  dashboard polls that dir at ~500ms. This avoids tmux output scraping or process
  introspection — events come from the source of truth.
  - Reference: `reference/clorch/src__clorch__hooks__event_handler.sh` (466 lines)
  - Reference: `reference/clorch/src__clorch__hooks__notify_handler.sh` (125 lines)
  - Reference: `reference/clorch/src__clorch__hooks__installer.py` (292 lines, manages settings.json mutation)
  - Reference: `reference/clorch/src__clorch__state__manager.py` (272 lines)
  - Reference: `reference/clorch/src__clorch__state__models.py` (293 lines, AgentState + StatusSummary)
  - Reference: `reference/clorch/src__clorch__state__watcher.py` (150 lines, polling watcher)
- **Status state machine**: `WORKING`, `IDLE`, `WAITING_PERMISSION`, `WAITING_ANSWER`, `ERROR`. Reference: `reference/clorch/src__clorch__constants.py`
- **Action queue UX**: pending permissions get hotkeys (`a`–`z`); operator can approve/deny without leaving the dashboard. `Y` approves all pending; `!` toggles YOLO mode. Reference: `reference/clorch/src__clorch__tui__app.py` (1208 lines, Textual TUI)
- **tmux status-right widget** for compact at-a-glance counts. Reference: `bin/clorch tmux-widget` flow
- **Per-tool rules engine** in YAML — `~/.config/clorch/rules.yaml` controls auto-approve/deny per tool
- **CLI layout**: `clorch` (TUI), `clorch list`, `clorch status`, `clorch tmux-widget`, `clorch init`, `clorch uninstall`
- **Settings-mutation discipline**: `clorch init` backs up `~/.claude/settings.json` before writing, supports `--dry-run`, and `clorch uninstall` cleanly removes the hooks (verified during this user's actual uninstall)

## Linux gaps to fix (the whole reason this project exists)

These are confirmed bugs / no-ops on Ubuntu 24+Wayland+ghostty+tmux, observed
during a multi-hour debugging session today:

1. **Terminal-tab activation is macOS-only**
   - `reference/clorch/src__clorch__terminal__ghostty.py` is a 207-line file
     where every method calls `osascript` (AppleScript). On Linux, every method
     no-ops. Selecting an agent in the TUI to "open its tab" silently fails.
   - The TUI's `→` (jump to selected agent) is broken on Linux.
   - Linux ghostty has no AppleScript bridge. Wayland blocks X11-style window
     poking (no wmctrl/xdotool for Wayland-native windows). Ghostty exposes a
     D-Bus interface (`com.mitchellh.ghostty`) with limited actions:
     `new-window`, `new-window-command`, `present-surface`, `quit`,
     `open-config`, `reload-config`. There's no per-window-close action, but
     `present-surface(uint64 surface_id)` could focus a known window.
   - **Verified during today's session**: spawning a window via
     `ghostty +new-window --command=...` opens a daemon-owned window correctly
     but introduces a Wayland tile-snap freeze (Super+Left hangs the window),
     reproducible across content. Mouse drag works; keyboard tile-snap doesn't.
     Spawning a separate ghostty process (`--gtk-single-instance=false`) gives
     it a duplicate app-id and breaks tile-snap differently. **Conclusion**:
     don't have the orchestrator spawn ghostty windows; let the user open
     windows manually and attach to a pre-built tmux session. (This is exactly
     what the user's `claude-resume-recent` script now does.)

2. **No activity collector for sparklines**
   - The `AgentState.activity_history` field is populated as `[0] * 10` and
     never updated. There's no background poller that samples claude CPU/IO
     activity, so the TUI's sparkline column shows flat lines on Linux.
   - Reference: `reference/clorch/src__clorch__state__models.py` line 70

3. **State directory is in /tmp (volatile)**
   - `STATE_DIR = Path(os.environ.get("CLORCH_STATE_DIR", "/tmp/clorch/state"))`
   - Wiped on reboot. Also accumulates dozens of zero-byte `.tmp.*` atomic-write
     leftovers (we observed ~50 of them).
   - Reference: `reference/clorch/src__clorch__config.py`

4. **Sound alerts are macOS-only**
   - notify_handler.sh uses macOS system sound names. Linux has no equivalent
     out of the box. Need libnotify (`notify-send`) and/or `paplay` with
     freedesktop sound theme.

5. **State-vs-reality drift**
   - Observed in user's environment: 6 state files vs 10 running claude
     processes. Sessions started before hook installation never get state
     files; sessions that crash mid-flight leave stale files. No periodic
     reconciliation pass.

6. **All sessions stuck on last hook event**
   - Observed: every state file showed `last_event=Notification` with status
     `WAITING_ANSWER`, frozen for tens of minutes. Hooks fire on events but
     nothing updates state when the claude process *quietly resumes work* (no
     PostToolUse fires). Need either an idle-timeout transition or the
     activity collector mentioned in (2).

## Target features (v1 scope — be ruthless)

**Must-have (ship-blocking):**

- F1. **Hook installer** that adds claude-orchestrator's hooks to
  `~/.claude/settings.json` (with backup + dry-run + uninstall, mirroring
  clorch's discipline)
- F2. **Hook handler script** (POSIX shell) that writes per-session JSON state
  to `$XDG_STATE_HOME/claude-orchestrator/sessions/<sid>.json` (default
  `~/.local/state/claude-orchestrator/sessions/<sid>.json`) — persistent across
  reboots
- F3. **State machine**: WORKING / IDLE / WAITING_PERMISSION / WAITING_ANSWER /
  ERROR / DEAD (DEAD is new — we explicitly transition when process is gone)
- F4. **CLI surface**: `cco` (or similar short name) with subcommands matching
  clorch (`tui`, `list`, `status`, `tmux-widget`, `init`, `uninstall`)
- F5. **TUI dashboard** showing a live table of all sessions with status,
  project, cwd, age, last event, and (if available) attention reason
- F6. **tmux-window navigation** that uses tmux directly (`tmux select-window`,
  `select-pane`) — the only navigation mode supported on Linux. No ghostty/X11
  stuff. If the session isn't in any tmux window, "open" is a no-op with a
  clear message ("not running under tmux — start the session via tmux").

**Should-have (v1.1 acceptable):**

- S1. **Activity collector** — small thread that polls claude PIDs' CPU% / IO
  every N seconds and pushes into `activity_history` for sparklines.
- S2. **Permission approve/deny inline** — read `~/.claude/projects/<>/.../perm-request.json` (or whatever Claude Code's
  permission protocol exposes) and write a response back. Verify whether this
  is even possible from outside the claude process; if not, drop and document.
- S3. **Rules engine** — YAML at `~/.config/claude-orchestrator/rules.yaml`
  for per-tool auto-approve/deny patterns.
- S4. **Linux notifications** via `notify-send` (libnotify) for attention
  events; sound via `paplay $XDG_DATA_DIRS/sounds/freedesktop/stereo/bell.oga`.
- S5. **Reconciliation pass** — periodically check that state files correspond
  to alive claude processes; mark dead ones DEAD and prune after retention
  window.
- S6. **`claude-resume-recent` integration** — ensure resumed sessions show up
  in the dashboard immediately. Likely requires the hook installer to also
  update tmux window naming so the dashboard can map session→window.

**Won't-have (out of v1 scope):**

- Cross-host (multi-machine) support
- Web UI (TUI only)
- Cost/usage analytics (separate concerns; see claude-monitor)
- macOS support — explicitly Linux-first. PRs welcome later.
- Spawning ghostty windows from the dashboard (the user opens windows manually;
  dashboard provides the attach commands and tmux-window pointers)

## Constraints

- **Language**: Python 3.11+ likely (clorch is Python; reuse what's possible).
  Hook handlers stay in POSIX shell for portability.
- **TUI**: Textual is fine (clorch uses it; user has it implicitly). Or
  alternatives if compelling reason.
- **Dependencies**: minimize. tmux + jq + libnotify + Python stdlib + Textual
  is a reasonable baseline. No GTK, no AppleScript, no X11-only tools.
- **Install**: pipx install from git, ideally a single editable install during
  development. Long-term, publish to PyPI.
- **License**: Match clorch (MIT or AGPL — check `reference/clorch/`'s LICENSE).
- **Repo location**: `~/projects/claude-orchestrator/` (already initialized,
  empty git repo)
- **Existing user setup the dashboard must coexist with**:
  - `~/.local/bin/claude-resume-recent` (selector + tmux session builder)
  - tmux session(s) the user already has running
  - ghostty as the only terminal
  - claude-orchestrator hooks live alongside any other hooks already in
    settings.json (the user has `gsd-*` hooks; don't clobber them)

## Architecture sketch (a starting point — challenge it)

```
Claude Code session
    │
    ▼  hooks (PreToolUse, PostToolUse, Notification, Stop, …)
event_handler.sh ──► writes/updates JSON to ~/.local/state/claude-orchestrator/sessions/<sid>.json
    │
    ▼ (filesystem watcher, 500ms poll)
StateManager (Python) reads all *.json
    │
    ├─► CLI list/status/tmux-widget output
    ├─► Textual TUI dashboard (live)
    ├─► Activity collector (every 1s, samples /proc/<pid>/stat for claude PIDs in same cwd)
    └─► Reconciliation pass (every 10s, marks DEAD sessions)

Navigation: TUI's "jump to selected" calls tmux select-window/-pane only.
            No ghostty automation. Print attach command if not in tmux.
```

## What success looks like

After install and one full claude session lifecycle:

1. `cco init` adds 8–10 hook entries to `~/.claude/settings.json` (with backup),
   does NOT clobber existing `gsd-*` or other hooks.
2. Starting a new claude session creates a state file within 100ms of the first
   hook firing.
3. `cco list` shows that session as `WORKING` (or whatever its real status is).
4. `cco` (TUI) shows it live; status updates within 500ms of a hook event.
5. Triggering a permission prompt in claude shows up in `cco` as
   `WAITING_PERMISSION` within 500ms; the queue gains a hotkey.
6. Pressing `→` (jump) on a session in tmux selects that tmux window. If not in
   tmux, prints `tmux attach -t <session>` to clipboard or shows it.
7. Closing the claude process triggers `Stop` hook → state transitions to
   IDLE/DEAD; session is reconciled out within retention window.
8. `tmux status-right '#(cco tmux-widget)'` shows live counts that match the TUI.
9. After reboot, state files persist (or fresh empty state if appropriate).
10. `cco uninstall` removes all hook entries cleanly, restores settings.json
    backup, leaves the state files intact (separate `--purge-state` flag if
    desired).

## Reference materials checked into this repo

```
reference/clorch/
├── src__clorch__cli.py                       (CLI entrypoint + subcommand routing)
├── src__clorch__config.py                    (paths + constants)
├── src__clorch__constants.py                 (AgentStatus enum, display table)
├── src__clorch__state__manager.py            (StateManager — read JSON files)
├── src__clorch__state__models.py             (AgentState, StatusSummary)
├── src__clorch__state__watcher.py            (polling watcher)
├── src__clorch__hooks__installer.py          (settings.json mutation discipline)
├── src__clorch__hooks__event_handler.sh      (the actual hook script — 466 lines, study this carefully)
├── src__clorch__hooks__notify_handler.sh     (notification hook)
├── src__clorch__terminal__ghostty.py         (the broken-on-Linux backend; reference for what NOT to do)
├── src__clorch__terminal__detect.py          (backend selection logic)
├── src__clorch__tmux__navigator.py           (tmux jump logic — partly portable)
├── src__clorch__tmux__session.py             (tmux integration)
├── src__clorch__tui__app.py                  (Textual TUI — 1208 lines, take cues from selection/queue UX)
├── README.md                                 (clorch's pitch + UX summary)
├── AGENTS.md                                 (clorch's contributor guide)
├── CLAUDE.md                                 (clorch's claude-code contributor instructions)
└── pyproject.toml                            (deps + entry points)
```

You will be asked to analyze this brief from a specific perspective.
**Read the relevant reference files** for grounding rather than relying on
generic advice — the goal is a plan that can actually be built, not a wishlist.

---

## Decisions Log (post-research, 2026-04-29)

### Q1 — Hook payload contents (resolved)

Claude Code hook handlers receive **JSON via stdin** containing `session_id`,
`cwd`, `tool_name`, `tool_input`, `hook_event_name`, `transcript_path`, plus
event-specific fields. **PID is NOT in the payload.**

Implication: clorch's `$PPID` capture is just the parent process (shell, tmux,
ghostty, etc.) — not claude itself. We adopt:

- `session_id` is the **canonical key** for state files, not PID.
- `$PPID` is a secondary liveness hint only — never assumed to be claude.
- For PID-based liveness checks, we resolve `cwd → claude PID` via
  `pgrep -x claude` + `/proc/<pid>/cwd` matching, or skip PID liveness entirely
  and rely on `last_event_time` + reconciler.

### Q2 — tmux pane user-options (resolved, empirically confirmed)

Setting via `tmux set-option -p @cco-session-id $sid` and reading via
`tmux show-options -p @cco-session-id` works. P10 plan is viable: each
spawned pane can be tagged with its session-id, and the hook handler can
read that tag back to populate `tmux_pane`/`tmux_window` in state.

### Q3 — Permission resolution hooks (HUGE simplification)

Claude Code has **25+ hook events** including:

- `PermissionRequest` — fires when permission dialog about to show. Hook
  return value can return `{"hookSpecificOutput": {"decision": {"behavior":
  "allow|deny"}}}` to **auto-resolve** the permission without the user
  seeing the dialog.
- `PermissionDenied` — fires when auto-mode classifier denies a tool call.

**No `PermissionGranted` event** — grant is inferred from next
`PreToolUse` after `PermissionRequest`.

#### Design changes vs original brief

| Original Plan | Updated Plan |
|---|---|
| **P9** (inline approve/deny via `tmux send-keys`) was main implementation, ~2d, brittle | **P9 deleted entirely.** Auto-approve/deny happens via hook return value. The TUI's "approve" button just writes to a pending-decisions file that the next `PermissionRequest` hook reads. |
| Rules engine (P8) was **warn-only in v1**, action gated to v2 | Rules engine **first-class from P8**. The `PermissionRequest` hook consults rules.yaml and returns the decision to claude before the dialog appears. |
| State machine had `WAITING_PERMISSION` exit only via inferred next event | State machine has explicit `PermissionRequest → resolved` (via hook decision recorded) and `PermissionDenied` events |
| Hook events tracked: `PreToolUse`, `PostToolUse`, `Notification`, `Stop`, `SessionStart` | Add: `PermissionRequest`, `PermissionDenied`, `PostToolUseFailure`, `SessionEnd`, `UserPromptSubmit`. Cover the actual decision points. |

#### Hook return-value protocol (new in this design)

The hook handler script for `PermissionRequest` MUST emit JSON to stdout
when it has a decision:

```json
{
  "hookSpecificOutput": {
    "decision": {"behavior": "allow"}
  }
}
```

For `cco` to honor user clicks/keystrokes in the TUI, the TUI writes a
pending-decision file at
`$XDG_STATE_HOME/claude-orchestrator/pending/<session_id>.json`. The
`PermissionRequest` hook handler reads that file (with timeout) and emits
the decision. If no pending decision and no rules-engine match, the hook
emits empty JSON and claude shows its normal dialog.

Race window: from when `PermissionRequest` fires to when the hook handler
reads the pending file is the only window where a TUI click could miss.
Mitigation: TUI commits decision to disk first, THEN updates UI; hook waits
up to 100ms for a fresh decision file before falling through.

#### Reference docs (for design.md citations)

- https://code.claude.com/docs/en/hooks.md — Hooks Reference (events, payloads)
- https://code.claude.com/docs/en/hooks-guide.md — Hooks Guide (PermissionRequest auto-approve)

### Q4 — License (resolved)

**MIT.** Maximum portability, friendliest for upstream PRs to clorch (need
to verify clorch's actual LICENSE — `reference/clorch/` has `LICENSE` file
to check), least friction for contributors.

### Q5 — Repo visibility (resolved)

**Private until v0 ships.** Iterate freely without judgment. Flip public
after the v0 gate passes (1 week of dogfood + decision to proceed to v1).
At that point, also enable issues + draft README + screenshot.

