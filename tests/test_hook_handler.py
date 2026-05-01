"""End-to-end tests for the hook handler shell script.

These tests exercise the actual event_handler.sh by piping JSON to it and
inspecting the resulting state file. They require `bash` and `jq` on PATH.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from claude_orchestrator.config import hook_handler_path

HANDLER = hook_handler_path()
SCHEMA_VERSION = 2


@pytest.fixture
def state_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Sandboxed env: state/pending/lock dirs all under tmp_path."""
    state = tmp_path / "sessions"
    pending = tmp_path / "pending"
    lock = tmp_path / "locks"
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "CCO_STATE_DIR": str(state),
        "CCO_PENDING_DIR": str(pending),
        "CCO_LOCK_DIR": str(lock),
    }
    return env


def _fire_hook(
    input_json: dict[str, object], env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(HANDLER)],
        input=json.dumps(input_json),
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
        check=False,
    )


def _state_file(env: dict[str, str], sid: str) -> Path:
    return Path(env["CCO_STATE_DIR"]) / f"{sid}.json"


def _read_state(env: dict[str, str], sid: str) -> dict[str, object]:
    return json.loads(_state_file(env, sid).read_text())


def _required_tools_present() -> bool:
    return all(shutil.which(t) for t in ("bash", "jq", "flock"))


@pytest.fixture(autouse=True)
def _skip_if_tools_missing() -> None:
    if not _required_tools_present():
        pytest.skip("requires bash + jq + flock on PATH")


def test_session_start_writes_idle_state(state_env: dict[str, str]) -> None:
    sid = "test-session-1"
    result = _fire_hook(
        {
            "session_id": sid,
            "hook_event_name": "SessionStart",
            "cwd": "/tmp/myproject",
        },
        state_env,
    )
    assert result.returncode == 0, result.stderr
    state = _read_state(state_env, sid)
    assert state["session_id"] == sid
    assert state["status"] == "IDLE"
    assert state["schema_version"] == SCHEMA_VERSION
    assert state["project_name"] == "myproject"
    assert state["last_event"] == "SessionStart"
    assert state["last_event_seq"] == 1


def test_pre_tool_use_increments_tool_count(state_env: dict[str, str]) -> None:
    sid = "test-session-2"
    for _ in range(3):
        result = _fire_hook(
            {
                "session_id": sid,
                "hook_event_name": "PreToolUse",
                "cwd": "/tmp/x",
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
            },
            state_env,
        )
        assert result.returncode == 0
    state = _read_state(state_env, sid)
    assert state["status"] == "WORKING"
    assert state["tool_count"] == 3
    assert state["last_event_seq"] == 3


def test_notification_question_transitions_to_waiting_answer(
    state_env: dict[str, str],
) -> None:
    sid = "test-session-3"
    _fire_hook(
        {"session_id": sid, "hook_event_name": "SessionStart", "cwd": "/tmp/x"},
        state_env,
    )
    _fire_hook(
        {
            "session_id": sid,
            "hook_event_name": "Notification",
            "cwd": "/tmp/x",
            "message": "Claude is waiting for your input",
        },
        state_env,
    )
    state = _read_state(state_env, sid)
    assert state["status"] == "WAITING_ANSWER"
    assert state["notification"]["type"] == "question"


def test_permission_request_transitions_to_waiting_permission(
    state_env: dict[str, str],
) -> None:
    sid = "test-session-4"
    _fire_hook(
        {
            "session_id": sid,
            "hook_event_name": "PermissionRequest",
            "cwd": "/tmp/x",
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf /"},
        },
        state_env,
    )
    state = _read_state(state_env, sid)
    assert state["status"] == "WAITING_PERMISSION"
    assert state["notification"]["type"] == "permission"
    assert state["notification"]["tool"] == "Bash"


