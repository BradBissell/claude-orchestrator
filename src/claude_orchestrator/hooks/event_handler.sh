#!/usr/bin/env bash
# claude-orchestrator hook handler.
#
# Receives JSON on stdin from Claude Code hooks. Writes per-session state
# files to $CCO_STATE_DIR (default: $XDG_STATE_HOME/claude-orchestrator/sessions/).
#
# Design contract:
#   * Fail OPEN. ANY failure → exit 0, no output, no error to claude.
#     A buggy hook must NEVER block the user's claude session.
#   * Atomic writes. mktemp + fsync + mv-rename. No partial states.
#   * Per-session flock. 30 sessions firing in parallel must not lose updates.
#   * No git, no PPID-based heuristics, no shell injection of user data.
#     Every Claude-controlled value flows through `jq --arg` / `--argjson`.
#   * Sub-15 ms p95 latency budget.
#
# Hook events handled:
#   SessionStart, SessionEnd, UserPromptSubmit
#   PreToolUse, PostToolUse, PostToolUseFailure
#   Notification, PermissionRequest, PermissionDenied
#   Stop, StopFailure, SubagentStart, SubagentStop
#
# For PermissionRequest, the handler emits a JSON decision object on stdout
# IF a pending decision file exists at $CCO_PENDING_DIR/<sid>.json. Otherwise
# emits empty output (claude shows the normal dialog).

# --- safety + fail-OPEN trap ----------------------------------------------
# Even with set -e, we want to GUARANTEE exit 0. A trap on ERR + EXIT enforces it.
set -u

cco_exit_open() {
  # Fail OPEN: regardless of internal failures, never block claude.
  exit 0
}
trap cco_exit_open ERR EXIT

# Defeat hook-environment hijack vectors.
unset BASH_ENV ENV PROMPT_COMMAND
PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH

# --- paths ----------------------------------------------------------------
STATE_DIR="${CCO_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/claude-orchestrator/sessions}"
PENDING_DIR="${CCO_PENDING_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/claude-orchestrator/pending}"
LOCK_DIR="${CCO_LOCK_DIR:-${XDG_RUNTIME_DIR:-/tmp}/claude-orchestrator/locks}"
SCHEMA_VERSION=1

mkdir -p "$STATE_DIR" "$PENDING_DIR" "$LOCK_DIR" 2>/dev/null || cco_exit_open
chmod 0700 "$STATE_DIR" "$PENDING_DIR" "$LOCK_DIR" 2>/dev/null || true

# --- read input -----------------------------------------------------------
INPUT_JSON="$(cat)"
[ -z "$INPUT_JSON" ] && cco_exit_open

# Probe for jq early — if missing, fail open silently.
command -v jq >/dev/null 2>&1 || cco_exit_open

SESSION_ID="$(printf '%s' "$INPUT_JSON" | jq -r '.session_id // empty')"
[ -z "$SESSION_ID" ] && cco_exit_open

# Defensive: anchor session_id to safe characters before path use.
case "$SESSION_ID" in
  *[!a-zA-Z0-9_-]*) cco_exit_open ;;
esac

EVENT_NAME="$(printf '%s' "$INPUT_JSON" | jq -r '.hook_event_name // empty')"
[ -z "$EVENT_NAME" ] && cco_exit_open

CWD="$(printf '%s' "$INPUT_JSON" | jq -r '.cwd // empty')"
[ -z "$CWD" ] && CWD="$PWD"
PROJECT_NAME="$(basename -- "$CWD")"

NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

STATE_FILE="$STATE_DIR/$SESSION_ID.json"
LOCK_FILE="$LOCK_DIR/$SESSION_ID.lock"

# --- per-session lock -----------------------------------------------------
exec 9>"$LOCK_FILE" || cco_exit_open
flock -w 2 9 || cco_exit_open

