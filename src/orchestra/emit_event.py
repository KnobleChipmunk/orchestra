#!/usr/bin/env python3
"""PreToolUse/PostToolUse/Stop hook handler that mirrors each hook event to the
multi-agent observability dashboard (disler's server).

Runs as an INDEPENDENT hook command alongside write_status.py — a separate
process with its own stdin copy — so status.json writes are unaffected by
anything here. Fire-and-forget: stdlib urllib only, short timeout, every
exception swallowed, always exit 0. A dead or absent dashboard must never
block, slow, or fail an agent's turn.

The POST is performed in a DETACHED background process (double-fork): the hook
process returns to Claude Code immediately, so even a black-holed dashboard
adds zero latency to the agent's turn. This matters on WSL2, where a connect
to an unbound localhost port black-holes for the full timeout rather than
refusing quickly — see docs/observability.md.

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
    dead dashboard can never affect the agent's turn.

    Pure and in-process (no forking): the unit of work a detached child runs,
    and the seam the behavior tests drive directly.
    """
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


def _detach() -> bool:
    """Double-fork a fully detached grandchild to carry the POST.

    Returns True in the grandchild (which should perform the POST and then
    exit), False in the original process (which should return immediately).
    Seam for tests: patched to avoid real forks.

    The grandchild redirects stdin/stdout/stderr to /dev/null so the hook's
    stdout pipe reaches EOF when the parent exits — otherwise Claude Code would
    block waiting on the fd the grandchild inherited, re-introducing the stall
    detaching is meant to remove.
    """
    if os.fork() > 0:
        # Original process: reap the immediately-exiting first child (no
        # zombie) and return so the hook completes at once.
        os.wait()
        return False
    os.setsid()
    if os.fork() > 0:
        os._exit(0)  # first child exits; grandchild is orphaned to init
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    return True


def emit(name: str, stdin_text: str) -> None:
    """Entry point: detach, then POST in the background grandchild.

    The parent returns instantly; the grandchild does the (possibly slow) POST
    and exits. Disabled emission short-circuits before any fork.
    """
    if resolve_url() is None:
        return
    if not _detach():
        return
    try:
        run(name, stdin_text)
    finally:
        os._exit(0)


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    try:
        emit(name, sys.stdin.read())
    except Exception:
        pass
    # Exit 0 = allow the Stop/tool to proceed normally (hook stdout protocol).
    print(json.dumps({"continue": True}))


if __name__ == "__main__":
    main()
