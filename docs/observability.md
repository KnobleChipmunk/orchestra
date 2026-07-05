# Observability (optional)

A live, per-agent dashboard of every orchestra hook event, via disler's
[multi-agent-observability](https://github.com/disler/claude-code-hooks-multi-agent-observability).

**Optional infrastructure — orchestra runs identically without it.** No agent
behavior, coordination, or status reporting depends on the dashboard; emission
is purely additive and detached (see the guarantee below).

## What it is

Each agent's `PreToolUse` / `PostToolUse` / `Stop` hooks fire-and-forget POST a
JSON event to a local dashboard server, tagged `source_app=orchestra`,
`session_id=<agent-name>`. You get a live stream of what every agent is doing,
turn by turn, tool by tool.

The blackboard, sentinels, and `status.json` are untouched: `emit_event.py`
runs as a **separate hook command in its own process**, independent of the
`write_status.py` status write. `write_status.py` still owns `Stop` and its
narrow `Write|Edit|Bash` `PostToolUse` matcher exactly as before; the emitter is
added alongside it (and on a new `PreToolUse`), firing for **all** tools.

Event body (matches disler's server schema — verified against the repo):

```json
{
  "source_app": "orchestra",
  "session_id": "<agent-name>",
  "hook_event_type": "PreToolUse | PostToolUse | Stop",
  "payload": { ...the raw hook event... },
  "timestamp": 1720000000000
}
```

## Start it

1. Clone + run disler's dashboard as a **sibling** repo (not inside orchestra;
   it is not vendored):

   ```bash
   git clone https://github.com/disler/claude-code-hooks-multi-agent-observability \
     ~/projects/multi-agent-observability
   # needs Bun (https://bun.sh). Follow that repo's README to start:
   #   server -> http://localhost:4000  (POST /events, WS /stream)
   #   client -> http://localhost:5173
   ```

2. Agents emit automatically once spawned — no per-agent config. To point
   elsewhere or turn emission off, set in the worker environment:

   - `ORCHESTRA_EVENTS_URL` — endpoint. Default `http://localhost:4000/events`.
   - `ORCHESTRA_EVENTS_URL=""` (empty string) — **disables emission entirely**
     (short-circuits before any network or fork work).

   The variable inherits from each worker's environment, so you toggle the
   dashboard globally — no need to re-spawn agents.

## Fire-and-forget guarantee

Emission must never block, slow, or fail an agent's turn — this is the
load-bearing constraint of the feature. It is enforced two ways:

- **Detached POST.** The hook process double-forks a `setsid`'d grandchild
  (with stdin/stdout/stderr redirected to `/dev/null`) that carries the POST
  and exits; the hook process itself returns to Claude Code immediately. So the
  agent's turn cost is independent of whether the dashboard is up, down, slow,
  or black-holing.
- **Swallow everything.** stdlib `urllib` only (no dependencies), a 2s timeout
  on the (backgrounded) request, and every exception swallowed. The hook always
  exits 0.

### Measured overhead (WSL2 host, `TIMEOUT_S = 2.0`)

| Dashboard state | Hook process return time |
| --- | --- |
| UP (listening on 4000) | ~20–30 ms (POST itself ~0.8 ms in the child) |
| DOWN (nothing bound) | ~20–30 ms |

**WSL2 note (why detaching matters here):** on WSL2, a TCP connect to an
unbound `localhost:4000` — or even explicit `127.0.0.1:4000` — does **not**
return `ECONNREFUSED` quickly; it **black-holes for the full timeout** (~2s).
A synchronous POST would therefore add ~2s to *every* hook when the dashboard
is down (≈4s per tool call with Pre+Post), which is exactly why the POST is
detached. `TIMEOUT_S` now only bounds the orphaned background child, never the
turn. If you ever run the emitter synchronously (you shouldn't), 0.5s is the
timeout knob.
