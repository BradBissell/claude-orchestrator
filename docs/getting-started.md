# Getting started with `cco`

A 5-minute walkthrough from zero → live dashboard. Targets Ubuntu 22.04+
with bash, jq, tmux, and Python 3.11+.

## 1. Prerequisites

```bash
# Verify your distro has what's needed.
command -v jq    >/dev/null && echo "✓ jq"    || echo "  apt install jq"
command -v tmux  >/dev/null && echo "✓ tmux"  || echo "  apt install tmux"
command -v flock >/dev/null && echo "✓ flock" || echo "  (part of util-linux; should already be present)"
python3 -c 'import sys; assert sys.version_info >= (3,11)' && echo "✓ python ≥ 3.11"
```

## 2. Install

While the project is in pre-v1, install editable from your local checkout:

```bash
cd ~/projects/claude-orchestrator
pipx install --editable .
cco --version  # → cco 0.0.0
```

## 3. Wire up hooks

`cco init` adds entries to `~/.claude/settings.json` so every Claude Code
event ends up writing to cco's state dir. Other hooks already in your
`settings.json` (gsd-*, cbm-*, …) are preserved.

```bash
# Preview first — nothing is written.
cco init --dry-run

# Actually install. Creates a timestamped backup before any change.
cco init

# Restart any open Claude Code sessions so they pick up the new hooks.
```

To remove later:

```bash
cco uninstall              # remove just cco's hook entries
cco uninstall --dry-run    # preview
cco uninstall --restore-backup  # rescue mode: restore the most recent backup
```

## 4. Verify it's working

In one terminal, start a Claude Code session normally. In another:

```bash
cco list      # rich table of every detected session
cco status    # one-liner: "W:1 I:0 | total:1"
```

State files live at `~/.local/state/claude-orchestrator/sessions/<sid>.json`
(override with `$CCO_STATE_DIR`).

## 5. tmux status-bar widget

Add this to `~/.tmux.conf` to show live session counts in tmux's status bar:

```tmux
# claude-orchestrator widget — refreshes every 2s
set -g status-interval 2
set -g status-right '#(cco tmux-widget) | %H:%M %d-%b'

# Optional: highlight tmux windows where activity is happening so you can
# spot which session needs attention even before checking cco.
setw -g monitor-activity on
setw -g monitor-bell on
set  -g visual-activity off
set  -g visual-bell off
```

Reload tmux config inside an existing session:

```bash
tmux source-file ~/.tmux.conf
```

The widget output is colour-coded:

| Tag | Meaning |
|---|---|
| `PERM:N` | N sessions waiting on permission (red) |
| `WAIT:N` | N sessions waiting on user answer (yellow) |
| `ERR:N`  | N sessions in error state (purple) |
| `W:N`    | N sessions actively working (green) |
| `I:N`    | N idle sessions (dim) |
| `·`      | No sessions detected |

## 6. Per-session details on demand

```bash
# Watch the table refresh (2s default; tweak with -n)
watch -n 2 'cco list'

# Inspect a specific session's raw JSON
cat ~/.local/state/claude-orchestrator/sessions/<sid>.json | jq .
```

A real Textual dashboard (`cco tui`) is wired in P6 of the roadmap. For
now, `watch cco list` is the daily-driver UI.

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| `cco list` shows no sessions despite running Claude Code | `cco init` not run, or claude was running before `cco init` and hasn't been restarted |
| State files exist but `cco list` says "No active sessions" | Check `$CCO_STATE_DIR` matches what your hooks write to (env var inheritance from your shell) |
| `cco init` reports "already installed for: …" | Idempotent — nothing to do. To verify, `grep claude-orchestrator ~/.claude/settings.json` |
| Hook handler errors in `~/.claude/settings.json.bak.*` | `cco uninstall --restore-backup` resets to the most recent good backup |
| Want to nuke everything | `cco uninstall && rm -rf ~/.local/state/claude-orchestrator ~/.config/claude-orchestrator` |

## 8. What's next

See `docs/project-brief.md` for the full roadmap. The next milestone (P5+)
adds:

- Reconciliation pass (mark dead sessions DEAD)
- Textual TUI dashboard (`cco tui`)
- Linux notifications via `notify-send`
- Rules engine (auto-allow/deny via `~/.config/claude-orchestrator/rules.yaml`)
- claude-resume-recent integration (tmux pane tagging)
