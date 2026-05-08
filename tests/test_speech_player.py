"""Unit tests for the SpeechPlayer queue logic.

Subprocess management is exercised by injecting a fake spawner — we don't
spawn kokoro in the test suite (no audio device, no model file, no point).
Test the *behavioural contracts* (FIFO, dedup, preempt, stop) and trust
the real spawner to do its job in production.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_orchestrator import speech
from claude_orchestrator.speech_player import (
    MAX_QUEUE,
    QueueItem,
    SpeechPlayer,
)


class FakeProc:
    """Minimal Popen stand-in for queue tests."""

    def __init__(self) -> None:
        self.terminated = False
        self.killed = False
        self.pid = 99999  # the test stubs os.killpg so this is never signalled
        self._returncode: int | None = None

    @property
    def returncode(self) -> int | None:
        return self._returncode

    def poll(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        self.terminated = True
        self._returncode = -15

    def kill(self) -> None:
        self.killed = True
        self._returncode = -9

    def wait(self, timeout: float = 0) -> int:
        if self._returncode is None:
            self._returncode = 0
        return self._returncode

    def finish(self) -> None:
        """Test-only: simulate the subprocess exiting cleanly."""
        self._returncode = 0


@pytest.fixture
def proc_log() -> list[FakeProc]:
    return []


@pytest.fixture
def player(proc_log: list[FakeProc]) -> SpeechPlayer:
    def spawner(_item: QueueItem) -> FakeProc:
        proc = FakeProc()
        proc_log.append(proc)
        return proc  # type: ignore[return-value]

    # Patch os.killpg in the player module so terminate doesn't try to
    # signal a real process group on the FakeProc's bogus pid.
    import claude_orchestrator.speech_player as sp

    sp.os.killpg = lambda *_args, **_kwargs: None  # type: ignore[assignment]
    return SpeechPlayer(spawner=spawner)


def _item(sid: str, text: str = "Hello.") -> QueueItem:
    return QueueItem(session_id=sid, text=text, sentences=[text])


# ---- FIFO across different sessions --------------------------------------


def test_first_enqueue_starts_immediately(player: SpeechPlayer, proc_log: list[FakeProc]) -> None:
    player.enqueue(_item("a"))
    assert player.now_playing is not None
    assert player.now_playing.session_id == "a"
    assert player.queue_snapshot == []
    assert len(proc_log) == 1


def test_second_enqueue_for_different_session_queues(
    player: SpeechPlayer, proc_log: list[FakeProc]
) -> None:
    player.enqueue(_item("a"))
    player.enqueue(_item("b"))
    assert player.now_playing is not None
    assert player.now_playing.session_id == "a"
    assert [q.session_id for q in player.queue_snapshot] == ["b"]
    assert len(proc_log) == 1  # only `a` started spawning


def test_finished_playback_advances_queue(player: SpeechPlayer, proc_log: list[FakeProc]) -> None:
    player.enqueue(_item("a"))
    player.enqueue(_item("b"))
    player.enqueue(_item("c"))
    proc_log[0].finish()
    player.tick()
    assert player.now_playing is not None
    assert player.now_playing.session_id == "b"
    assert [q.session_id for q in player.queue_snapshot] == ["c"]


def test_queue_drains_in_order(player: SpeechPlayer, proc_log: list[FakeProc]) -> None:
    for sid in ("a", "b", "c"):
        player.enqueue(_item(sid))
    seen = [player.now_playing.session_id]  # type: ignore[union-attr]
    while player.now_playing is not None:
        proc_log[-1].finish()
        player.tick()
        if player.now_playing is not None:
            seen.append(player.now_playing.session_id)
    assert seen == ["a", "b", "c"]


# ---- same-session semantics ----------------------------------------------


def test_same_session_in_queue_replaces_existing_entry(
    player: SpeechPlayer, proc_log: list[FakeProc]
) -> None:
    """A new Stop for sid `b` while `b` is already queued must replace the
    queued entry, not duplicate it. We never want to read a stale reply
    for a conversation that just got a fresher response."""
    player.enqueue(_item("a"))
    player.enqueue(_item("b", text="OLD"))
    player.enqueue(_item("b", text="NEW"))
    assert [q.session_id for q in player.queue_snapshot] == ["b"]
    assert player.queue_snapshot[0].text == "NEW"
    # `a` is still playing; not preempted.
    assert player.now_playing.session_id == "a"  # type: ignore[union-attr]


def test_same_session_currently_playing_preempts(
    player: SpeechPlayer, proc_log: list[FakeProc]
) -> None:
    """User chose preempt-on-collision: a new response for the in-flight
    session kills the audio and starts the new one."""
    player.enqueue(_item("a", text="OLD"))
    old_proc = proc_log[-1]
    player.enqueue(_item("a", text="NEW"))
    # Previous proc was terminated.
    assert old_proc.terminated or old_proc.poll() is not None
    # New playback started for the same session.
    assert player.now_playing.session_id == "a"  # type: ignore[union-attr]
    assert player.now_playing.text == "NEW"  # type: ignore[union-attr]
    assert len(proc_log) == 2


def test_preempt_does_not_disturb_queue(player: SpeechPlayer, proc_log: list[FakeProc]) -> None:
    player.enqueue(_item("a", text="OLD"))
    player.enqueue(_item("b"))
    player.enqueue(_item("a", text="NEW"))  # preempts a; b stays queued
    assert player.now_playing.text == "NEW"  # type: ignore[union-attr]
    assert [q.session_id for q in player.queue_snapshot] == ["b"]


# ---- stop event ----------------------------------------------------------


def test_stop_event_drops_queued_session(player: SpeechPlayer) -> None:
    player.enqueue(_item("a"))
    player.enqueue(_item("b"))
    player.enqueue(_item("c"))
    player.stop("b")
    assert [q.session_id for q in player.queue_snapshot] == ["c"]
    assert player.now_playing.session_id == "a"  # type: ignore[union-attr]


def test_stop_event_kills_current_and_advances(
    player: SpeechPlayer, proc_log: list[FakeProc]
) -> None:
    """Used when UPS fires for the speaking session — kill the audio and
    move on to whatever's next in the queue."""
    player.enqueue(_item("a"))
    player.enqueue(_item("b"))
    proc_for_a = proc_log[-1]
    player.stop("a")
    assert proc_for_a.terminated or proc_for_a.poll() is not None
    assert player.now_playing.session_id == "b"  # type: ignore[union-attr]


