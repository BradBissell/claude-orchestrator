"""Smoke tests to verify P0 install + CLI plumbing."""

from __future__ import annotations

import subprocess
import sys

import claude_orchestrator
from claude_orchestrator.cli import main


def test_version_attribute() -> None:
    assert claude_orchestrator.__version__ == "0.1.0"


def test_main_no_args_prints_help_and_exits_zero(capsys) -> None:
    rc = main([])
    captured = capsys.readouterr()
    assert rc == 0
    assert "cco" in captured.err.lower() or "cco" in captured.out.lower()


def test_unknown_subcommand_exits_two(capsys) -> None:
    import pytest

    # argparse calls sys.exit(2) on an invalid choice → SystemExit.
    with pytest.raises(SystemExit) as exc:
        main(["bogus-subcommand-that-does-not-exist"])
    assert exc.value.code == 2
    err = capsys.readouterr().err.lower()
    assert "invalid choice" in err or "unrecognized" in err or "bogus" in err


def test_version_via_subprocess() -> None:
    """End-to-end: invoking the installed entrypoint prints the version."""
    result = subprocess.run(
        [sys.executable, "-m", "claude_orchestrator", "--version"],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    assert "cco 0.1.0" in result.stdout
