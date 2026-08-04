# Orchestra — Phase 1 Desktop→Code Orchestrator

Claude Desktop (Windows) orchestrates concurrent Claude Code workers (WSL2 tmux
windows) through a FastMCP stdio server and a filesystem blackboard.

## Non-negotiables
- **No API key anywhere.** All auth is subscription OAuth (`ANTHROPIC_API_KEY`
  unset). If it's set, `claude -p` silently bills the API.
- **Every MCP tool returns instantly.** Claude Desktop hard-timeouts tool calls
  at ~4 minutes with no server push; workers communicate only via status files.
- **Every worker runs in its own git worktree.** `spawn_agent` workdir must be
  a worktree; two agents never edit the same file.
- **TDD.** Fake `subprocess.run` (patch `server.RUN`); real status files in tmp
  dirs; no test may spawn tmux or claude.

## Layout
- `src/orchestra/server.py` — MCP server (tools: spawn_agent, send_to_agent,
  get_agent_status, list_agents, read_agent_log, stop_agent)
- `src/orchestra/write_status.py` — Stop/PostToolUse hook handler workers run
- `docs/` — Desktop config snippet, orchestrator prompt, smoke-test guide
- Blackboard at `$ORCHESTRA_AGENTS_DIR` (default `/home/user/agents`):
  `<name>/status.json`, `inbox.md`, `agent.log`, `task.md`

## Status protocol (workers)
- `BLOCKED: <reason>` → state=blocked, needs orchestrator input
- `DONE: <summary>` → state=done
- Stop with neither sentinel → state=idle (awaiting input) — Stop fires on
  every turn end, so bare Stop NEVER means done.

## Dev
```bash
source .venv/bin/activate
pytest && ruff check src tests && mypy src
```