# --- claude PID discovery -------------------------------------------------
# Walk parents from our own PID, skipping shells, until we hit the claude
# process that fired this hook. Recording its PID lets the dashboard match
# state files to running claudes unambiguously, even when several share a cwd.
find_claude_pid() {
  local pid="$$"
  local depth=0
  while [ "$depth" -lt 8 ]; do
    pid="$(awk '/^PPid:/{print $2; exit}' "/proc/$pid/status" 2>/dev/null)" || return 0
    [ -z "$pid" ] && return 0
    [ "$pid" -le 1 ] && return 0
    local comm
    comm="$(cat "/proc/$pid/comm" 2>/dev/null)" || comm=""
    case "$comm" in
      sh|bash|dash|zsh|fish|ksh|"") ;;        # keep walking past shells
      *) printf '%s' "$pid"; return 0 ;;       # first non-shell ancestor
    esac
    depth=$((depth + 1))
  done
}

CLAUDE_PID="$(find_claude_pid)"

# --- helpers --------------------------------------------------------------
read_field() {
  # $1 = field name, $2 = default. Reads from current state file.
  if [ -f "$STATE_FILE" ]; then
    jq -r --arg f "$1" --arg d "$2" '.[$f] // $d' "$STATE_FILE" 2>/dev/null || printf '%s' "$2"
  else
    printf '%s' "$2"
  fi
}

write_state() {
  # Receives the desired JSON document on stdin and writes it atomically.
  local tmp
  tmp="$(mktemp "$STATE_DIR/.tmp.XXXXXX")" || return
  trap 'rm -f "$tmp"' RETURN
  cat >"$tmp" || return
  # fsync the temp file's contents before rename — survives crashes.
  if command -v sync >/dev/null 2>&1; then
    sync -f "$tmp" 2>/dev/null || sync "$tmp" 2>/dev/null || true
  fi
  chmod 0600 "$tmp" 2>/dev/null || true
  mv -f "$tmp" "$STATE_FILE" 2>/dev/null || return
}

base_state() {
  # Emit the always-present base fields. Status + event-specific fields are
  # added by the event branch via jq merge.
  if [ -f "$STATE_FILE" ]; then
    jq -c \
      --arg ev "$EVENT_NAME" \
      --arg ts "$NOW" \
      --argjson sv "$SCHEMA_VERSION" \
      --arg cwd "$CWD" \
      --arg proj "$PROJECT_NAME" \
      --arg cpid "${CLAUDE_PID:-}" \
      '. + {
         schema_version: $sv,
         cwd: $cwd,
         project_name: $proj,
         last_event: $ev,
         last_event_time: $ts,
         last_event_seq: ((.last_event_seq // 0) + 1),
         claude_pid: (if $cpid == "" then .claude_pid else ($cpid | tonumber) end)
       }' "$STATE_FILE"
  else
    jq -nc \
      --arg sid "$SESSION_ID" \
      --arg cwd "$CWD" \
      --arg proj "$PROJECT_NAME" \
      --arg started "$NOW" \
      --arg ev "$EVENT_NAME" \
      --arg ts "$NOW" \
      --argjson sv "$SCHEMA_VERSION" \
      --arg cpid "${CLAUDE_PID:-}" \
      '{
         schema_version: $sv,
         session_id: $sid,
         cwd: $cwd,
         project_name: $proj,
         started_at: $started,
         status: "IDLE",
         last_event: $ev,
         last_event_time: $ts,
         last_event_seq: 1,
         tool_count: 0,
         error_count: 0,
         tmux_session: null,
         tmux_window: null,
         tmux_pane: null,
         claude_pid: (if $cpid == "" then null else ($cpid | tonumber) end),
         notification: null
       }'
  fi
}

