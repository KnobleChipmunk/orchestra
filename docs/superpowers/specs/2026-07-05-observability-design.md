# Design: Live observability layer for orchestra agents

**Date:** 2026-07-05
**Status:** Approved; **amended 2026-07-05** (see "Amendment" below)
**Branch:** `worktree-feature+observability`

## Amendment 2026-07-05 — detached emission (supersedes "synchronous POST")

The original design POSTed synchronously with a ≤2s timeout, on the assumption
that a **down** dashboard refuses instantly (~ms) and only a black-holed host
hits the timeout. **Measurement on the real WSL2 deployment host disproved
this:** a connect to an unbound `localhost:4000` (and explicit `127.0.0.1:4000`)
**black-holes for the full timeout** rather than refusing — WSL2 loopback
behavior. So "down" == "timeout", the normal case, not an edge case.

Measured per-hook cost (this WSL2 box, `TIMEOUT_S = 2.0`):

| Dashboard state | Synchronous POST | **Detached (adopted)** |
| --- | --- | --- |
| UP (listening) | ~0.8 ms | ~0.8 ms (child); hook returns ~20–30 ms |
| DOWN (nothing bound) | **~2000 ms** | **~20–30 ms** (hook process) |

With all-tools Pre+Post breadth, the synchronous down-case added ~4s **per tool
call** — silently violating the package's load-bearing constraint ("a dead/absent
dashboard must never block, slow, or fail an agent's turn") and the "orchestra
runs identically without it" promise, unless an operator remembered to set
`ORCHESTRA_EVENTS_URL=""`.

**Decision:** the POST runs in a **detached background process** (double-fork +
`setsid` + std-fd redirect to `/dev/null`). The hook process returns to Claude
Code immediately; the orphaned grandchild carries the (possibly slow) POST and
exits. `TIMEOUT_S` stays 2.0 — it now only bounds the orphaned child, never the
turn. The fork is isolated behind a `_detach()` seam (patched in tests, so
`run()` still asserts POST behavior in-process). This is the only change from
the approved design; the schema, wiring, env config, and breadth are unchanged.

## Goal

Every orchestra agent's hook events stream into disler's
[multi-agent-observability](https://github.com/disler/claude-code-hooks-multi-agent-observability)
dashboard at `localhost:5173`, tagged per-agent, with **zero change** to
orchestra's coordination architecture (blackboard, sentinels, status.json,
delivery, watchdog all untouched).

Two packages:
1. **Event emission** from orchestra's hook layer (this repo).
2. **Stand up the dashboard** — adopt disler's repo as sibling infra, smoke
   test, document.

## Non-negotiables (inherited, still binding)

- No API key anywhere (subscription OAuth only).
- Every MCP tool returns instantly.
- Every worker runs in its own git worktree.
- TDD: fake `subprocess`/network, real status files in tmp dirs, no test
  spawns tmux/claude or touches the network.
- **Emission is additive.** If any existing test breaks, the design is wrong,
  not the test.

## Verified external schema (source of truth: the repo, checked 2026-07-05)

disler's server: `POST http://localhost:4000/events`, WebSocket `ws://…:4000/stream`,
client at `localhost:5173`.

Required body fields (server returns `400 "Missing required fields"` if any
missing): `source_app`, `session_id`, `hook_event_type`, `payload`.

> **Correction to the handoff:** the handoff summary said map `app: "orchestra"`.
> The actual required key is **`source_app`**, not `app`. We use `source_app`.

Optional fields disler's own `send_event.py` includes: `timestamp` (ms epoch),
`model_name`, and various passthroughs. We include `timestamp`; everything
agent-specific rides inside `payload`.

## Package 1 — Event emission

### Component: `src/orchestra/emit_event.py` (new module)

A standalone hook handler, structurally parallel to `write_status.py`:
- CLI: `emit_event.py <agent_name>` — agent name from `argv[1]`, identical to
  how `write_status.py` resolves identity. This name is the event's
  `session_id` (per handoff: `session_id = <agent name>`).
- Reads the hook payload JSON from **stdin** (same contract as `write_status.py`).
- Builds one event and POSTs it:

  ```json
  {
    "source_app": "orchestra",
    "session_id": "<agent-name>",
    "hook_event_type": "<event['hook_event_name']>",
    "payload": <the raw hook event dict>,
    "timestamp": <int ms epoch>
  }
  ```

- **Fire-and-forget guarantee (load-bearing):**
  - stdlib `urllib.request` only — **no new dependencies**.
  - Module-level timeout constant `TIMEOUT_S = 2.0` (the handoff ceiling;
    localhost round-trips are <5ms, so this only bounds a black-hole host).
  - **Every** exception swallowed (URLError, timeout, socket error, malformed
    stdin, anything). The process always exits 0 and prints `{"continue": true}`
    so the hook never blocks the tool/turn.
  - Malformed stdin → swallow, **no POST** (nothing meaningful to emit).

- **Config via env:**
  - `ORCHESTRA_EVENTS_URL` — endpoint, default `http://localhost:4000/events`.
  - If `ORCHESTRA_EVENTS_URL` is set to the **empty string**, emission is
    disabled entirely: early return before any socket work.

