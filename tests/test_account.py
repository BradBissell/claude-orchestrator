"""Tests for account.toml loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_orchestrator import account


def test_returns_defaults_when_file_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CCO_ACCOUNT_CONFIG", str(tmp_path / "missing.toml"))
    cfg = account.load_account_config()
    assert cfg.weekly_cap_tokens is None


def test_loads_weekly_cap_from_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "account.toml"
    p.write_text("weekly_cap_tokens = 3_000_000\n")
    monkeypatch.setenv("CCO_ACCOUNT_CONFIG", str(p))
    cfg = account.load_account_config()
    assert cfg.weekly_cap_tokens == 3_000_000


def test_rejects_non_positive_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "account.toml"
    p.write_text("weekly_cap_tokens = 0\n")
    monkeypatch.setenv("CCO_ACCOUNT_CONFIG", str(p))
    cfg = account.load_account_config()
    assert cfg.weekly_cap_tokens is None


def test_garbled_toml_logs_and_returns_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = tmp_path / "account.toml"
    p.write_text("not valid = = toml\n")
    monkeypatch.setenv("CCO_ACCOUNT_CONFIG", str(p))
    cfg = account.load_account_config()
    assert cfg.weekly_cap_tokens is None


def test_explicit_path_argument_overrides_env(tmp_path: Path) -> None:
    p = tmp_path / "specific.toml"
    p.write_text("weekly_cap_tokens = 1234567\n")
    cfg = account.load_account_config(p)
    assert cfg.weekly_cap_tokens == 1234567
