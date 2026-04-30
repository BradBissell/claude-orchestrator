"""CLI entrypoint for `cco`.

Subcommand surface (P2 wires list/status/tmux-widget; init/uninstall/tui
remain stubbed for P3/P6).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

from claude_orchestrator import __version__
from claude_orchestrator.constants import STATUS_DISPLAY, AgentStatus
from claude_orchestrator.state.manager import StateManager

# tmux-widget tags for nord-ish palette so it pops in tmux status-right.
TMUX_COLOR = {
    AgentStatus.WORKING: "#A3BE8C",
    AgentStatus.IDLE: "#616E88",
    AgentStatus.WAITING_PERMISSION: "#BF616A",
    AgentStatus.WAITING_ANSWER: "#EBCB8B",
    AgentStatus.ERROR: "#B48EAD",
    AgentStatus.DEAD: "#3B4252",
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cco",
        description="claude-orchestrator — Linux-native dashboard for Claude Code sessions.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"cco {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    subparsers.add_parser("list", help="List all known Claude Code sessions")
    subparsers.add_parser("status", help="One-line summary for scripts")
    subparsers.add_parser("tmux-widget", help="Output for tmux status-right")

    init_p = subparsers.add_parser("init", help="Install hooks into ~/.claude/settings.json")
    init_p.add_argument("--dry-run", action="store_true", help="Print plan without writing")

    uninstall_p = subparsers.add_parser("uninstall", help="Remove cco hooks from settings.json")
    uninstall_p.add_argument("--dry-run", action="store_true", help="Print plan without writing")
    uninstall_p.add_argument(
        "--restore-backup",
        action="store_true",
        help="Rescue mode: replace settings.json with the most recent backup",
    )

    subparsers.add_parser(
        "refresh-tmux",
        help="Scan running claude processes and update each session's tmux pane mapping",
    )
    subparsers.add_parser("tui", help="Launch the Textual TUI dashboard (P6)")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help(sys.stderr)
        return 0

    if args.command == "list":
        return _cmd_list()
    if args.command == "status":
        return _cmd_status()
    if args.command == "tmux-widget":
        return _cmd_tmux_widget()
    if args.command == "init":
        return _cmd_init(dry_run=bool(args.dry_run))
    if args.command == "uninstall":
        return _cmd_uninstall(
            dry_run=bool(args.dry_run),
            restore_backup=bool(getattr(args, "restore_backup", False)),
        )
    if args.command == "refresh-tmux":
        return _cmd_refresh_tmux()
    if args.command == "tui":
        return _cmd_tui()

    print(
        f"cco: subcommand '{args.command}' is not implemented yet "
        f"(see docs/project-brief.md for phasing)",
        file=sys.stderr,
    )
    return 2


# --- list ------------------------------------------------------------------


def _cmd_list() -> int:
    """Print a rich table of all known sessions."""
    from rich.console import Console
    from rich.table import Table

    console = Console(highlight=False)
    agents = StateManager().scan()

    if not agents:
        console.print("[dim]No active sessions.[/dim]")
        return 0

    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("STATUS", justify="left", no_wrap=True)
    table.add_column("PROJECT", overflow="ellipsis", max_width=22)
    table.add_column("AGE", justify="right", no_wrap=True)
    table.add_column("LAST EVENT", overflow="ellipsis", max_width=20)
    table.add_column("TOOLS", justify="right", no_wrap=True)
    table.add_column("ERR", justify="right", no_wrap=True)
    table.add_column("CWD", overflow="ellipsis")
    table.add_column("SID", no_wrap=True)

    for a in agents:
        symbol, label, color = STATUS_DISPLAY[a.status]
        status_cell = f"[{color}]{symbol} {label}[/]"
        age = _human_age(a.last_event_time)
        err_cell = f"[red]{a.error_count}[/]" if a.error_count else "0"
        table.add_row(
            status_cell,
            a.project_name or "-",
            age,
            a.last_event,
            str(a.tool_count),
            err_cell,
            a.cwd,
            a.session_id[:8],
        )

    console.print(table)
    return 0


# --- status ---------------------------------------------------------------


def _cmd_status() -> int:
    """One-line summary suitable for shell-script consumption."""
    summary = StateManager().get_summary()
    line = summary.status_line()
    print(f"{line}  | total:{summary.total}")
    return 0


# --- tmux-widget ---------------------------------------------------------


def _cmd_tmux_widget() -> int:
    """Compact, colorised summary for `tmux status-right`.

    Output uses tmux format-string color escapes so it integrates cleanly
    with `set -g status-right '#(cco tmux-widget)'`.
    """
    summary = StateManager().get_summary()
    parts: list[str] = ["#[fg=#88C0D0]cco#[default]"]
    if summary.attention:
        if summary.waiting_permission:
            parts.append(
                _tmux_tag("PERM", summary.waiting_permission, AgentStatus.WAITING_PERMISSION)
            )
        if summary.waiting_answer:
            parts.append(_tmux_tag("WAIT", summary.waiting_answer, AgentStatus.WAITING_ANSWER))
        if summary.error:
            parts.append(_tmux_tag("ERR", summary.error, AgentStatus.ERROR))
    if summary.working:
        parts.append(_tmux_tag("W", summary.working, AgentStatus.WORKING))
    if summary.idle:
        parts.append(_tmux_tag("I", summary.idle, AgentStatus.IDLE))
    if summary.total == 0:
        parts.append("#[fg=#3B4252]·#[default]")
    print(" ".join(parts))
    return 0


def _tmux_tag(label: str, count: int, status: AgentStatus) -> str:
    color = TMUX_COLOR[status]
    return f"#[fg={color}]{label}:{count}#[default]"


# --- init / uninstall -----------------------------------------------------


def _cmd_init(*, dry_run: bool) -> int:
    """Install cco hooks into ~/.claude/settings.json."""
    if os.geteuid() == 0:
        print("cco: refusing to run as root.", file=sys.stderr)
        return 3
    from claude_orchestrator.hooks import installer

    plan = installer.install(dry_run=dry_run)
    print(plan.summary())
    if dry_run:
        print("\n[dry-run] no changes written.")
    elif plan.events_to_add:
        print("\nDone. Restart any active Claude Code sessions to pick up the new hooks.")
    return 0


def _cmd_uninstall(*, dry_run: bool, restore_backup: bool) -> int:
    """Remove cco hooks (or restore from latest backup)."""
    if os.geteuid() == 0:
        print("cco: refusing to run as root.", file=sys.stderr)
        return 3
    from claude_orchestrator.hooks import installer

    if restore_backup:
        if dry_run:
            backup = installer.latest_backup_path()
            print(f"would restore: {backup or '(no backup found)'}")
            return 0
        backup = installer.restore_backup()
        if backup is None:
            print("cco: no backup found to restore.", file=sys.stderr)
            return 4
        print(f"restored: {backup}")
        return 0

    plan = installer.uninstall(dry_run=dry_run)
    print(plan.summary())
    if dry_run:
        print("\n[dry-run] no changes written.")
    return 0


def _cmd_refresh_tmux() -> int:
    """Scan tmux + /proc to backfill tmux mappings on existing state files."""
    from claude_orchestrator.config import state_dir
    from claude_orchestrator.tmux.discover import discover_panes, enrich_state_files

    panes = discover_panes()
    if not panes:
        print("cco: no claude processes found inside tmux panes.", file=sys.stderr)
        return 0
    updated = enrich_state_files(state_dir())
    print(f"discovered {len(panes)} pane(s); updated {updated} state file(s).")
    return 0


# --- tui ------------------------------------------------------------------


def _cmd_tui() -> int:
    """Launch the Textual dashboard."""
    try:
        from claude_orchestrator.tui.app import run as run_tui
    except ImportError as exc:
        print(
            "cco: TUI extras not installed. Reinstall with `pipx install -e '.[tui]'` "
            f"or `pip install textual`. ({exc})",
            file=sys.stderr,
        )
        return 5
    return run_tui()


# --- helpers --------------------------------------------------------------


def _human_age(iso_ts: str) -> str:
    """Render an ISO-8601 timestamp as a compact age string ('3m', '2h', '1d')."""
    if not iso_ts:
        return "-"
    try:
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except ValueError:
        return "?"
    now = datetime.now(UTC)
    delta = max(0, int((now - ts).total_seconds()))
    if delta < 60:
        return f"{delta}s"
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86400:
        return f"{delta // 3600}h"
    return f"{delta // 86400}d"


if __name__ == "__main__":
    raise SystemExit(main())
