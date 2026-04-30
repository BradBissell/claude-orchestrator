"""Allow `python -m claude_orchestrator` to invoke the CLI."""

from __future__ import annotations

from claude_orchestrator.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
