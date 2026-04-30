# claude-orchestrator (`cco`)

Linux-native dashboard + lifecycle orchestrator for Claude Code sessions.

> **Status: pre-v0 (P0 bootstrap).** Not installable yet. See `docs/project-brief.md`.

## What it is

`cco` watches every Claude Code session you have running and tells you, at a
glance, which ones need your attention right now (permission requested, error,
waiting on user input) and which are still working away. It's a Linux-native
successor to [`clorch`](https://github.com/androsovm/clorch) — same hook-driven
architecture, but built from the start to work on Wayland + ghostty + tmux,
without the macOS-only AppleScript navigation layer that breaks on Linux.

## Why it exists

Running 10+ Claude Code sessions in parallel is normal now. tmux can show you
all of them, but tmux can't tell you that session 7 is currently blocked on a
permission prompt while the other 9 are still working — the kernel of need
that `cco` solves.

`clorch` solved this on macOS. On Ubuntu+Wayland+ghostty, half its features
silently no-op: terminal-tab activation uses AppleScript, the activity
collector never runs, state lives in `/tmp` and gets wiped on reboot, sound
alerts don't play. `cco` rewrites those parts for Linux while reusing
clorch's solid foundation (hook installer discipline, atomic-write pattern,
state schema, tmux navigator).

## Design highlights

- **Hook-driven**: every state transition comes from a Claude Code hook event.
  No tmux output scraping. No process introspection. No polling.
- **POSIX shell hook handler** with per-session `flock`, fail-OPEN error
  handling (a buggy hook never blocks claude), atomic state writes.
- **State persists** in `$XDG_STATE_HOME/claude-orchestrator/sessions/`
  (default `~/.local/state/claude-orchestrator/sessions/`), survives reboots,
  files mode 0600.
- **Auto-approve via hook return value**, not keystroke injection. Rules
  engine can answer permission prompts before the user sees the dialog.
- **tmux is the only navigation surface.** No AppleScript, no X11, no
  Wayland window-poking. If a session isn't in tmux, navigation is a no-op
  with a clear message.
- **Same CLI shape as clorch** to minimise muscle-memory friction:
  `cco`, `cco list`, `cco status`, `cco tmux-widget`, `cco init`,
  `cco uninstall`.

## Installation

```bash
# Pre-v0 — not yet installable. Once P0 is solid:
pipx install --editable ~/projects/claude-orchestrator
cco --version
```

## Roadmap

See `docs/project-brief.md` for the full plan. In short:

- **v0 (5 days)** — bash-only hook + state writer + read-only CLI + tmux config
- **v0 gate** — 1 week of dogfood before committing to v1
- **v1 (~8.5 days)** — Textual TUI, reconciliation, libnotify, rules engine, claude-resume-recent integration
- **v1.x** — activity collector, sparklines

## License

MIT. Same as clorch — patches are friendly to backport upstream.

## Contributing

Pre-v0; not yet accepting external contributions. Once v1 ships and the repo
goes public, see `CONTRIBUTING.md`.
