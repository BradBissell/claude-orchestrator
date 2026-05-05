# Architecture

`cco` is a Linux-native dashboard for Claude Code sessions. It avoids
terminal-automation hacks (AppleScript, iTerm window control) entirely
and instead leans on three primitives that Linux users already have:

1. **Claude Code hooks** — for real-time session state.
2. **tmux** — for window management.
3. **Per-session JSON state files on disk** — for the source of truth.

## Components

```
~/.claude/settings.json
  hooks → src/claude_orchestrator/hooks/event_handler.sh
            │ on every PreToolUse / PostToolUse / Notification / Stop / …
            ▼
            $XDG_STATE_HOME/claude-orchestrator/sessions/<sid>.json   (mode 0600)
                                       ▲
                                       │ atomic read
                                       │
src/claude_orchestrator/
├── hooks/event_handler.sh     ← shell, set -u, sanitized PATH, jq --arg only
├── hooks/installer.py         ← installs/uninstalls hooks via settings.json
├── state/manager.py           ← scans + reads state files
├── state/reconciler.py        ← prunes dead sessions, fixes stuck waits
├── tmux/discover.py           ← walks /proc + tmux to find pane → claude pid
├── tmux/navigator.py          ← `tmux select-window` to jump to a session
├── tui/app.py                 ← Textual TUI dashboard
├── usage.py                   ← ccusage transcript-token aggregation
└── account_usage.py           ← server-anchored per-account 5h/7d limits
```

## Why hook-driven, not scraped

Earlier dashboards in this niche scrape Claude's terminal output (or worse,
control a specific terminal emulator). That breaks the moment you change
terminal, switch to Wayland, or run Claude headless. `cco` instead reads
the official hook events Claude Code already emits, so there is nothing
to "screen-scrape" and the dashboard works the same in any terminal.

## State file invariants

- **One JSON file per Claude session**, keyed by Claude's session ID.
- **0600 mode, parent dir 0700.** Never world-readable.
- **Atomic writes** — temp file + `rename(2)`. Readers never see a
  half-written file.
- **The hook is the only writer** during a session's lifetime; the
  reconciler is the only writer that can mark a file `DEAD`.

## Tmux integration

`cco` records the tmux pane each Claude session lives in (set by the
hook on every event, so it self-heals on `claude --resume`). Pressing
Enter on a row runs `tmux select-window -t <pane_id>` against the
user's current tmux client — no terminal-emulator-specific hacks
needed.

## Privacy

Everything stays on your machine. The only network call `cco` makes is
to `https://api.anthropic.com/api/oauth/usage` for per-account usage
anchoring; see [SECURITY.md](../SECURITY.md) for the full surface area.
