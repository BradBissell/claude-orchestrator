# Stitch Design Handoff — TUI Redesign

**Status:** Design generated in Stitch; not yet ported to Textual. Pick this up
in a fresh Claude Code session.

## What lives where

### Stitch project (Google AI Studio / Stitch)

- **Project**: `Cco Terminal Dashboard UI`
- **Project ID**: `1459994676986774038`
- **Direct URL pattern**: `https://stitch.withgoogle.com/projects/1459994676986774038`

### Screens already generated

The 3-state spec from the prompt produced 3+ screens:

| Title | Screen ID | What it shows |
|---|---|---|
| `cco - Healthy Fleet` | `1d6ac1b6ccba4effb13c245502758092` | 13 sessions, mostly green, no attention |
| `cco - Operator Needed` | `17bf1ac7ff5541e7852fa501da02627b` | 2 PERM + 1 ERR + 1 WAIT |
| `cco - Operator Needed` (variant) | `6c0a7375a4ed48bc984f33f1b0330a90` | alt layout |
| `cco - Operator Needed` (variant) | `b3cd9e08f6084b138a90b264c577fad2` | alt layout |
| `cco - Mostly Stale` | `4f5e957674ba4135bad22b5c68596711` | 6 dead, 2 idle, 1 working |

### Local artifacts (committed in this repo)

- `docs/stitch/healthy.html` + `.png` — the Healthy Fleet screen
- `docs/stitch/operator.html` + `.png` — the Operator Needed screen

(Mostly-Stale not fetched; pull via the Stitch MCP if needed.)

### Design system tokens (established in the Stitch project)

```yaml
# from project's designMd
colors:
  surface:           '#0d1515'   # base terminal floor
  surface_container: '#192121'   # data row alt
  surface_container_high: '#232c2b'
  on_surface:        '#dbe4e3'   # primary text (off-white)
  on_surface_variant:'#b9cac9'   # dim secondary text
  outline:           '#839493'   # divider lines
  primary:           '#00ffff'   # cyan branding + active selection
  # status accents
  green:             '#3fb950'   # WORKING
  red:               '#f85149'   # WAITING_PERMISSION
  yellow:            '#d29922'   # WAITING_ANSWER
  magenta:           '#d2a8ff'   # ERROR
  dim_gray:          '#3b4048'   # IDLE / DEAD

typography:
  font: JetBrains Mono   # strict monospace throughout
  header: 16px / 700
  body:   14px / 400
  label:  12px / 400
```

Full design markdown is in the Stitch project's `designTheme.designMd`.

## Where the prompt is

The prompt that produced these screens is the assistant message immediately
preceding the "use stitch mcp to update the cco tui" turn — search the
conversation history for "Design a terminal-style dashboard UI". Re-read it
before regenerating; key constraints:

- Half-screen height (~24 rows of terminal)
- 2 rows per session: primary row + dim italic conversation summary
- Status icons: `● ◐ ▶ ◌ ⚠ ☠`
- Sparklines: `▁▂▃▄▅▆▇█` per session
- Token usage in summary line
- No web chrome — pure terminal aesthetic

## Next-session todo list

1. **Read** the screenshots: `docs/stitch/healthy.png` (and operator.png).
   They're the spec. The HTML alongside them is reference for spacing/colors,
   not for direct port.

2. **Create `src/claude_orchestrator/tui/theme.tcss`** — a Textual stylesheet
   with the design tokens above. Approx structure:
   ```tcss
   $surface: #0d1515;
   $surface-alt: #192121;
   $primary: #00ffff;
   $green: #3fb950;
   /* … etc … */
   Screen { background: $surface; color: #dbe4e3; }
   Header { background: $surface; color: $primary; }
   /* status row colors per AgentStatus */
   ```

3. **Rewrite `src/claude_orchestrator/tui/app.py`** to match the Stitch layout:
   - Header strip with live status counters (PERM/WAIT/ERR/WORK/IDLE/DEAD badges)
   - Summary line: aggregate sparkline + token count + active-count
   - Replace the single `DataTable` with a `ListView` of 2-row session cards
     (each card a custom widget showing the primary row + the conversation
     summary subline). Or keep DataTable but add a second row per session.
   - Footer hint bar (already exists via Textual `Footer`)
   - Consider splitting into `tui/widgets/session_row.py`,
     `tui/widgets/header_bar.py`, `tui/widgets/footer_hints.py` (the
     architect's earlier guidance — "<400 lines per file").

4. **Add a conversation-summary field to AgentState** — currently no such
   field exists. The hook handler would need to extract the latest user
   prompt or last assistant tool call from the transcript jsonl and store
   it in state. Consider:
   - schema bump to v2 (already plumbed through models.py)
   - `last_summary` field, populated from `transcript_path` in hook payload
   - Truncate to 70 chars

5. **Add a per-session activity sample** for the sparklines (P12 in the
   project brief, previously deferred). Either:
   - extend the activity collector design
   - OR sample-on-render: walk `/proc/<claude_pid>/stat` cpu deltas at TUI
     refresh time (every 500ms × ~10 samples = 5s window)

6. **Add token-usage tracking** for the summary line. Sources:
   - `~/.claude/projects/<>/<sid>.jsonl` has token counts per assistant turn
   - Sum across all live sessions
   - Cache between TUI refreshes — re-parsing every jsonl per tick is wasteful

7. **Update tests**:
   - Snapshot test the new layout via Textual's pilot harness
   - The 5 existing TUI tests still pass

## Tooling reference

- Stitch MCP tools available: `mcp__stitch__list_projects`,
  `mcp__stitch__get_screen`, `mcp__stitch__list_screens`,
  `mcp__stitch__generate_screen_from_text`,
  `mcp__stitch__generate_variants`, `mcp__stitch__edit_screens`
- Re-pull HTML if needed:
  ```python
  mcp__stitch__get_screen(
      name="projects/1459994676986774038/screens/<screen_id>",
      projectId="1459994676986774038",
      screenId="<screen_id>",
  )
  ```

## What's already shipped (don't redo)

- v0 hooks + state writer + atomic settings.json install (P0–P4)
- Read-only CLI: `cco list / status / tmux-widget` (P2)
- Basic Textual TUI: `cco tui` with DataTable + Enter-to-jump (P5/P6)
- tmux pane discovery via /proc walk + --resume argv match
- DEAD detection via os.kill(pid, 0) liveness check
- 103 tests passing, all checks green

## Why we paused

Conversation context hit 90%. Pushing through to port the design risked
leaving half-applied changes. Fresh session, full context, do it cleanly.