# ---- queue cap -----------------------------------------------------------


def test_queue_caps_at_max_and_drops_oldest(
    player: SpeechPlayer,
) -> None:
    player.enqueue(_item("playing"))  # currently playing
    for i in range(MAX_QUEUE + 5):
        player.enqueue(_item(f"q{i}"))
    snap = player.queue_snapshot
    assert len(snap) == MAX_QUEUE
    # Oldest were dropped — we keep the most recent N.
    expected_tail = [f"q{i}" for i in range(5, MAX_QUEUE + 5)]
    assert [q.session_id for q in snap] == expected_tail


# ---- watcher integration -------------------------------------------------


def test_tick_routes_log_events_into_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, proc_log: list[FakeProc]
) -> None:
    """The player's tick reads new log records via SpeechWatcher and
    routes them into the queue, so the TUI just needs to call tick()."""
    log = tmp_path / "speech.jsonl"
    monkeypatch.setenv("CCO_SPEECH_LOG", str(log))

    def spawner(_item: QueueItem) -> FakeProc:
        proc = FakeProc()
        proc_log.append(proc)
        return proc  # type: ignore[return-value]

    import claude_orchestrator.speech_player as sp

    sp.os.killpg = lambda *_a, **_k: None  # type: ignore[assignment]

    watcher = speech.SpeechWatcher(log)
    player = SpeechPlayer(spawner=spawner, watcher=watcher)

    speech.append_start("alpha", "Hello from alpha.")
    speech.append_start("beta", "Hello from beta.")

    player.tick()
    assert player.now_playing is not None
    assert player.now_playing.session_id == "alpha"
    assert [q.session_id for q in player.queue_snapshot] == ["beta"]


def test_tick_advances_after_subprocess_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, proc_log: list[FakeProc]
) -> None:
    log = tmp_path / "speech.jsonl"
    monkeypatch.setenv("CCO_SPEECH_LOG", str(log))

    def spawner(_item: QueueItem) -> FakeProc:
        proc = FakeProc()
        proc_log.append(proc)
        return proc  # type: ignore[return-value]

    import claude_orchestrator.speech_player as sp

    sp.os.killpg = lambda *_a, **_k: None  # type: ignore[assignment]

    watcher = speech.SpeechWatcher(log)
    player = SpeechPlayer(spawner=spawner, watcher=watcher)

    speech.append_start("alpha", "First.")
    speech.append_start("beta", "Second.")
    player.tick()
    assert player.now_playing.session_id == "alpha"  # type: ignore[union-attr]
    proc_log[-1].finish()
    player.tick()
    assert player.now_playing.session_id == "beta"  # type: ignore[union-attr]


# ---- watcher unit -------------------------------------------------------


def test_watcher_returns_only_new_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "speech.jsonl"
    monkeypatch.setenv("CCO_SPEECH_LOG", str(log))
    speech.append_start("alpha", "Old.")
    watcher = speech.SpeechWatcher(log)
    # Initial position is end-of-file → first poll should return nothing.
    assert watcher.poll() == []
    speech.append_start("beta", "New.")
    events = watcher.poll()
    assert len(events) == 1
    assert events[0]["session_id"] == "beta"
    # Subsequent poll with no new writes returns []
    assert watcher.poll() == []


def test_watcher_recovers_from_truncation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "speech.jsonl"
    monkeypatch.setenv("CCO_SPEECH_LOG", str(log))
    speech.append_start("a", "A.")
    speech.append_start("b", "B.")
    watcher = speech.SpeechWatcher(log)
    # Simulate the truncation pass: rewrite file with just one short line.
    log.write_text(
        '{"event":"start","ts":"2026-04-29T10:00:00.000000Z",'
        '"session_id":"c","text":"C.","sentences":["C."],"speed":1.0}\n'
    )
    events = watcher.poll()
    assert len(events) == 1
    assert events[0]["session_id"] == "c"
