"""Hook installer: mutates ~/.claude/settings.json to register cco's hooks.

Discipline:
- Atomic writes (tempfile + fsync + os.replace) — never leave a half-written file.
- Backup before any mutation. Keep last 3 rotations.
- Coexists with the user's other hooks (gsd-*, cbm-*, …) by matching on our
  command path; we only ever add or remove entries that point at *our*
  event_handler.sh. Other entries are untouched.
- `--dry-run` prints the planned diff without writing.
- `--restore-backup` rescue mode: replace settings.json with the latest backup.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_orchestrator.config import claude_settings_path, hook_handler_path

# Hook events cco subscribes to. Order matches Claude Code's documented
# lifecycle so a user reading settings.json can scan it logically.
CCO_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "Notification",
    "PermissionRequest",
    "PermissionDenied",
    "Stop",
    "StopFailure",
    "SessionEnd",
    "SubagentStart",
    "SubagentStop",
)

# Maximum number of rotated backups to retain.
BACKUP_KEEP = 3


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstallPlan:
    """What `init` *would* do, separated from doing it (for --dry-run)."""

    settings_path: Path
    handler_path: Path
    events_to_add: list[str]
    events_already_installed: list[str]
    backup_path: Path | None  # None on dry-run

    def summary(self) -> str:
        lines = [
            f"settings.json: {self.settings_path}",
            f"handler:       {self.handler_path}",
            f"backup:        {self.backup_path or '(dry-run, none written)'}",
            "",
        ]
        if self.events_already_installed:
            lines.append(f"already installed for: {', '.join(self.events_already_installed)}")
        if self.events_to_add:
            lines.append(f"will add hook for:    {', '.join(self.events_to_add)}")
        else:
            lines.append("nothing to do — hooks already installed for every event.")
        return "\n".join(lines)


@dataclass(frozen=True)
class UninstallPlan:
    """What `uninstall` *would* remove (for --dry-run)."""

    settings_path: Path
    handler_path: Path
    events_with_cco_hook: list[str]
    backup_path: Path | None

    def summary(self) -> str:
        lines = [
            f"settings.json: {self.settings_path}",
            f"handler match: {self.handler_path}",
            f"backup:        {self.backup_path or '(dry-run, none written)'}",
            "",
        ]
        if self.events_with_cco_hook:
            lines.append(f"will remove cco hook from: {', '.join(self.events_with_cco_hook)}")
        else:
            lines.append("nothing to do — no cco hooks present.")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# atomic write + backup
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, content: str, *, mode: int = 0o600) -> None:
    """Write content to path atomically (tempfile + fsync + os.replace).

    A power loss or kill during write leaves either the old file intact or
    the new file fully written — never a half-state.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmpname = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmpname, mode)
        os.replace(tmpname, path)
    except Exception:
        with _suppress_oserror():
            os.unlink(tmpname)
        raise


class _suppress_oserror:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return isinstance(exc, OSError)


def _make_backup(settings_path: Path) -> Path:
    """Copy settings.json to settings.json.bak.<unixtime>; rotate to BACKUP_KEEP."""
    if not settings_path.is_file():
        # Nothing to back up.
        return settings_path.with_suffix(settings_path.suffix + ".bak.empty")

    import time

    stamp = int(time.time())
    backup = settings_path.with_name(f"{settings_path.name}.bak.{stamp}")
    backup.write_bytes(settings_path.read_bytes())
    backup.chmod(0o600)
    _rotate_backups(settings_path)
    return backup


