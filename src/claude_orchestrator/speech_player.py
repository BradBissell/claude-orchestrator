"""TTS playback queue + subprocess manager owned by the cco TUI.

When cco is the speech owner (see CLI `cco speech install`), this module
runs the kokoro pipeline directly instead of relying on the user's
~/.claude/hooks/tts-speak-response. The benefit: a real FIFO queue with
same-session dedup — overlapping responses from multiple Claude sessions
play one at a time instead of stomping on each other.

Behavioural rules (FAQ):
  * **Different sessions**: enqueue, FIFO. The bar shows the current
    speaker and the waiting queue.
  * **Same session, queued**: the new entry replaces the old one in the
    queue (we never want to read the stale reply once a fresher one
    exists for the same conversation).
  * **Same session, currently playing**: PREEMPT. Kill the in-flight
    subprocess and start the new entry. The user explicitly chose this
    semantic — old reply is stale, new content supersedes.
  * **`stop` event** (e.g. UPS fired in that session): drop matching sid
    from queue + terminate playback if it's the current item.
  * **Queue cap**: MAX_QUEUE entries. When 30 sessions all Stop in a
    burst, oldest gets dropped — better than letting the queue grow
    unbounded.

Design discipline:
  * Subprocess management is isolated behind a `Spawner` callable so the
    queue logic is unit-testable without spawning kokoro.
  * `tick()` is the single entry point — it polls the SpeechWatcher,
    routes events into the queue, and reaps finished playback. Designed
    to be driven from a Textual interval; cheap on every tick when idle.
  * No threads, no asyncio events. Subprocess lifecycle uses Popen +
    poll() so Textual's event loop owns scheduling.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from claude_orchestrator.speech import SpeechWatcher, estimated_duration_ms

MAX_QUEUE = 5

# Default playback command: pipe `text` on stdin, ducks other audio, plays
# via kokoro+paplay. Mirrors the user's existing tts-speak-response wiring
# so behaviour is identical when cco is the owner.
_DEFAULT_TTS_COMMAND = str(
    Path(os.environ.get("HOME", "")) / ".local/share/kokoro-tts/play-ducked.sh"
)


def default_tts_command() -> list[str] | None:
    """Resolve the playback command. None if unconfigured / missing on disk."""
    raw = os.environ.get("CCO_TTS_COMMAND")
    if raw:
        return [raw]
    if Path(_DEFAULT_TTS_COMMAND).is_file():
        return [_DEFAULT_TTS_COMMAND]
    return None


@dataclass
class QueueItem:
    """One queued (or currently-playing) speech request."""

    session_id: str
    text: str
    sentences: list[str] = field(default_factory=list)
    speed: float = 1.3
    started_ms: int = 0  # original Stop hook timestamp
    enqueued_ms: int = 0  # when cco accepted it (used for queue ordering)


@dataclass
class _PlaybackHandle:
    item: QueueItem
    proc: subprocess.Popen | None  # None when playback couldn't start


# Spawner signature: takes a QueueItem, returns a Popen (or None if it
# couldn't start). Injected so tests can use a fake.
Spawner = Callable[[QueueItem], "subprocess.Popen | None"]


def _real_spawner(cmd: list[str]) -> Spawner:
    """Returns a Spawner that runs `cmd` with the item's text on stdin."""

    def spawn(item: QueueItem) -> subprocess.Popen | None:
        try:
            return subprocess.Popen(  # noqa: S603 - command is operator-controlled
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # New session so a SIGTERM hits the whole pipeline (kokoro
                # speak.py + paplay), not just the wrapper script. Without
                # this, killing the wrapper leaves paplay holding the audio
                # device for the next sentence and we get audible overlap
                # on preempt.
                start_new_session=True,
            )
        except (OSError, ValueError):
            return None

    def spawn_and_feed(item: QueueItem) -> subprocess.Popen | None:
        proc = spawn(item)
        if proc is None or proc.stdin is None:
            return proc
        try:
            proc.stdin.write(item.text.encode("utf-8"))
            proc.stdin.close()
        except (OSError, BrokenPipeError):
            # Subprocess died before we could finish writing — Popen will
            # surface the failure on poll(), so just return.
            pass
        return proc

    return spawn_and_feed


