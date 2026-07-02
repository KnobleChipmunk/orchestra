# orchestra

Phase 1 prototype: Claude Desktop orchestrating multiple concurrent Claude
Code workers via a FastMCP stdio server (WSL2) and a filesystem blackboard.

- Desktop calls instant MCP tools (`spawn_agent`, `send_to_agent`,
  `get_agent_status`, …); it never waits on a worker (Desktop's ~4-min
  tool-call timeout forbids it).
- Workers are interactive `claude` sessions in tmux windows, one git worktree
  each, subscription OAuth only (no API key).
- Workers report state via Stop/PostToolUse hooks writing `status.json` with a
  `BLOCKED:`/`DONE:` sentinel protocol; the orchestrator polls.

See `docs/setup.md` for install, Desktop config, and the smoke test.
Design rationale: `docs-research-phase1.md`; implementation notes: `CLAUDE.md`.