apply_status() {
  # Transition status. $1 = new status string, optional $2 = jq filter for extra fields.
  local new_status="$1"
  local extra_filter="${2:-}"
  local merged
  merged="$(base_state)" || return
  if [ -n "$extra_filter" ]; then
    merged="$(printf '%s' "$merged" | jq -c --arg s "$new_status" ". + {status: \$s} | $extra_filter")" || return
  else
    merged="$(printf '%s' "$merged" | jq -c --arg s "$new_status" '. + {status: $s}')" || return
  fi
  printf '%s\n' "$merged" | write_state
}

# --- tmux mapping (best-effort, non-blocking) -----------------------------
populate_tmux_mapping() {
  # If we're inside a tmux pane, capture session/window/pane refs.
  # Re-read every event because tmux assignments can change.
  if [ -n "${TMUX:-}" ] && command -v tmux >/dev/null 2>&1; then
    local info
    info="$(tmux display-message -p $'#S\t#W\t#{pane_id}' 2>/dev/null)" || return 0
    if [ -n "$info" ]; then
      local s w p
      IFS=$'\t' read -r s w p <<<"$info"
      jq -c \
        --arg s "$s" --arg w "$w" --arg p "$p" \
        '. + {tmux_session: $s, tmux_window: $w, tmux_pane: $p}'
      return 0
    fi
  fi
  cat
}

# --- event branches -------------------------------------------------------
case "$EVENT_NAME" in
  SessionStart)
    base_state | jq -c '. + {status: "IDLE"}' | populate_tmux_mapping | write_state
    ;;

  SessionEnd | Stop | StopFailure)
    apply_status "IDLE"
    ;;

  PreToolUse)
    base_state \
      | jq -c '. + {status: "WORKING", tool_count: (.tool_count + 1), notification: null}' \
      | populate_tmux_mapping \
      | write_state
    ;;

  PostToolUse)
    base_state | jq -c '. + {status: "WORKING", notification: null}' | write_state
    ;;

  PostToolUseFailure)
    base_state \
      | jq -c '. + {status: "ERROR", error_count: (.error_count + 1)}' \
      | write_state
    ;;

  Notification)
    NOTIF_MSG="$(printf '%s' "$INPUT_JSON" | jq -r '.message // ""')"
    case "$NOTIF_MSG" in
      *"waiting for your input"*|*"need your"*|*"clarif"*)
        NOTIF_TYPE="question"; STATUS="WAITING_ANSWER" ;;
      *"permission"*|*"approve"*|*"allow"*)
        NOTIF_TYPE="permission"; STATUS="WAITING_PERMISSION" ;;
      *)
        NOTIF_TYPE="question"; STATUS="WAITING_ANSWER" ;;
    esac
    base_state \
      | jq -c \
          --arg s "$STATUS" \
          --arg nt "$NOTIF_TYPE" \
          '. + {
             status: $s,
             notification: {
               type: $nt,
               tool: null,
               redacted_summary: null
             }
           }' \
      | write_state
    ;;

  PermissionRequest)
    TOOL_NAME="$(printf '%s' "$INPUT_JSON" | jq -r '.tool_name // "unknown"')"
    base_state \
      | jq -c \
          --arg tool "$TOOL_NAME" \
          '. + {
             status: "WAITING_PERMISSION",
             notification: {
               type: "permission",
               tool: $tool,
               redacted_summary: null
             }
           }' \
      | write_state

    # If a pending decision file exists, emit it as the hook return value.
    PENDING_FILE="$PENDING_DIR/$SESSION_ID.json"
    if [ -f "$PENDING_FILE" ]; then
      cat "$PENDING_FILE"
      rm -f "$PENDING_FILE" 2>/dev/null || true
    fi
    ;;

  PermissionDenied)
    base_state \
      | jq -c '. + {status: "ERROR", error_count: (.error_count + 1), notification: null}' \
      | write_state
    ;;

  UserPromptSubmit | SubagentStart | SubagentStop)
    base_state | write_state
    ;;

  *)
    # Unknown event — record it but don't fabricate a status.
    base_state | write_state
    ;;
esac

# Trap will run cco_exit_open → exit 0.
