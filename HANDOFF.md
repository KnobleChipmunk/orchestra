---
created: 2026-07-02 13:15:00
from: windows
project: orchestra (multi-agent orchestrator, Phase 1 prototype)
urgency: medium
---

# Handoff: Build the Phase 1 Desktop→Code orchestrator prototype

## Context
Nate wants Claude Desktop orchestrating multiple concurrent Claude Code workers
(Phase 1), eventually peer-to-peer via a blackboard (Phase 2). Claude Desktop
ran a discovery session; its full findings are in `docs-research-phase1.md`
(this directory) — read it first, especially Deliverable 5 (prototype spec) and
the Recommendations. The architecture is sound and the previous session's
review confirmed the primitive inventory. Auth is subscription OAuth
throughout; NO API key anywhere (blank/unset ANTHROPIC_API_KEY — same billing
trap as prompt-improver).

## Current State
- Nothing built yet. This directory is the intended project home (standard new
  project setup still needed: pyproject, .gitignore, .env/.env.example,
  CLAUDE.md, src/tests/docs/scripts).
- The research doc's prototype spec (Deliverable 5) is the blueprint, BUT it has
  three confirmed bugs to fix during implementation (below).
- Related working reference: ~/projects/prompt-improver (same FastMCP-adjacent
  stack, TDD conventions, isolation/billing patterns, wsl.exe Desktop bridge
  precedent in memory `reference_mcp_wsl_desktop_bridge`).

## Next Steps
1. Standard project setup in ~/projects/orchestra; git init on a feature
   branch; private GitHub repo under KnobleChipmunk when Nate says so.
2. Build the orchestrator MCP server (FastMCP, stdio) + write_status.py hook
   handler per Deliverable 5, TDD throughout (fake subprocess.run; real status
   files in tmp dirs; no test spawns tmux or claude).
3. Apply the three spec fixes:
   - **Launch bug:** do NOT launch `claude < task.md` (stdin redirect kills
     interactivity). Launch bare `claude` in the tmux window, then deliver the
     task prompt via `tmux send-keys` after a startup delay.
   - **Permissions:** workers start in auto mode — Nate's decision. Pass
     `--permission-mode acceptEdits` (escalate to bypassPermissions only if
     Bash approval prompts still stall unattended workers) plus explicit
     `--allowedTools "Read,Write,Edit,Bash"` at spawn. Safe because every
     worker is confined to its own git worktree.
   - **False done:** Stop hook fires on every turn end, not task completion.
     Add a `DONE:` sentinel symmetric with `BLOCKED:` in the status protocol;
     Stop without a sentinel maps to `idle/awaiting-input`, not `done`.
4. Live smoke test = the doc's pass criteria: one task to Desktop → ≥2
   concurrent workers in tmux → beta hits its engineered blocker →
   status.json flips to blocked → Desktop polls, sees it, reacts via
   send_to_agent. Nate drives Desktop; Claude can watch tmux/status files.

## Blockers or Warnings
- Desktop MCP tool calls hard-timeout ~4 min and there is no server push —
  every tool must return instantly; workers communicate ONLY via status files
  (the whole design flows from this; don't fight it).
- Desktop config lives at %APPDATA%\Claude\claude_desktop_config.json (Windows
  side); bridge command is `wsl.exe -e bash -lc "..."`. Fully quit Desktop
  (not just close) to reload config.
- tmux send-keys with multiline/quoted text is fragile — send single-line
  messages or write-to-inbox + short notify line.
- Research doc caveats worth honoring: don't build on `--input-format
  stream-json` (undocumented), `--bare` (needs API key), Channels/Agent Teams
  (research preview/experimental).

## Key Files
- /home/tusk/projects/orchestra/docs-research-phase1.md — the full research doc
- /home/tusk/projects/orchestra/HANDOFF.md — this note
- /home/tusk/projects/prompt-improver/ — conventions reference (TDD, pyproject,
  README/CLAUDE.md style, billing trap)
- Memory: project_prompt_improver.md, reference_mcp_wsl_desktop_bridge.md