class SpeechPlayer:
    """FIFO queue with same-session dedup and preempt-on-collision."""

    def __init__(
        self,
        spawner: Spawner | None = None,
        watcher: SpeechWatcher | None = None,
        max_queue: int = MAX_QUEUE,
    ) -> None:
        if spawner is None:
            cmd = default_tts_command()
            spawner = _real_spawner(cmd) if cmd else _null_spawner
        self._spawn = spawner
        self._watcher = watcher  # optional — set by tick() integration
        self._max_queue = max_queue
        self._queue: list[QueueItem] = []
        self._current: _PlaybackHandle | None = None

    # ---- introspection (used by SpeechBar / tests) -----------------------

    @property
    def now_playing(self) -> QueueItem | None:
        return self._current.item if self._current else None

    @property
    def queue_snapshot(self) -> list[QueueItem]:
        return list(self._queue)

    # ---- public API ------------------------------------------------------

    def enqueue(self, item: QueueItem) -> None:
        """Apply the queue rules described in this module's docstring.

        Behaviour summary:
          - Same sid currently playing → preempt (kill + start new).
          - Same sid in queue → replace that entry (no new audio yet).
          - Otherwise → append; if nothing is playing, start now.
        """
        item.enqueued_ms = item.enqueued_ms or _now_ms()

        if self._current and self._current.item.session_id == item.session_id:
            self._terminate_current()
            self._start(item)
            return

        # Same-session dedup in the waiting queue.
        self._queue = [q for q in self._queue if q.session_id != item.session_id]

        if self._current is None:
            self._start(item)
            return

        self._queue.append(item)
        # Cap from the front so the most-recent N stay (oldest drop first).
        if len(self._queue) > self._max_queue:
            self._queue = self._queue[-self._max_queue :]

    def stop(self, session_id: str) -> None:
        """A `stop` event arrived (UPS fired). Drop from queue + kill if
        currently playing."""
        self._queue = [q for q in self._queue if q.session_id != session_id]
        if self._current and self._current.item.session_id == session_id:
            self._terminate_current()
            self._advance()

    def stop_all(self) -> None:
        """Tear everything down (called on app shutdown)."""
        self._queue.clear()
        self._terminate_current()

    def tick(self) -> None:
        """Poll the watcher and reap finished playback. Driven by Textual's
        interval timer (≈200ms cadence is plenty)."""
        if self._watcher is not None:
            for ev in self._watcher.poll():
                self._route_event(ev)
        self._reap_if_finished()

    # ---- internals -------------------------------------------------------

    def _route_event(self, ev: dict) -> None:
        sid = ev.get("session_id")
        if not isinstance(sid, str):
            return
        kind = ev.get("event")
        if kind == "start":
            text = ev.get("text") or ""
            sentences_raw = ev.get("sentences") or []
            sentences = [s for s in sentences_raw if isinstance(s, str)]
            try:
                speed = float(ev.get("speed") or 1.3)
            except (TypeError, ValueError):
                speed = 1.3
            self.enqueue(
                QueueItem(
                    session_id=sid,
                    text=text,
                    sentences=sentences,
                    speed=speed,
                )
            )
        elif kind == "stop":
            self.stop(sid)

    def _start(self, item: QueueItem) -> None:
        proc = self._spawn(item)
        self._current = _PlaybackHandle(item=item, proc=proc)
        # If the spawner returned None (kokoro missing, etc.) we treat it
        # as instant-completion so the queue still drains. The bar will
        # show the item briefly and then advance.

    def _terminate_current(self) -> None:
        if self._current is None:
            return
        proc = self._current.proc
        if proc is not None and proc.poll() is None:
            try:
                # Kill the whole process group — kokoro spawns paplay and
                # without start_new_session above we'd leave the audio
                # subprocess running.
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                with _suppress_lookup():
                    proc.terminate()
            try:
                proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                with _suppress_lookup():
                    os.killpg(proc.pid, signal.SIGKILL)
        self._current = None

    def _reap_if_finished(self) -> None:
        if self._current is None:
            return
        proc = self._current.proc
        item = self._current.item
        if proc is None:
            # Spawner returned None (kokoro missing or in headless tests).
            # Keep the item "playing" for its estimated duration so the
            # bar still mirrors what WOULD be audible — instant-reap would
            # make new starts disappear before the user can read them.
            est_end = (item.enqueued_ms or _now_ms()) + estimated_duration_ms(
                item.text, item.sentences, speed=item.speed
            )
            if _now_ms() >= est_end:
                self._current = None
                self._advance()
            return
        if proc.poll() is not None:
            self._current = None
            self._advance()

    def _advance(self) -> None:
        """Pop the next queued item and start it."""
        if not self._queue:
            return
        nxt = self._queue.pop(0)
        self._start(nxt)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    return int(time.time() * 1000)


def _null_spawner(item: QueueItem) -> subprocess.Popen | None:
    """Used when no kokoro command is available — playback silently
    completes, preserving queue semantics for testing/headless CI."""
    return None


class _suppress_lookup:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return isinstance(exc, (ProcessLookupError, PermissionError, OSError))


def kokoro_available() -> bool:
    """Public helper for the CLI to surface a friendly error when the user
    asks cco to own playback but the kokoro pipeline isn't installed."""
    cmd = default_tts_command()
    if cmd is None:
        return False
    return shutil.which(cmd[0]) is not None or Path(cmd[0]).is_file()
