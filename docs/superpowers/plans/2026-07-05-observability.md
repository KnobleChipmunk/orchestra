# Observability Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

## Context

Orchestra agents currently report state only through the filesystem blackboard
(`status.json`, sentinels). There is no live, human-watchable view of what every
agent is doing turn-by-turn. This plan wires orchestra's hook layer to also
stream every hook event into disler's
[multi-agent-observability](https://github.com/disler/claude-code-hooks-multi-agent-observability)
dashboard (`localhost:5173`), tagged per-agent — **without changing any
coordination behavior** (blackboard, sentinels, `status.json`, delivery,
watchdog all untouched).

Design doc (approved): `docs/superpowers/specs/2026-07-05-observability-design.md`.

**Goal:** Every orchestra hook event (`PreToolUse`, `PostToolUse`, `Stop`) also
fire-and-forget POSTs to the observability dashboard, with zero risk to an
agent's turn if the dashboard is down or absent.

**Architecture:** A new standalone hook handler `emit_event.py` runs as an
*independent* second hook command alongside the existing `write_status.py` — a
separate OS process with its own stdin copy — so `status.json` writes are
unchanged *by construction*. Emission uses stdlib `urllib` only, a ≤2s timeout,
and swallows every exception.

**Tech Stack:** Python 3.10+ (stdlib `urllib`, no new deps), pytest, disler's
Bun + SQLite + Vue dashboard (adopted, not vendored).

## Global Constraints

- **No new dependencies.** stdlib `urllib.request` only.
- **No API key anywhere** (inherited; this work adds none).
- **Every MCP tool still returns instantly.** Emission runs in worker hook
  processes, never on an MCP tool's critical path.
- **Emission is additive.** If any existing test breaks, the design is wrong,
  not the test. All 107 existing tests must still pass.
- **Fire-and-forget is load-bearing.** A dead/absent dashboard must never block,
  slow, or fail an agent's turn. Timeout ≤ 2s, every exception swallowed, hook
  always exits 0.
- **Verified schema (source of truth: disler's repo, checked 2026-07-05):**
  `POST http://localhost:4000/events`; required body keys `source_app`,
  `session_id`, `hook_event_type`, `payload` (missing any → server `400`).
  Use `source_app: "orchestra"` (NOT `app` — the handoff summary was wrong).
- Config env var: `ORCHESTRA_EVENTS_URL`, default `http://localhost:4000/events`;
  **empty string disables emission entirely**.
- Run tests from the worktree with `PYTHONPATH=src` so the correct checkout is
  validated: `source /home/user/projects/orchestra/.venv/bin/activate` then
  `PYTHONPATH=src python -m pytest ...`.
- ruff line-length 100; type hints on every signature; `mypy src` clean.

---

### Task 0: Land the approved spec + plan under version control

Plan mode blocked committing these earlier. First implementation action is to
put the design doc (already committed at `9bc1125`) and this plan under
`docs/superpowers/` so the branch is self-describing.

**Files:**
- Create: `docs/superpowers/plans/2026-07-05-observability.md` (copy of this plan)

- [ ] **Step 1:** Copy this plan file into the repo:
  `cp /home/user/.claude/plans/review-the-orchestra-repo-scalable-sphinx.md docs/superpowers/plans/2026-07-05-observability.md`
- [ ] **Step 2:** Commit:
```bash
git add docs/superpowers/plans/2026-07-05-observability.md
git commit -m "Plan: observability layer implementation"
```

---

### Task 1: `emit_event.py` — the fire-and-forget emitter

**Files:**
- Create: `src/orchestra/emit_event.py`
- Test: `tests/test_emit_event.py`

**Interfaces (produced — later tasks/tests rely on these exact names):**
- `resolve_url() -> str | None` — endpoint, or `None` when disabled (env == "").
- `build_event(name: str, event: dict) -> dict` — the POST body.
- `post(url: str, body: dict) -> None` — does the `urlopen` POST (patched in tests).
- `run(name: str, stdin_text: str) -> None` — orchestrates, swallows all failure.
- `main() -> None` — CLI entry: `emit_event.py <agent_name>`.
- Module seam `_now_ms() -> int` (monkeypatched in tests for a stable timestamp).
- Module-level `SOURCE_APP = "orchestra"`, `DEFAULT_URL`, `EVENTS_URL_ENV`,
  `TIMEOUT_S = 2.0`.
- Tests patch `orchestra.emit_event.urlopen` (module imports the name directly).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_emit_event.py
"""Tests for the observability emit hook handler.

The network is always faked (urlopen patched); no test touches the network or
spawns anything. Payloads are plain dicts, mirroring test_write_status.py.
"""
import json

import pytest

from orchestra import emit_event


class FakeUrlopen:
    """Captures the Request passed to urlopen; optionally raises to simulate
    a down/black-holed dashboard."""

    def __init__(self, raise_exc: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.raise_exc = raise_exc

    def __call__(self, req, timeout=None):
        if self.raise_exc is not None:
            raise self.raise_exc
        body = json.loads(req.data.decode())
        self.calls.append((req.full_url, body))

        class R:  # context-manager-free stand-in; run() calls .close()
            def close(self_inner) -> None:
                return None

        return R()


@pytest.fixture()
def fake_urlopen(monkeypatch):
    f = FakeUrlopen()
    monkeypatch.setattr(emit_event, "urlopen", f)
    monkeypatch.setattr(emit_event, "_now_ms", lambda: 1234)
    return f


def test_happy_path_posts_correct_event(fake_urlopen, monkeypatch) -> None:
    monkeypatch.delenv("ORCHESTRA_EVENTS_URL", raising=False)
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash"}
    emit_event.run("alpha", json.dumps(payload))
    assert len(fake_urlopen.calls) == 1
    url, body = fake_urlopen.calls[0]
    assert url == "http://localhost:4000/events"
    assert body == {
        "source_app": "orchestra",
        "session_id": "alpha",
        "hook_event_type": "PreToolUse",
        "payload": payload,
        "timestamp": 1234,
    }


def test_timeout_is_swallowed(monkeypatch) -> None:
    import socket
    monkeypatch.delenv("ORCHESTRA_EVENTS_URL", raising=False)
    monkeypatch.setattr(emit_event, "urlopen", FakeUrlopen(socket.timeout()))
    emit_event.run("alpha", json.dumps({"hook_event_name": "Stop"}))  # no raise


def test_connection_refused_is_swallowed(monkeypatch) -> None:
    from urllib.error import URLError
    monkeypatch.delenv("ORCHESTRA_EVENTS_URL", raising=False)
    monkeypatch.setattr(emit_event, "urlopen",
                        FakeUrlopen(URLError("Connection refused")))
    emit_event.run("alpha", json.dumps({"hook_event_name": "Stop"}))  # no raise


def test_disabled_via_empty_env_never_posts(monkeypatch) -> None:
    monkeypatch.setenv("ORCHESTRA_EVENTS_URL", "")
    f = FakeUrlopen()
    monkeypatch.setattr(emit_event, "urlopen", f)
    emit_event.run("alpha", json.dumps({"hook_event_name": "Stop"}))
    assert f.calls == []


def test_custom_url_via_env(fake_urlopen, monkeypatch) -> None:
    monkeypatch.setenv("ORCHESTRA_EVENTS_URL", "http://host:9999/events")
    emit_event.run("alpha", json.dumps({"hook_event_name": "Stop"}))
    assert fake_urlopen.calls[0][0] == "http://host:9999/events"


def test_malformed_stdin_never_posts(fake_urlopen, monkeypatch) -> None:
    monkeypatch.delenv("ORCHESTRA_EVENTS_URL", raising=False)
    emit_event.run("alpha", "this is not json {")
    assert fake_urlopen.calls == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src python -m pytest tests/test_emit_event.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'orchestra.emit_event'`.

- [ ] **Step 3: Write the implementation**

```python
# src/orchestra/emit_event.py
#!/usr/bin/env python3
"""PreToolUse/PostToolUse/Stop hook handler that mirrors each hook event to the
multi-agent observability dashboard (disler's server).

Runs as an INDEPENDENT hook command alongside write_status.py — a separate
process with its own stdin copy — so status.json writes are unaffected by
anything here. Fire-and-forget: stdlib urllib only, short timeout, every
exception swallowed, always exit 0. A dead or absent dashboard must never
block, slow, or fail an agent's turn.

Usage (wired via --settings at spawn): emit_event.py <agent_name>
"""
import json
import os
import sys
import time
from urllib.request import Request, urlopen

SOURCE_APP = "orchestra"
EVENTS_URL_ENV = "ORCHESTRA_EVENTS_URL"
DEFAULT_URL = "http://localhost:4000/events"
# Handoff ceiling. localhost round-trips are <5ms and a not-running dashboard
# refuses instantly; this only bounds a silently black-holed host. Drop to 0.5
# if per-hook latency under a network fault ever matters.
TIMEOUT_S = 2.0


def _now_ms() -> int:
    """Millisecond epoch. Seam for tests."""
    return int(time.time() * 1000)


def resolve_url() -> str | None:
    """Endpoint URL, or None if emission is disabled.

    Env unset -> default endpoint. Env set to "" -> disabled (explicit opt-out).
    """
    val = os.environ.get(EVENTS_URL_ENV)
    if val is None:
        return DEFAULT_URL
    if val == "":
        return None
    return val


def build_event(name: str, event: dict) -> dict:
    """The observability POST body. `name` is both the per-agent tag and the
    dashboard session id; the raw hook event rides inside `payload`."""
    return {
        "source_app": SOURCE_APP,
        "session_id": name,
        "hook_event_type": event.get("hook_event_name", ""),
        "payload": event,
        "timestamp": _now_ms(),
    }


def post(url: str, body: dict) -> None:
    """POST one event as JSON. Raises on any network error (caller swallows)."""
    data = json.dumps(body).encode()
    req = Request(url, data=data,
                  headers={"Content-Type": "application/json"}, method="POST")
    urlopen(req, timeout=TIMEOUT_S).close()


def run(name: str, stdin_text: str) -> None:
    """Parse stdin, build the event, POST it — swallowing every failure so a
    dead dashboard can never affect the agent's turn."""
    url = resolve_url()
    if url is None:
        return
    try:
        event = json.loads(stdin_text)
    except (json.JSONDecodeError, ValueError):
        return
    if not isinstance(event, dict):
        return
    try:
        post(url, build_event(name, event))
    except Exception:
        pass


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    try:
        run(name, sys.stdin.read())
    except Exception:
        pass
    # Exit 0 = allow the Stop/tool to proceed normally (hook stdout protocol).
    print(json.dumps({"continue": True}))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src python -m pytest tests/test_emit_event.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Lint + type check the new module**

Run: `ruff check src/orchestra/emit_event.py tests/test_emit_event.py && mypy src/orchestra/emit_event.py`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add src/orchestra/emit_event.py tests/test_emit_event.py
git commit -m "feat: fire-and-forget observability event emitter"
```

---

### Task 2: Wire the emitter into the spawn hook config

Add `emit_event.py` as an independent second hook command in
`_launch_claude_window`, on `Stop` + `PostToolUse` (alongside the unchanged
`write_status.py`) and on a **new** `PreToolUse`. Emit fires for **all tools**
(no matcher); `write_status.py` keeps its narrow `Write|Edit|Bash` matcher.

**Files:**
- Modify: `src/orchestra/server.py` (constant near line 28; `_launch_claude_window` settings block ~lines 106-117)
- Test: `tests/test_server.py` (extend, near existing `test_spawn_configures_status_hooks` ~line 82)

**Interfaces:**
- Consumes: `emit_event.py` at `src/orchestra/emit_event.py` (from Task 1).
- Produces: generated `--settings` JSON whose `hooks` block contains the emit
  command on `PreToolUse`, `PostToolUse`, and `Stop`.

- [ ] **Step 1: Write the failing test** (add to `tests/test_server.py`; add
  `import shlex` at the top if not present)

```python
def _settings_of(launch: str) -> dict:
    """Recover the --settings JSON dict embedded in a tmux launch command."""
    toks = shlex.split(launch)
    return json.loads(toks[toks.index("--settings") + 1])


def test_spawn_wires_emit_event_on_all_three_hooks(fake: FakeRun) -> None:
    _spawn()
    launch = next(c for c in fake.calls if c[:2] == ["tmux", "new-window"])[-1]
    hooks = _settings_of(launch)["hooks"]
    assert "emit_event" in json.dumps(hooks["PreToolUse"])
    assert "emit_event" in json.dumps(hooks["PostToolUse"])
    assert "emit_event" in json.dumps(hooks["Stop"])


def test_spawn_preserves_write_status_hooks_unchanged(fake: FakeRun) -> None:
    _spawn()
    launch = next(c for c in fake.calls if c[:2] == ["tmux", "new-window"])[-1]
    hooks = _settings_of(launch)["hooks"]
    # write_status still on Stop and ONLY on the narrow PostToolUse matcher.
    assert "write_status" in json.dumps(hooks["Stop"])
    post_groups = hooks["PostToolUse"]
    narrow = next(g for g in post_groups if g.get("matcher") == "Write|Edit|Bash")
    assert "write_status" in json.dumps(narrow)
    assert all(
        "write_status" not in json.dumps(g)
        for g in post_groups if g.get("matcher") != "Write|Edit|Bash"
    )
    # PreToolUse is emit-only — never write_status.
    assert "write_status" not in json.dumps(hooks["PreToolUse"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src python -m pytest tests/test_server.py -k "emit_event or write_status_hooks_unchanged" -v`
Expected: FAIL — `KeyError: 'PreToolUse'` / no `emit_event` in settings.

- [ ] **Step 3: Add the module constant** (in `server.py`, right after
  `HOOK_HANDLER = ...` near line 28):

```python
EMIT_HANDLER = Path(__file__).resolve().parent / "emit_event.py"
```

- [ ] **Step 4: Rewrite the settings hooks block** in `_launch_claude_window`
  (replace the current `hook_cmd = ...` + `settings = json.dumps({...})` block,
  ~lines 106-117):

```python
    hook_cmd = f"{PYTHON} {shlex.quote(str(HOOK_HANDLER))} {shlex.quote(name)}"
    emit_cmd = f"{PYTHON} {shlex.quote(str(EMIT_HANDLER))} {shlex.quote(name)}"
    settings = json.dumps({
        # Auto-approve project-scope .mcp.json (the lead's worktree carries
        # one so it can spawn workers); an approval dialog would wedge the
        # session exactly like the folder-trust prompt did.
        "enableAllProjectMcpServers": True,
        "hooks": {
            # emit_event mirrors every hook to the observability dashboard,
            # fire-and-forget, independent of the status write. All tools.
            "PreToolUse": [
                {"hooks": [{"type": "command", "command": emit_cmd}]},
            ],
            "Stop": [
                {"hooks": [
                    {"type": "command", "command": hook_cmd},
                    {"type": "command", "command": emit_cmd},
                ]},
            ],
            "PostToolUse": [
                {"matcher": "Write|Edit|Bash",
                 "hooks": [{"type": "command", "command": hook_cmd}]},
                {"hooks": [{"type": "command", "command": emit_cmd}]},
            ],
        }
    })
```

- [ ] **Step 5: Run the new + existing wiring tests**

Run: `PYTHONPATH=src python -m pytest tests/test_server.py -v`
Expected: PASS — including the pre-existing `test_spawn_configures_status_hooks`
(still true: `write_status` present in launch).

- [ ] **Step 6: Full suite + lint + types (regression gate)**

Run: `PYTHONPATH=src python -m pytest -q && ruff check src tests && mypy src`
Expected: all pass (was 107; now 107 + 8 new = 115), no lint/type errors.

- [ ] **Step 7: Commit**

```bash
git add src/orchestra/server.py tests/test_server.py
git commit -m "feat: wire emit_event into spawn hooks (Pre/Post/Stop)"
```

---

### Task 3: Operator docs — `docs/observability.md`

**Files:**
- Create: `docs/observability.md`

- [ ] **Step 1:** Write the doc. Required content: what it is; how to start it
  (server, client, `ORCHESTRA_EVENTS_URL`); the fire-and-forget guarantee; that
  it is **optional** (orchestra runs identically without it). Skeleton:

```markdown
# Observability (optional)

Live per-agent dashboard of every orchestra hook event, via disler's
[multi-agent-observability](https://github.com/disler/claude-code-hooks-multi-agent-observability).
**Optional infrastructure — orchestra runs identically without it.**

## What it is
Each agent's `PreToolUse` / `PostToolUse` / `Stop` hooks fire-and-forget POST a
JSON event to a local dashboard server, tagged `source_app=orchestra`,
`session_id=<agent-name>`. The blackboard, sentinels, and `status.json` are
unaffected — emission is a separate hook process from the status write.

## Start it
1. Clone + run disler's dashboard (sibling repo, not inside orchestra):
   `~/projects/multi-agent-observability` — server on `:4000`, client on `:5173`
   (see its README; needs Bun).
2. Agents emit automatically. To point elsewhere or disable:
   - `ORCHESTRA_EVENTS_URL` (default `http://localhost:4000/events`).
   - Set `ORCHESTRA_EVENTS_URL=""` to disable emission entirely.
   The var inherits from each worker's environment — toggle globally, no respawn.

## Fire-and-forget guarantee
Emission uses stdlib `urllib`, a {TIMEOUT_S}s timeout, and swallows every
exception; the hook always exits 0. A dead or absent dashboard never blocks,
slows, or fails an agent's turn. Worst case per hook when the host black-holes
packets ≈ the timeout; a not-running dashboard refuses instantly (~ms).
```

  (Replace `{TIMEOUT_S}` with the actual `2.0`, and fill the measured overhead
  number from Task 5.)

- [ ] **Step 2: Commit**

```bash
git add docs/observability.md
git commit -m "docs: observability layer operator guide"
```

---

### Task 4: Stand up disler's dashboard (Package 2 — manual, side-effecting)

Not TDD — this is real infra setup outside the repo. Do it as its own verifiable
step after Tasks 1-3 are green.

- [ ] **Step 1:** Check for Bun: `command -v bun || echo MISSING`. If missing,
  install per disler's README (`curl -fsSL https://bun.sh/install | bash`).
- [ ] **Step 2:** Clone sibling repo (NOT inside orchestra):
  `git clone https://github.com/disler/claude-code-hooks-multi-agent-observability ~/projects/multi-agent-observability`
- [ ] **Step 3:** Start server + client per its README. Confirm `:4000` (events
  + `/stream`) and client `:5173` are up (e.g. `curl -s localhost:4000/events`
  returns non-connection-error; open `localhost:5173`).
- [ ] **Step 4:** Do NOT vendor disler's code into orchestra and do NOT modify
  his repo beyond config needed to run.

---

### Task 5: Live smoke test + overhead measurement (Definition of Done)

- [ ] **Step 1: Measure worst-case overhead** (for the PR / DoD line). Run this
  BEFORE Task 4 starts the dashboard (so "not running" is true), or against a
  guaranteed-unused port. Run from the worktree:

```bash
source /home/user/projects/orchestra/.venv/bin/activate
# (a) dashboard NOT running -> connection refused, should be ~milliseconds
PYTHONPATH=src ORCHESTRA_EVENTS_URL=http://localhost:4000/events python - <<'PY'
import json, time
from orchestra import emit_event
payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash"})
t = time.perf_counter()
for _ in range(50):
    emit_event.run("bench", payload)
print("refused mean ms:", round((time.perf_counter() - t) / 50 * 1000, 2))
PY
# (b) black-holed host -> bounded by TIMEOUT_S (~2s). Non-routable IP:
PYTHONPATH=src ORCHESTRA_EVENTS_URL=http://10.255.255.1:4000/events python - <<'PY'
import json, time
from orchestra import emit_event
t = time.perf_counter()
emit_event.run("bench", json.dumps({"hook_event_name": "Stop"}))
print("black-hole one-call s:", round(time.perf_counter() - t, 2))
PY
```
Record both numbers for the PR description.

- [ ] **Step 2: Live end-to-end smoke test.** With the dashboard running
  (Task 4), spawn one throwaway worker on a trivial task in a scratch worktree
  via the orchestrator MCP tools (or a manual `claude` launch mirroring
  `_launch_claude_window`). Have it run a couple of tools and stop.
- [ ] **Step 3: Confirm in the dashboard** that this agent's
  `PreToolUse`/`PostToolUse`/`Stop` events appear tagged `orchestra` /
  `<agent-name>`. Screenshot or describe for the PR. Clean up the scratch
  worker/worktree afterward.
- [ ] **Step 4:** Backfill the measured overhead + timeout note into
  `docs/observability.md` (Task 3) and commit if changed.

---

### Task 6: Open the PR

- [ ] **Step 1:** Push the branch and open a PR to `main` (per project
  convention — no direct pushes to main). **Do not push without explicit user
  confirmation first** (global rule). Show the commits and ask before pushing.
- [ ] **Step 2: PR description must include:**
  - Justification for separate module vs folding into `write_status.py`.
  - The `source_app` schema correction vs the handoff.
  - The measured worst-case per-hook overhead when the server is down (Task 5),
    plus the note: dropping `TIMEOUT_S` to 0.5s is the knob if it ever matters.
  - Smoke-test screenshot/description.

## Verification

- **Unit/regression (offline, authoritative):**
  `source /home/user/projects/orchestra/.venv/bin/activate && PYTHONPATH=src python -m pytest -q && ruff check src tests && mypy src`
  — all green, existing 107 tests unaffected, ~8 new tests pass.
- **Emitter behavior:** covered by `tests/test_emit_event.py` (happy path,
  timeout swallowed, connection-refused swallowed, disabled-via-env, custom URL,
  malformed stdin).
- **Wiring:** `tests/test_server.py` asserts emit on all three hooks and
  `write_status.py` unchanged on its narrow matcher; the pre-existing
  `test_spawn_configures_status_hooks` still passes.
- **Overhead:** Task 5 Step 1 prints refused-mean-ms (~ms) and black-hole-call-s
  (~2s) — the DoD numbers.
- **Live:** Task 5 Steps 2-3 — real agent events visible in the dashboard.

## Notes / risks

- WSL2: `localhost` from a worker hook resolves within the distro; only
  investigate networking if the live smoke test (Task 5) fails.
- Tasks 1-3 are fully offline and TDD — land them first as the mergeable core.
  Tasks 4-5 are side-effecting infra/manual verification; sequence them after.
- `_now_ms` is a monkeypatch seam only; production always uses real time.
