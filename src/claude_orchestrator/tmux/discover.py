"""Discover the tmux pane for each running claude process.

When a session is started outside cco's hook coverage (e.g. before `cco init`
was run, or the SessionStart hook missed the TMUX env var), the state file's
tmux_* fields stay null and `cco tui`'s Enter-to-jump can't help.

This module fixes that gap by walking /proc + `tmux list-panes` to
reconstruct the mapping (cwd, claude_pid) → (tmux_session, tmux_window,
tmux_pane). Use it on demand from the CLI (`cco refresh-tmux`) or
inline from the TUI when a jump misses.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Bound on parent-walk depth so a corrupt /proc entry can't loop forever.
MAX_ANCESTOR_DEPTH = 32


@dataclass(frozen=True)
class TmuxPaneInfo:
    """tmux fields we care about for navigation, plus the claude pid we
    matched against (for callers that want to verify liveness)."""

    tmux_session: str
    tmux_window: str
    tmux_pane: str
    claude_pid: int
    cwd: str


def has_tmux() -> bool:
    return shutil.which("tmux") is not None


def _list_tmux_panes() -> list[tuple[int, str, str, str]]:
    """Return [(pane_pid, session, window, pane_id), ...] for every pane
    on the running tmux server. Empty list on any failure."""
    try:
        out = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_pid}\t#S\t#W\t#{pane_id}"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return []
    if out.returncode != 0:
        return []

    panes: list[tuple[int, str, str, str]] = []
    for line in out.stdout.splitlines():
        try:
            pid_str, sess, win, pane_id = line.split("\t", 3)
            panes.append((int(pid_str), sess, win, pane_id))
        except (ValueError, IndexError):
            continue
    return panes


def _claude_pids() -> list[int]:
    """Return PIDs of every running `claude` process. Empty list if none."""
    try:
        out = subprocess.run(
            ["pgrep", "-x", "claude"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return []
    if out.returncode != 0:
        # pgrep returns 1 when no processes match — that's fine, just no claudes.
        return []
    return [int(p) for p in out.stdout.split() if p.strip().isdigit()]


def _read_proc_cwd(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def _read_proc_ppid(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("PPid:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1])
    except (OSError, ValueError):
        return None
    return None


def _walk_to_tmux_pane(
    start_pid: int, pane_pids: dict[int, tuple[str, str, str]]
) -> tuple[str, str, str] | None:
    """Walk parent PIDs up from start_pid; return the (session, window,
    pane_id) of the first ancestor that is a known tmux pane root, or None."""
    pid: int | None = start_pid
    for _ in range(MAX_ANCESTOR_DEPTH):
        if pid is None or pid <= 1:
            return None
        if pid in pane_pids:
            return pane_pids[pid]
        pid = _read_proc_ppid(pid)
    return None


def discover_panes() -> list[TmuxPaneInfo]:
    """Find every running claude process that lives inside a tmux pane.

    Empty list when tmux isn't installed, no claudes are running, or none
    of them are inside a pane (claude was started in a plain terminal).
    """
    if not has_tmux():
        return []

    panes = _list_tmux_panes()
    if not panes:
        return []
    pane_pids: dict[int, tuple[str, str, str]] = {
        pid: (sess, win, pane_id) for pid, sess, win, pane_id in panes
    }

    discovered: list[TmuxPaneInfo] = []
    for cpid in _claude_pids():
        cwd = _read_proc_cwd(cpid)
        if cwd is None:
            continue
        match = _walk_to_tmux_pane(cpid, pane_pids)
        if match is None:
            continue
        sess, win, pane = match
        discovered.append(
            TmuxPaneInfo(
                tmux_session=sess,
                tmux_window=win,
                tmux_pane=pane,
                claude_pid=cpid,
                cwd=cwd,
            )
        )
    return discovered


def enrich_state_files(state_dir: Path) -> int:
    """Walk `state_dir`, and for every state file whose tmux fields are
    None, look up the matching claude process and write tmux info back.

    Match priority:
      1. **claude_pid** (definitive). If the state file already records the
         claude pid (the handler walks up to find it), match by pid — this
         is unambiguous even when several sessions share a cwd.
      2. **cwd**, but ONLY when exactly one running claude has that cwd.
         If multiple discovered panes share a cwd, skip the file and wait
         for the next hook event (which will record claude_pid).

    Returns the number of state files updated.
    """
    import json

    if not state_dir.is_dir():
        return 0

    discovered = discover_panes()
    if not discovered:
        return 0

    by_pid: dict[int, TmuxPaneInfo] = {info.claude_pid: info for info in discovered}

    cwd_counts: dict[str, int] = {}
    for info in discovered:
        cwd_counts[info.cwd] = cwd_counts.get(info.cwd, 0) + 1
    by_unique_cwd: dict[str, TmuxPaneInfo] = {
        info.cwd: info for info in discovered if cwd_counts[info.cwd] == 1
    }

    updated = 0
    for path in state_dir.glob("*.json"):
        if path.name.startswith(".tmp"):
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue

        # Match priority 1: recorded claude_pid (unambiguous).
        match: TmuxPaneInfo | None = None
        recorded_pid = data.get("claude_pid")
        if isinstance(recorded_pid, int) and recorded_pid in by_pid:
            match = by_pid[recorded_pid]
        else:
            # Match priority 2: unique cwd.
            cwd = data.get("cwd")
            if cwd:
                match = by_unique_cwd.get(cwd)
        if match is None:
            continue

        # Only overwrite when fields are absent — don't clobber a fresher
        # mapping that the hook handler might have just written.
        changed = False
        for field in ("tmux_session", "tmux_window", "tmux_pane"):
            if not data.get(field):
                changed = True
        if not changed:
            continue

        data["tmux_session"] = match.tmux_session
        data["tmux_window"] = match.tmux_window
        data["tmux_pane"] = match.tmux_pane
        # Also record the discovered claude_pid if the file was missing it.
        if not data.get("claude_pid"):
            data["claude_pid"] = match.claude_pid

        # Atomic-rename write so a partial write can't corrupt the file
        # the watcher is reading concurrently.
        tmp = path.with_name(f".tmp.enrich.{path.name}")
        try:
            tmp.write_text(json.dumps(data))
            tmp.chmod(0o600)
            tmp.replace(path)
            updated += 1
        except OSError:
            with _suppress():
                tmp.unlink()
    return updated


class _suppress:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return isinstance(exc, OSError)