### Why a separate module + separate hook command (not folded into `write_status.py`)

Decision: **separate**.
- "status.json writes are unchanged" becomes true *by construction* — status
  and emission run as different OS processes with different stdin copies. A
  hang, crash, or exception in emission cannot touch the status write.
- The fire-and-forget network call is fully isolated from the load-bearing
  status logic.
- Cost: one extra short-lived Python process per hook event on the worker
  (not on any MCP tool). Negligible, and never on orchestra's critical path.

### Hook wiring (in `server.py` `_launch_claude_window`)

`write_status.py` stays **exactly as-is** on its existing matchers. The emitter
is added as an independent second command, and PreToolUse is newly registered.

Emission breadth decision: **all tools** (no matcher) for Pre/PostToolUse — a
full activity stream is the point of the dashboard. `write_status.py` keeps its
narrow `Write|Edit|Bash` PostToolUse matcher, unchanged.

Resulting `hooks` block:

```jsonc
{
  "PreToolUse":  [ { "hooks": [ { "command": "<emit_cmd>" } ] } ],
  "PostToolUse": [
    { "matcher": "Write|Edit|Bash",
      "hooks": [ { "command": "<hook_cmd>" } ] },     // write_status, UNCHANGED
    { "hooks": [ { "command": "<emit_cmd>" } ] }       // emit, all tools
  ],
  "Stop": [
    { "hooks": [ { "command": "<hook_cmd>" },          // write_status, UNCHANGED
                 { "command": "<emit_cmd>" } ] }        // emit
  ]
}
```

where `emit_cmd = "<PYTHON> <path>/emit_event.py <name>"`, mirroring how
`hook_cmd` is built. `ORCHESTRA_EVENTS_URL` is *not* injected by the spawn path
— it inherits from the worker's environment, so operators enable/disable the
dashboard globally without re-spawning agents. (Default endpoint applies when
unset.)

### Agent name / identity resolution

Reuse `write_status.py`'s exact mechanism: the name is passed as `argv[1]` at
hook-registration time by `_launch_claude_window`. No new env var, no cwd
inference. Consistent by construction.

## Package 2 — Stand up the dashboard (adopt, don't build)

1. Clone disler's repo to `~/projects/multi-agent-observability` (sibling of
   orchestra, **not** inside it). Check for Bun; install if absent.
2. Run server + client per its README.
3. Live smoke test: spawn one throwaway worker on a trivial task in a scratch
   worktree; confirm its PreToolUse/PostToolUse/Stop events appear in the
   dashboard tagged `orchestra` / `<agent-name>`. Capture confirmation for the
   PR (screenshot or described).
4. Do **not** vendor disler's code into orchestra and do **not** modify his
   repo beyond config needed to run. If his server needs schema tweaks to
   accept our events, adapt *our* emitter, never his server.

### Docs: `docs/observability.md` (new)

Short operator doc: what it is, how to start it (server, client, the
`ORCHESTRA_EVENTS_URL` env var), the fire-and-forget guarantee, and that it is
**optional** infrastructure — orchestra runs identically without it.

## Testing (TDD, written first)

`tests/test_emit_event.py`, all network faked by patching
`orchestra.emit_event.urlopen` (or `urllib.request.urlopen`); real payloads
from fixture dicts/files; nothing touches the network or spawns anything.

Cases:
1. **Happy path** — valid stdin → exactly one POST to the default URL with the
   correct body (`source_app="orchestra"`, `session_id=<name>`,
   `hook_event_type` from `hook_event_name`, `payload` == raw event).
2. **Timeout swallowed** — `urlopen` raises `socket.timeout` → no exception
   escapes, exit 0.
3. **Connection refused swallowed** — `urlopen` raises `URLError` → swallowed.
4. **Disabled via env** — `ORCHESTRA_EVENTS_URL=""` → `urlopen` never called.
5. **Custom URL via env** — `ORCHESTRA_EVENTS_URL=http://host:9/events` → POST
   targets that URL.
6. **Malformed stdin** — non-JSON stdin → swallowed, `urlopen` never called.
7. **Hook wiring** — a `server.py` test asserting the generated settings JSON
   contains the emit command under PreToolUse, PostToolUse, and Stop, and that
   `write_status.py`'s existing Stop + `Write|Edit|Bash` PostToolUse entries
   are still present unchanged.

### Overhead measurement (definition of done)

Measure and report in the PR: wall-clock of one `emit_event` invocation when
the server is **down**. Expectation:
- Server not running (TCP refused) → milliseconds (fast-fail).
- Host black-holes packets → bounded by `TIMEOUT_S` (~2s worst case).

## Out of scope (explicit)

- Agent Teams / teammateMode.
- Custom blackboard-state event types (state/needs/inbox depth).
- Sandboxing skill.
- Any change to messaging, delivery, watchdog, or sentinel logic.

## Definition of done

- PR ready: emitter + hook wiring + tests green (pytest, ruff, mypy).
- Live smoke test passed with dashboard screenshot/description.
- `docs/observability.md` present.
- PR description states measured worst-case per-hook overhead when server down.
