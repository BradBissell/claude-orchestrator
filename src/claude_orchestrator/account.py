"""User-configured account metadata (token cap, etc.).

Read once at startup from ~/.config/claude-orchestrator/account.toml. The
file is optional; missing → no cap, summary line just shows the total.

Schema (all keys optional):
  weekly_cap_tokens = 3_000_000

Future: hook into `claude /usage` to fetch caps automatically. Today /usage
is a slash command in the interactive REPL — not pipeable, not exposed via
flags — so we rely on the user-supplied number.
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from claude_orchestrator.config import _xdg_home

log = logging.getLogger(__name__)


def account_config_path() -> Path:
    """Resolve the account config path, honoring CCO_ACCOUNT_CONFIG override
    (used by tests to point at a tmp_path file)."""
    raw = os.environ.get("CCO_ACCOUNT_CONFIG")
    if raw:
        return Path(raw).expanduser()
    return _xdg_home("XDG_CONFIG_HOME", ".config") / "claude-orchestrator" / "account.toml"


@dataclass(frozen=True)
class AccountConfig:
    """Subset of account.toml the dashboard cares about. Empty/None when
    the file is absent or malformed; consumers must handle that."""

    weekly_cap_tokens: int | None = None


def load_account_config(path: Path | None = None) -> AccountConfig:
    """Load and validate account.toml. Returns defaults on any failure —
    the dashboard should never crash because the config is missing or bad.
    """
    target = path if path is not None else account_config_path()
    if not target.is_file():
        return AccountConfig()
    try:
        with target.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        log.warning("account.toml unreadable, ignoring: %s", exc)
        return AccountConfig()

    cap_raw = data.get("weekly_cap_tokens")
    cap: int | None = None
    if isinstance(cap_raw, int) and cap_raw > 0:
        cap = cap_raw
    return AccountConfig(weekly_cap_tokens=cap)
