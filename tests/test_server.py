"""Tests for the orchestrator MCP server tool functions.

subprocess.run is faked (captured, never executed) — no test spawns tmux or
claude. Status/inbox/log files are real files under tmp dirs.
"""
import json
from pathlib import Path

import pytest

from orchestra import server


class FakeRun:
    """Captures subprocess.run calls; pretends tmux session already exists."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()

    def commands(self) -> list[str]:
        return [" ".join(c) for c in self.calls]


@pytest.fixture()
def fake(monkeypatch, tmp_path: Path) -> FakeRun:
    f = FakeRun()
    monkeypatch.setattr(server, "RUN", f)
    monkeypatch.setattr(server, "AGENTS_DIR", tmp_path)
    return f


def _spawn(name: str = "alpha", prompt: str = "do the thing", workdir: str = "/wt") -> str:
    return server.spawn_agent(name, prompt, workdir)


# --- spawn_agent -----------------------------------------------------------

def test_spawn_writes_task_file_and_starting_status(fake: FakeRun) -> None:
    _spawn(prompt="add /health endpoint")
    d = server.AGENTS_DIR / "alpha"
    assert (d / "task.md").read_text() == "add /health endpoint"
    status = json.loads((d / "status.json").read_text())
    assert status["state"] == "starting"
    assert status["agent"] == "alpha"


def test_spawn_launches_bare_claude_without_stdin_redirect(fake: FakeRun) -> None:
    _spawn()
    new_window = next(c for c in fake.calls if c[:2] == ["tmux", "new-window"])
    launch = new_window[-1]
    assert "claude" in launch
    assert "<" not in launch  # stdin redirect kills interactivity
    assert "task.md" not in launch  # task delivered via send-keys, not at launch
    assert "| tee" not in launch  # piping stdout breaks the interactive TUI


def test_spawn_passes_permission_flags(fake: FakeRun) -> None:
    _spawn()
    launch = next(c for c in fake.calls if c[:2] == ["tmux", "new-window"])[-1]
    assert "--permission-mode acceptEdits" in launch
    assert "--allowedTools" in launch and "Read,Write,Edit,Bash" in launch


def test_spawn_configures_status_hooks(fake: FakeRun) -> None:
    _spawn()
    launch = next(c for c in fake.calls if c[:2] == ["tmux", "new-window"])[-1]
    assert "--settings" in launch
    assert "write_status" in launch


def test_spawn_logs_via_pipe_pane(fake: FakeRun) -> None:
    _spawn()
    pipe = next(c for c in fake.calls if c[:2] == ["tmux", "pipe-pane"])
    assert "agent.log" in " ".join(pipe)


def test_spawn_delivers_task_via_delayed_send_keys(fake: FakeRun) -> None:
    _spawn()
    # send-keys is scheduled in the background after a startup delay, and the
    # typed line points the worker at its task.md (single-line, tmux-safe).
    run_shell = next(c for c in fake.calls if c[:2] == ["tmux", "run-shell"])
    joined = " ".join(run_shell)
    assert "-b" in run_shell
    assert "sleep" in joined
    assert "send-keys" in joined
    assert "task.md" in joined


def test_spawn_returns_immediately_with_confirmation(fake: FakeRun) -> None:
    result = _spawn(name="beta", workdir="/home/x/beta-wt")
    assert "beta" in result and "/home/x/beta-wt" in result


# --- send_to_agent ---------------------------------------------------------

def test_send_appends_inbox_and_sends_single_line(fake: FakeRun) -> None:
    _spawn(name="beta")
    fake.calls.clear()
    server.send_to_agent("beta", "line one\nline two")
    inbox = (server.AGENTS_DIR / "beta" / "inbox.md").read_text()
    assert "line one\nline two" in inbox
    send = next(c for c in fake.calls if c[:2] == ["tmux", "send-keys"])
    typed = send[-2]  # last arg is "Enter"
    assert "\n" not in typed  # multiline send-keys is fragile


def test_send_to_unknown_agent_reports_error(fake: FakeRun) -> None:
    assert "No agent" in server.send_to_agent("ghost", "hello")


# --- status / list / log / stop --------------------------------------------

def test_get_agent_status_roundtrip(fake: FakeRun) -> None:
    _spawn(name="alpha")
    status = json.loads(server.get_agent_status("alpha"))
    assert status["state"] == "starting"


def test_get_agent_status_missing(fake: FakeRun) -> None:
    assert "No status" in server.get_agent_status("ghost")


def test_list_agents(fake: FakeRun) -> None:
    _spawn(name="alpha")
    _spawn(name="beta")
    listed = json.loads(server.list_agents())
    assert {a["agent"] for a in listed} == {"alpha", "beta"}


def test_list_agents_empty(fake: FakeRun) -> None:
    assert "No agents" in server.list_agents()


def test_read_agent_log_tails(fake: FakeRun) -> None:
    d = server.AGENTS_DIR / "alpha"
    d.mkdir(parents=True)
    (d / "agent.log").write_text("\n".join(f"line{i}" for i in range(100)))
    out = server.read_agent_log("alpha", tail=5)
    assert out.splitlines() == [f"line{i}" for i in range(95, 100)]


def test_read_agent_log_missing(fake: FakeRun) -> None:
    assert "No log" in server.read_agent_log("ghost")


def test_stop_agent_kills_window_and_marks_stopped(fake: FakeRun) -> None:
    _spawn(name="alpha")
    fake.calls.clear()
    server.stop_agent("alpha")
    assert any(c[:2] == ["tmux", "kill-window"] for c in fake.calls)
    assert json.loads(server.get_agent_status("alpha"))["state"] == "stopped"
