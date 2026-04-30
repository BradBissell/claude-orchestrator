"""tmux navigation: jump to the pane/window for a given AgentState.

This is the only navigation surface cco supports. There's no ghostty
automation, no X11 window-poking, no AppleScript. If the session isn't in a
tmux pane (because the user runs claude in a plain terminal), navigation
returns NotInTmux and the caller can show a hint.

The functions here shell out to `tmux` directly — fast, no library deps,
matches the reference clorch's pure-tmux navigator (~85% lifted, with
defensive checks added).
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum

from claude_orchestrator.state.models import AgentState


class JumpResult(Enum):
    """Outcome of an attempt to jump to a session's tmux pane."""

    OK = "ok"
    NO_TMUX_INFO = "no_tmux_info"  # state file lacks tmux_* fields
    SESSION_NOT_FOUND = "session_not_found"  # tmux doesn't have that session anymore
    TMUX_MISSING = "tmux_missing"  # tmux not on PATH
    FAILED = "failed"  # something else went wrong (subprocess error, etc.)


@dataclass(frozen=True)
class JumpOutcome:
    result: JumpResult
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.result is JumpResult.OK


def _run_tmux(
    *args: str, check: bool = False, timeout: float = 2.0
) -> subprocess.CompletedProcess[str]:
    """Invoke tmux with args. Returns the completed process. Never raises on
    timeout / non-zero unless check=True; that's the caller's job."""
    return subprocess.run(
        ["tmux", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )


def has_tmux() -> bool:
    return shutil.which("tmux") is not None


def _session_exists(name: str) -> bool:
    proc = _run_tmux("has-session", "-t=" + name)
    return proc.returncode == 0


def jump_to(state: AgentState) -> JumpOutcome:
    """Switch the user's current tmux client to the session/window/pane the
    given AgentState was last seen in."""
    if not has_tmux():
        return JumpOutcome(JumpResult.TMUX_MISSING, "tmux not on PATH")

    sess = state.tmux_session
    win = state.tmux_window
    pane = state.tmux_pane

    if not sess or not win:
        return JumpOutcome(
            JumpResult.NO_TMUX_INFO,
            "this session never reported a tmux pane — start it inside tmux to enable jump",
        )

    if not _session_exists(sess):
        return JumpOutcome(
            JumpResult.SESSION_NOT_FOUND,
            f"tmux session '{sess}' no longer exists",
        )

    target_window = f"{sess}:{win}"
    proc = _run_tmux("select-window", "-t", target_window)
    if proc.returncode != 0:
        return JumpOutcome(
            JumpResult.FAILED,
            f"select-window failed: {proc.stderr.strip() or 'no stderr'}",
        )

    if pane:
        # %42 / 0.0 / etc. — only set the pane if we have one. Failures here
        # are non-fatal (we already moved the window).
        _run_tmux("select-pane", "-t", pane)

    return JumpOutcome(JumpResult.OK)
