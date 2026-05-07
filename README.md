# cco — Claude Code Orchestrator for Linux

[![CI](https://github.com/BradBissell/claude-orchestrator/actions/workflows/ci.yml/badge.svg)](https://github.com/BradBissell/claude-orchestrator/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

A Linux-native TUI that watches every Claude Code session you have
running and tells you — at a glance — which ones need your attention,
which are still working, and which went idle.

<img width="2806" height="1972" alt="image" src="https://github.com/user-attachments/assets/50f4b678-2da8-4ee1-b52e-3d148ce4ae7c" />

## Why

Running 10+ Claude Code sessions in parallel is normal now. tmux shows
you all of them, but tmux can't tell you that session 7 is blocked on
a permission prompt while the other nine are still working. `cco`
solves exactly that — and presses Enter to jump to the right tmux
window.

## Install

```bash
pipx install cco
cco init           # installs the hooks into ~/.claude/settings.json
cco                # launches the TUI dashboard
```

(Or use `pip install --user cco` / `uv tool install cco`.)

## Quickstart

1. **`cco init`** — adds Claude Code hooks to `~/.claude/settings.json`
   so every session reports its state. The original settings file is
   backed up; `cco uninstall` cleanly removes them.
2. **`cco`** — opens the TUI. Use `j`/`k` or arrow keys to navigate,
   `/` to filter, `Enter` to jump to a session's tmux pane, `x` to
   kill, `?` for the full keymap.
3. **`cco list`** — script-friendly one-line-per-session status, for
   tmux status-right widgets or shell scripts.

See [`docs/getting-started.md`](docs/getting-started.md) for a longer
walkthrough.

## Highlights

- **Hook-driven, not scraped.** State comes from official Claude Code
  hook events — no terminal-output parsing, no AppleScript, no
  Wayland window-poking. Works the same in Ghostty, Alacritty, kitty,
  GNOME Terminal, or under `mosh`.
- **tmux-native navigation.** Every session is mapped to its tmux
  pane on every event, so `claude --resume` after a closed window
  self-heals. Pressing Enter does `tmux select-window -t <pane>`
  against your current client.
- **Per-session state on disk.** `$XDG_STATE_HOME/claude-orchestrator/`,
  mode 0600, atomic writes. Surviving a reboot is a feature.
- **Per-account 5h / 7d usage strip.** Anchors against the official
  `/api/oauth/usage` endpoint, then extrapolates with local ccusage
  deltas — accurate without hammering the API.
- **POSIX-shell hook handler** with `set -u`, sanitized PATH, jq
  `--arg` everywhere, per-session flock, and fail-OPEN error handling
  (a buggy hook never blocks Claude).
- **Auto-approve via hook return value**, not keystroke injection.
  Rules engine answers permission prompts before the dialog renders.

## Design

[`docs/architecture.md`](docs/architecture.md) walks through the
components. Short version: the hook script writes JSON state, the
TUI reads it. There is no daemon.

## Privacy & security

`cco` is a local tool. Nothing leaves your machine except for one
authenticated call to `https://api.anthropic.com/api/oauth/usage` to
compute per-account usage anchors. Full surface area in
[`SECURITY.md`](SECURITY.md).

## Requirements

- Linux (any modern distro; tested on Ubuntu 24.04)
- Python 3.11+
- tmux 3.2+
- Claude Code installed and at least one session run

## Contributing

Bug reports and PRs welcome. See [`CONTRIBUTING.md`](CONTRIBUTING.md)
for dev setup, testing conventions, and PR guidelines. Security
issues: see [`SECURITY.md`](SECURITY.md).

## License

MIT. Inspired by [`clorch`](https://github.com/androsovm/clorch) (the
macOS-only ancestor); patches that improve cross-platform support
upstream are encouraged.
