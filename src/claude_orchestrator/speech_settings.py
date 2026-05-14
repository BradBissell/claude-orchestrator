"""Persistent settings for the cco speech subsystem.

Three independent control surfaces, resolved in priority order:

  1. **Env var** ``CCO_TTS_ENABLED`` (``0``/``1``/``true``/``false``/``on``/``off``)
     — wins over everything; lets you launch with a one-shot override
     without touching disk. Useful for ``CCO_TTS_ENABLED=0 cco tui`` when
     you're in a meeting.
  2. **Persisted file** at ``$XDG_CONFIG_HOME/claude-orchestrator/speech.json``
     — what the TUI's ``m`` hotkey and the ``cco speech enable|disable``
     CLI write. Survives restarts.
  3. **Default**: enabled iff a kokoro pipeline is detected on disk.
     A user without TTS installed should never have cco try to play
     audio just because they opened the dashboard.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

ENV_VAR = "CCO_TTS_ENABLED"


class SettingsSource(Enum):
    """Which layer determined the effective state — surfaced by `cco speech
    status` so users can debug \"why is it on/off?\"."""

    ENV = "env"
    FILE = "file"
    DEFAULT = "default"


@dataclass(frozen=True)
class SpeechSettings:
    enabled: bool
    source: SettingsSource
    # Rolling-average chars/sec at speed=1, learned from observed playback
    # durations. None until cco has at least one natural completion. Used
    # by speech.py's rate function so the karaoke + progress bar advance
    # at YOUR kokoro's actual reading speed instead of a hardcoded guess.
    calibrated_chars_per_sec: float | None = None


def settings_path() -> Path:
    """Where the persisted state lives. Honors ``CCO_CONFIG_DIR`` (sandboxes
    in tests) and falls back to XDG conventions."""
    raw = os.environ.get("CCO_CONFIG_DIR")
    if raw:
        return Path(raw).expanduser() / "speech.json"
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg).expanduser() / "claude-orchestrator" / "speech.json"


def _parse_bool(s: str | None) -> bool | None:
    if s is None:
        return None
    v = s.strip().lower()
    if v in {"1", "true", "yes", "on", "enabled"}:
        return True
    if v in {"0", "false", "no", "off", "disabled"}:
        return False
    return None


def load() -> SpeechSettings:
    """Resolve the effective TTS-enabled state + load any persisted
    calibration. Calibration is read from the file regardless of which
    layer (env/file/default) decides the enabled flag — env override
    only governs whether cco SPEAKS, not how it estimates rate."""
    file_data: dict[str, Any] = {}
    p = settings_path()
    if p.is_file():
        try:
            raw = json.loads(p.read_text())
            if isinstance(raw, dict):
                file_data = raw
        except (OSError, ValueError, TypeError):
            # Corrupt config → treat as empty. Next save fixes it.
            pass

    calibrated = _coerce_calibrated_rate(file_data.get("calibrated_chars_per_sec"))

    env_val = _parse_bool(os.environ.get(ENV_VAR))
    if env_val is not None:
        return SpeechSettings(
            enabled=env_val,
            source=SettingsSource.ENV,
            calibrated_chars_per_sec=calibrated,
        )

    if "enabled" in file_data:
        return SpeechSettings(
            enabled=bool(file_data["enabled"]),
            source=SettingsSource.FILE,
            calibrated_chars_per_sec=calibrated,
        )

    return SpeechSettings(
        enabled=_default_enabled(),
        source=SettingsSource.DEFAULT,
        calibrated_chars_per_sec=calibrated,
    )


def _coerce_calibrated_rate(value: object) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float, str)):
        return None
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    if rate <= 0 or rate > 100:
        # Way outside any plausible TTS rate — treat as garbage.
        return None
    return rate


def save(
    enabled: bool | None = None,
    *,
    calibrated_chars_per_sec: float | None = None,
    clear_calibration: bool = False,
) -> Path:
    """Atomically merge updates into the config file.

    Only the kwargs you pass get written; everything else is preserved.
    `clear_calibration=True` removes the calibrated_chars_per_sec field
    (used by `cco speech reset-calibration`).
    """
    p = settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)

    # Preserve existing fields so partial updates don't drop them.
    current: dict[str, Any] = {}
    if p.is_file():
        try:
            raw = json.loads(p.read_text())
            if isinstance(raw, dict):
                current = raw
        except (OSError, ValueError, TypeError):
            pass

    if enabled is not None:
        current["enabled"] = bool(enabled)
    if clear_calibration:
        current.pop("calibrated_chars_per_sec", None)
    elif calibrated_chars_per_sec is not None:
        current["calibrated_chars_per_sec"] = float(calibrated_chars_per_sec)

    payload = json.dumps(current, indent=2) + "\n"
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        tmp.write_text(payload)
        tmp.chmod(0o600)
        os.replace(tmp, p)
    except OSError:
        if tmp.exists():
            import contextlib

            with contextlib.suppress(OSError):
                tmp.unlink()
        raise
    return p


def _default_enabled() -> bool:
    """Default to ON only if kokoro looks installed.

    Imported lazily to avoid a circular dependency with speech_player
    (which imports from speech.py, which we want to keep import-cheap).
    """
    try:
        from claude_orchestrator.speech_player import kokoro_available

        return kokoro_available()
    except ImportError:
        return False