def test_permission_request_emits_pending_decision(
    state_env: dict[str, str],
) -> None:
    sid = "test-session-5"
    pending_file = Path(state_env["CCO_PENDING_DIR"]) / f"{sid}.json"
    pending_file.parent.mkdir(parents=True, exist_ok=True)
    pending_file.write_text(json.dumps({"hookSpecificOutput": {"decision": {"behavior": "allow"}}}))
    result = _fire_hook(
        {
            "session_id": sid,
            "hook_event_name": "PermissionRequest",
            "cwd": "/tmp/x",
            "tool_name": "Bash",
        },
        state_env,
    )
    assert result.returncode == 0
    decision = json.loads(result.stdout)
    assert decision["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    assert not pending_file.exists(), "pending file should be consumed after read"


def test_post_tool_use_failure_increments_error_count(
    state_env: dict[str, str],
) -> None:
    sid = "test-session-6"
    _fire_hook(
        {
            "session_id": sid,
            "hook_event_name": "PostToolUseFailure",
            "cwd": "/tmp/x",
            "tool_name": "Bash",
        },
        state_env,
    )
    state = _read_state(state_env, sid)
    assert state["status"] == "ERROR"
    assert state["error_count"] == 1


def test_stop_transitions_to_idle(state_env: dict[str, str]) -> None:
    sid = "test-session-7"
    _fire_hook(
        {
            "session_id": sid,
            "hook_event_name": "PreToolUse",
            "cwd": "/tmp/x",
            "tool_name": "Bash",
        },
        state_env,
    )
    _fire_hook(
        {"session_id": sid, "hook_event_name": "Stop", "cwd": "/tmp/x"},
        state_env,
    )
    state = _read_state(state_env, sid)
    assert state["status"] == "IDLE"


def test_handler_fails_open_on_missing_session_id(state_env: dict[str, str]) -> None:
    result = _fire_hook(
        {"hook_event_name": "PreToolUse", "cwd": "/tmp/x"},
        state_env,
    )
    # Must not block claude — exit 0, no state file written.
    assert result.returncode == 0
    sessions = list(Path(state_env["CCO_STATE_DIR"]).glob("*.json"))
    assert sessions == []


def test_handler_fails_open_on_invalid_session_id(state_env: dict[str, str]) -> None:
    result = _fire_hook(
        {
            "session_id": "../escape-attempt",
            "hook_event_name": "SessionStart",
            "cwd": "/tmp/x",
        },
        state_env,
    )
    assert result.returncode == 0
    # No file should be created with a path-traversal session_id.
    sessions = list(Path(state_env["CCO_STATE_DIR"]).rglob("*.json"))
    assert sessions == []


def test_handler_fails_open_on_garbage_input(state_env: dict[str, str]) -> None:
    result = subprocess.run(
        ["bash", str(HANDLER)],
        input="not valid json at all",
        capture_output=True,
        text=True,
        timeout=5,
        env=state_env,
        check=False,
    )
    assert result.returncode == 0


def test_state_file_permissions_are_0600(state_env: dict[str, str]) -> None:
    sid = "test-session-8"
    _fire_hook(
        {"session_id": sid, "hook_event_name": "SessionStart", "cwd": "/tmp/x"},
        state_env,
    )
    mode = _state_file(state_env, sid).stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_cco_internal_env_skips_state_write(state_env: dict[str, str]) -> None:
    """When the summarizer subprocess fires `claude -p`, our hook handler
    sees CCO_INTERNAL=1 in the inherited env and exits without writing a
    state file. Without this guard, every summarization would create a
    ghost session in the dashboard."""
    sid = "test-session-internal"
    result = _fire_hook(
        {
            "session_id": sid,
            "hook_event_name": "SessionStart",
            "cwd": "/tmp/myproject",
        },
        {**state_env, "CCO_INTERNAL": "1"},
    )
    assert result.returncode == 0
    # No state file written.
    assert not _state_file(state_env, sid).exists()


def test_user_prompt_submit_records_last_summary(state_env: dict[str, str]) -> None:
    sid = "test-session-prompt"
    result = _fire_hook(
        {
            "session_id": sid,
            "hook_event_name": "UserPromptSubmit",
            "cwd": "/tmp/myproject",
            "prompt": "fix the failing test in tests/test_foo.py",
        },
        state_env,
    )
    assert result.returncode == 0, result.stderr
    state = _read_state(state_env, sid)
    assert state["last_summary"] == "fix the failing test in tests/test_foo.py"


def test_user_prompt_submit_truncates_to_70_chars(state_env: dict[str, str]) -> None:
    sid = "test-session-prompt-long"
    long_prompt = "x" * 200
    _fire_hook(
        {
            "session_id": sid,
            "hook_event_name": "UserPromptSubmit",
            "cwd": "/tmp/myproject",
            "prompt": long_prompt,
        },
        state_env,
    )
    state = _read_state(state_env, sid)
    assert isinstance(state["last_summary"], str)
    assert len(state["last_summary"]) == 70


def test_user_prompt_submit_strips_newlines(state_env: dict[str, str]) -> None:
    """Newlines/tabs become spaces so the JSON state file stays single-line-safe."""
    sid = "test-session-prompt-multi"
    _fire_hook(
        {
            "session_id": sid,
            "hook_event_name": "UserPromptSubmit",
            "cwd": "/tmp/myproject",
            "prompt": "first line\nsecond\tline",
        },
        state_env,
    )
    state = _read_state(state_env, sid)
    assert "\n" not in state["last_summary"]  # type: ignore[operator]
    assert "\t" not in state["last_summary"]  # type: ignore[operator]


def test_concurrent_writes_dont_lose_updates(state_env: dict[str, str]) -> None:
    """Fire 10 hooks concurrently for the same session; tool_count must equal 10."""
    sid = "test-session-9"
    procs: list[subprocess.Popen[str]] = []
    for _ in range(10):
        p = subprocess.Popen(
            ["bash", str(HANDLER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=state_env,
        )
        p.stdin.write(  # type: ignore[union-attr]
            json.dumps(
                {
                    "session_id": sid,
                    "hook_event_name": "PreToolUse",
                    "cwd": "/tmp/x",
                    "tool_name": "Bash",
                }
            )
        )
        p.stdin.close()  # type: ignore[union-attr]
        procs.append(p)

    for p in procs:
        p.wait(timeout=5)
        assert p.returncode == 0

    state = _read_state(state_env, sid)
    assert state["tool_count"] == 10, (
        f"flock should have serialised all 10 writes; got {state['tool_count']}"
    )
    assert state["last_event_seq"] == 10


def _run_populate_tmux_mapping(
    stdin_json: str,
    *,
    tmux_body: str | None = None,
    tmux_set: bool = True,
) -> str:
    """Source event_handler.sh's `populate_tmux_mapping` in a subshell with a
    controlled `tmux` shim and run it against stdin_json. Lets us exercise
    the function in isolation without fighting the handler's PATH reset.
    """
    handler_text = HANDLER.read_text()
    start = handler_text.index("populate_tmux_mapping() {")
    end = handler_text.index("\n}\n", start) + 3
    fn_body = handler_text[start:end]

    body = tmux_body if tmux_body is not None else 'echo ""; return 1'
    tmux_var = "fake-server" if tmux_set else ""
    script = (
        "#!/usr/bin/env bash\nset -u\n"
        f'TMUX="{tmux_var}"\n'
        f"tmux() {{ {body}; }}\nexport -f tmux\n\n"
        f"{fn_body}\n\n"
        "populate_tmux_mapping\n"
    )
    proc = subprocess.run(
        ["bash", "-c", script],
        input=stdin_json,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_populate_tmux_mapping_enriches_stdin_with_tmux_fields() -> None:
    out = _run_populate_tmux_mapping(
        '{"session_id":"x","status":"IDLE"}',
        tmux_body=r'printf "work\tclaude\t%%9"',
    )
    parsed = json.loads(out)
    assert parsed["tmux_session"] == "work"
    assert parsed["tmux_window"] == "claude"
    assert parsed["tmux_pane"] == "%9"
    assert parsed["session_id"] == "x"


def test_populate_tmux_mapping_passes_stdin_through_when_tmux_unset() -> None:
    """No TMUX env → no enrichment, but the JSON must survive intact."""
    body = '{"session_id":"x","status":"IDLE"}'
    out = _run_populate_tmux_mapping(body, tmux_set=False)
    assert json.loads(out) == json.loads(body)


def test_populate_tmux_mapping_passes_stdin_through_on_tmux_failure() -> None:
    """Regression: a non-zero `tmux display-message` (server hiccup) used to
    cause populate_tmux_mapping to silently drop stdin, producing an empty
    state file. It must always pass stdin through."""
    body = '{"session_id":"x","status":"WORKING"}'
    out = _run_populate_tmux_mapping(body, tmux_body="return 1")
    assert json.loads(out) == json.loads(body)


def test_populate_tmux_mapping_passes_stdin_through_on_empty_tmux_output() -> None:
    body = '{"session_id":"x","status":"WORKING"}'
    out = _run_populate_tmux_mapping(body, tmux_body='echo ""')
    assert json.loads(out) == json.loads(body)