def _rotate_backups(settings_path: Path) -> None:
    backups = sorted(
        settings_path.parent.glob(f"{settings_path.name}.bak.*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for stale in backups[BACKUP_KEEP:]:
        with _suppress_oserror():
            stale.unlink()


def _latest_backup(settings_path: Path) -> Path | None:
    backups = sorted(
        settings_path.parent.glob(f"{settings_path.name}.bak.*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return backups[0] if backups else None


# ---------------------------------------------------------------------------
# settings.json mutation
# ---------------------------------------------------------------------------


def _load_settings(path: Path) -> dict[str, Any]:
    """Load settings.json. Returns {} if file is missing."""
    if not path.is_file():
        return {}
    raw = path.read_text()
    return json.loads(raw) if raw.strip() else {}


def _is_cco_entry(entry: dict[str, Any], handler_path: Path) -> bool:
    """True iff this hook entry refers to OUR event_handler.sh."""
    hooks = entry.get("hooks") or []
    handler_str = str(handler_path)
    for h in hooks:
        cmd = h.get("command", "")
        if isinstance(cmd, str) and handler_str in cmd:
            return True
    return False


def _build_cco_entry(handler_path: Path, event: str) -> dict[str, Any]:
    """Build the hooks-array entry that will be inserted under each event."""
    return {
        "matcher": "",
        "hooks": [
            {
                "type": "command",
                "command": (f'CCO_EVENT={event} "{handler_path}"'),
                "async": True,
            }
        ],
    }


def plan_install() -> InstallPlan:
    """Compute (without applying) what `cco init` would do."""
    settings_path = claude_settings_path()
    handler = hook_handler_path()
    settings = _load_settings(settings_path)
    hooks_root = settings.get("hooks") or {}

    already: list[str] = []
    to_add: list[str] = []
    for event in CCO_EVENTS:
        entries = hooks_root.get(event) or []
        if any(_is_cco_entry(e, handler) for e in entries):
            already.append(event)
        else:
            to_add.append(event)

    return InstallPlan(
        settings_path=settings_path,
        handler_path=handler,
        events_to_add=to_add,
        events_already_installed=already,
        backup_path=None,  # filled in by install()
    )


def install(*, dry_run: bool = False) -> InstallPlan:
    """Add cco's hook entries to settings.json. Returns the plan."""
    plan = plan_install()
    if dry_run or not plan.events_to_add:
        return plan

    settings_path = plan.settings_path
    handler = plan.handler_path

    backup = _make_backup(settings_path)
    settings = _load_settings(settings_path)
    hooks_root = settings.setdefault("hooks", {})
    for event in plan.events_to_add:
        entries = hooks_root.setdefault(event, [])
        entries.append(_build_cco_entry(handler, event))

    _atomic_write(
        settings_path,
        json.dumps(settings, indent=2, sort_keys=False) + "\n",
        mode=0o600,
    )

    return InstallPlan(
        settings_path=plan.settings_path,
        handler_path=plan.handler_path,
        events_to_add=plan.events_to_add,
        events_already_installed=plan.events_already_installed,
        backup_path=backup,
    )


def plan_uninstall() -> UninstallPlan:
    """Compute (without applying) what `cco uninstall` would do."""
    settings_path = claude_settings_path()
    handler = hook_handler_path()
    settings = _load_settings(settings_path)
    hooks_root = settings.get("hooks") or {}

    affected: list[str] = []
    for event, entries in hooks_root.items():
        if not isinstance(entries, list):
            continue
        if any(_is_cco_entry(e, handler) for e in entries):
            affected.append(event)

    return UninstallPlan(
        settings_path=settings_path,
        handler_path=handler,
        events_with_cco_hook=sorted(affected),
        backup_path=None,
    )


def uninstall(*, dry_run: bool = False) -> UninstallPlan:
    """Remove cco's hook entries from settings.json. Returns the plan."""
    plan = plan_uninstall()
    if dry_run or not plan.events_with_cco_hook:
        return plan

    settings_path = plan.settings_path
    handler = plan.handler_path

    backup = _make_backup(settings_path)
    settings = _load_settings(settings_path)
    hooks_root = settings.get("hooks", {})

    for event in list(hooks_root.keys()):
        entries = hooks_root.get(event)
        if not isinstance(entries, list):
            continue
        kept = [e for e in entries if not _is_cco_entry(e, handler)]
        if kept:
            hooks_root[event] = kept
        else:
            # Empty array left after removal — drop the key entirely so
            # uninstall is a clean inverse of install for previously-empty
            # events (preserves byte-identical round-trip when possible).
            del hooks_root[event]

    if not hooks_root:
        # If we just emptied hooks, drop the key too.
        settings.pop("hooks", None)

    _atomic_write(
        settings_path,
        json.dumps(settings, indent=2, sort_keys=False) + "\n",
        mode=0o600,
    )

    return UninstallPlan(
        settings_path=plan.settings_path,
        handler_path=plan.handler_path,
        events_with_cco_hook=plan.events_with_cco_hook,
        backup_path=backup,
    )


def restore_backup() -> Path | None:
    """Rescue: replace settings.json with the most-recent backup. Returns the
    backup path that was restored, or None if no backups exist."""
    settings_path = claude_settings_path()
    backup = _latest_backup(settings_path)
    if backup is None:
        return None
    _atomic_write(
        settings_path,
        backup.read_text(),
        mode=0o600,
    )
    return backup


def latest_backup_path() -> Path | None:
    """Public helper for the CLI's --restore-backup --dry-run path."""
    return _latest_backup(claude_settings_path())


# ---------------------------------------------------------------------------
# Speech-ownership: hand TTS playback over to cco.
# ---------------------------------------------------------------------------

# What we recognise as the user's existing TTS hook. Substring match against
# the hook entry's command string — captures both `tts-speak-response`
# and any `.kokoro` / `.piper` siblings the user might invoke.
TTS_HOOK_MARKER = "tts-speak-response"


@dataclass(frozen=True)
class SpeechInstallPlan:
    """What `cco speech install` *would* remove from settings.json."""

    settings_path: Path
    affected_events: list[str]
    affected_commands: list[str]
    backup_path: Path | None

    def summary(self) -> str:
        lines = [
            f"settings.json: {self.settings_path}",
            f"backup:        {self.backup_path or '(dry-run, none written)'}",
            "",
        ]
        if self.affected_events:
            lines.append(
                "will remove tts-speak-response hooks from: " + ", ".join(self.affected_events)
            )
            for cmd in self.affected_commands:
                lines.append(f"  - {cmd}")
        else:
            lines.append("nothing to do — no tts-speak-response hooks present.")
        return "\n".join(lines)


def _command_invokes_tts(cmd: object) -> bool:
    return isinstance(cmd, str) and TTS_HOOK_MARKER in cmd


def plan_speech_install() -> SpeechInstallPlan:
    """Compute (without applying) what `cco speech install` would do."""
    settings_path = claude_settings_path()
    settings = _load_settings(settings_path)
    hooks_root = settings.get("hooks") or {}

    affected_events: list[str] = []
    affected_cmds: list[str] = []
    for event, entries in hooks_root.items():
        if not isinstance(entries, list):
            continue
        event_matched = False
        for e in entries:
            if not isinstance(e, dict):
                continue
            for h in e.get("hooks") or []:
                if isinstance(h, dict) and _command_invokes_tts(h.get("command")):
                    affected_cmds.append(h["command"])
                    event_matched = True
        if event_matched:
            affected_events.append(event)
    return SpeechInstallPlan(
        settings_path=settings_path,
        affected_events=sorted(set(affected_events)),
        affected_commands=affected_cmds,
        backup_path=None,
    )


def install_speech(*, dry_run: bool = False) -> SpeechInstallPlan:
    """Remove tts-speak-response from every hook in settings.json so cco's
    SpeechPlayer is the single TTS path. Atomic write + backup, same
    discipline as `cco init` / `cco uninstall`."""
    plan = plan_speech_install()
    if dry_run or not plan.affected_events:
        return plan

    settings_path = plan.settings_path
    backup = _make_backup(settings_path)
    settings = _load_settings(settings_path)
    hooks_root = settings.get("hooks", {})

    for event in list(hooks_root.keys()):
        entries = hooks_root.get(event)
        if not isinstance(entries, list):
            continue
        kept: list[dict[str, Any]] = []
        for e in entries:
            if not isinstance(e, dict):
                kept.append(e)
                continue
            # Filter the inner hooks list — keep siblings (e.g. cco's own
            # event_handler.sh stays put when both hooks share an entry),
            # drop only commands matching TTS_HOOK_MARKER.
            inner = e.get("hooks") or []
            kept_inner = [
                h
                for h in inner
                if not (isinstance(h, dict) and _command_invokes_tts(h.get("command")))
            ]
            if kept_inner:
                new_entry = dict(e)
                new_entry["hooks"] = kept_inner
                kept.append(new_entry)
            # else: entry empty after removal — drop entirely.
        if kept:
            hooks_root[event] = kept
        else:
            del hooks_root[event]

    if not hooks_root:
        settings.pop("hooks", None)

    _atomic_write(
        settings_path,
        json.dumps(settings, indent=2, sort_keys=False) + "\n",
        mode=0o600,
    )

    return SpeechInstallPlan(
        settings_path=plan.settings_path,
        affected_events=plan.affected_events,
        affected_commands=plan.affected_commands,
        backup_path=backup,
    )
