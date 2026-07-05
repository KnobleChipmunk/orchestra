"""Tests for the orchestrator MCP server tool functions.

subprocess.run is faked (captured, never executed) — no test spawns tmux or
claude. Status/inbox/log files are real files under tmp dirs.
"""
import json
import shlex
from pathlib import Path

import pytest

from orchestra import server


class FakeRun:
    """Captures subprocess.run calls; pretends tmux session already exists.

    `git_status_stdout` lets tests simulate a dirty/clean worktree for the
    `git status --porcelain` check stop_agent runs before killing a window.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.git_status_stdout = ""

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        is_git_status = cmd[:1] == ["git"] and "status" in cmd

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        if is_git_status:
            R.stdout = self.git_status_stdout
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


# --- trust pre-seeding and duplicate-spawn guard ----------------------------

def test_spawn_pre_trusts_workdir_in_claude_json(fake: FakeRun, monkeypatch, tmp_path: Path) -> None:
    # Workers boot into the folder-trust dialog unless the workdir is already
    # trusted in ~/.claude.json; spawn must pre-seed it or the agent wedges.
    cfg = tmp_path / "claude.json"
    cfg.write_text(json.dumps({"projects": {"/existing": {"foo": 1}}, "theme": "dark"}))
    monkeypatch.setattr(server, "CLAUDE_JSON", cfg)
    _spawn(workdir="/wt")
    data = json.loads(cfg.read_text())
    assert data["projects"]["/wt"]["hasTrustDialogAccepted"] is True
    assert data["projects"]["/existing"] == {"foo": 1}  # existing config preserved
    assert data["theme"] == "dark"


def test_spawn_pre_trust_creates_claude_json_if_missing(
    fake: FakeRun, monkeypatch, tmp_path: Path
) -> None:
    cfg = tmp_path / "claude.json"
    monkeypatch.setattr(server, "CLAUDE_JSON", cfg)
    _spawn(workdir="/wt")
    assert json.loads(cfg.read_text())["projects"]["/wt"]["hasTrustDialogAccepted"] is True


def test_spawn_refuses_duplicate_active_agent(fake: FakeRun) -> None:
    # Desktop retried spawn_agent during the smoke test and got 3 windows all
    # named alpha; a second spawn for a live agent must refuse, not duplicate.
    _spawn(name="alpha")
    fake.calls.clear()
    result = server.spawn_agent("alpha", "again", "/wt")
    assert "already" in result.lower()
    assert not any(c[:2] == ["tmux", "new-window"] for c in fake.calls)


def test_spawn_allowed_again_after_stop(fake: FakeRun) -> None:
    _spawn(name="alpha")
    server.stop_agent("alpha")
    fake.calls.clear()
    result = server.spawn_agent("alpha", "again", "/wt")
    assert "Spawned" in result
    assert any(c[:2] == ["tmux", "new-window"] for c in fake.calls)


def test_spawn_allowed_again_after_done(fake: FakeRun) -> None:
    _spawn(name="alpha")
    status_path = server.AGENTS_DIR / "alpha" / "status.json"
    s = json.loads(status_path.read_text())
    s["state"] = "done"
    status_path.write_text(json.dumps(s))
    assert "Spawned" in server.spawn_agent("alpha", "again", "/wt")


# --- Enter must be sent separately (TUI paste detection swallows it) --------

def test_spawn_kickoff_sends_enter_separately(fake: FakeRun) -> None:
    _spawn()
    deliver = next(c for c in fake.calls if c[:2] == ["tmux", "run-shell"])[-1]
    # Two send-keys: text first, Enter alone after a pause.
    assert deliver.count("send-keys") == 2
    text_part, enter_part = deliver.rsplit("send-keys", 1)
    assert "Enter" not in text_part.split("send-keys", 1)[1]
    assert "Enter" in enter_part
    assert "sleep" in text_part.split("send-keys", 1)[1] + enter_part.split("Enter")[0] \
        or "sleep 1" in deliver


def test_send_to_agent_sends_enter_separately(fake: FakeRun, monkeypatch) -> None:
    monkeypatch.setattr(server, "SLEEP", lambda s: None)
    _spawn(name="beta")
    fake.calls.clear()
    server.send_to_agent("beta", "commit this")
    sends = [c for c in fake.calls if c[:2] == ["tmux", "send-keys"]]
    assert len(sends) == 2
    assert "Enter" not in sends[0]
    assert sends[1][-1] == "Enter" and "commit this" not in " ".join(sends[1])


# --- v2: model selection and notify plumbing ---------------------------------

def test_spawn_passes_default_sonnet_model(fake: FakeRun) -> None:
    _spawn()
    launch = next(c for c in fake.calls if c[:2] == ["tmux", "new-window"])[-1]
    assert "--model sonnet" in launch


def test_spawn_model_override_for_lead(fake: FakeRun) -> None:
    server.spawn_agent("lead", "coordinate", "/wt", model="opus")
    launch = next(c for c in fake.calls if c[:2] == ["tmux", "new-window"])[-1]
    assert "--model opus" in launch


def test_spawn_injects_notify_and_config_env(fake: FakeRun) -> None:
    server.spawn_agent("w1", "task", "/wt", notify_target="lead")
    launch = next(c for c in fake.calls if c[:2] == ["tmux", "new-window"])[-1]
    assert "ORCHESTRA_NOTIFY_TARGET=lead" in launch
    assert "ORCHESTRA_TMUX_SESSION=" in launch
    assert "ORCHESTRA_AGENTS_DIR=" in launch


def test_spawn_without_notify_target_sets_no_notify_env(fake: FakeRun) -> None:
    _spawn()
    launch = next(c for c in fake.calls if c[:2] == ["tmux", "new-window"])[-1]
    assert "ORCHESTRA_NOTIFY_TARGET" not in launch


def test_spawn_enables_project_mcp_servers(fake: FakeRun) -> None:
    # The lead spawns workers via the orchestrator MCP tools; its session must
    # auto-approve the project-scope .mcp.json in its worktree (a dialog would
    # wedge it exactly like the trust prompt did).
    _spawn()
    launch = next(c for c in fake.calls if c[:2] == ["tmux", "new-window"])[-1]
    assert "enableAllProjectMcpServers" in launch


def test_spawn_preapproves_orchestrator_mcp_tools(fake: FakeRun) -> None:
    # acceptEdits does not cover MCP tools; without an allowlist entry the
    # lead wedges on a permission dialog at its first orchestrator call.
    _spawn()
    launch = next(c for c in fake.calls if c[:2] == ["tmux", "new-window"])[-1]
    assert "mcp__claude-orchestrator" in launch


# --- v3: session-id persistence + respawn_agent + stop_agent guard ---------

def test_spawn_passes_session_id_and_persists_it_with_workdir(fake: FakeRun) -> None:
    _spawn(name="alpha", workdir="/wt/alpha")
    launch = next(c for c in fake.calls if c[:2] == ["tmux", "new-window"])[-1]
    assert "--session-id" in launch
    status = json.loads((server.AGENTS_DIR / "alpha" / "status.json").read_text())
    assert status["session_id"]
    assert status["session_id"] in launch
    assert status["workdir"] == "/wt/alpha"


def test_respawn_uses_resume_with_persisted_session_and_workdir(fake: FakeRun) -> None:
    _spawn(name="alpha", workdir="/wt/alpha")
    session_id = json.loads(
        (server.AGENTS_DIR / "alpha" / "status.json").read_text()
    )["session_id"]
    fake.calls.clear()
    server.respawn_agent("alpha")
    launch = next(c for c in fake.calls if c[:2] == ["tmux", "new-window"])[-1]
    assert f"--resume {session_id}" in launch
    assert "cd /wt/alpha" in launch


def test_respawn_kills_existing_window_first(fake: FakeRun) -> None:
    _spawn(name="alpha")
    fake.calls.clear()
    server.respawn_agent("alpha")
    calls = fake.commands()
    kill_idx = next(i for i, c in enumerate(calls) if c.startswith("tmux kill-window"))
    new_window_idx = next(i for i, c in enumerate(calls) if c.startswith("tmux new-window"))
    assert kill_idx < new_window_idx


def test_respawn_reattaches_pipe_pane_logging(fake: FakeRun) -> None:
    _spawn(name="alpha")
    fake.calls.clear()
    server.respawn_agent("alpha")
    pipe = next(c for c in fake.calls if c[:2] == ["tmux", "pipe-pane"])
    assert "agent.log" in " ".join(pipe)


def test_respawn_missing_agent_reports_error(fake: FakeRun) -> None:
    assert "No agent" in server.respawn_agent("ghost")


def test_stop_agent_refuses_with_uncommitted_changes(fake: FakeRun) -> None:
    _spawn(name="alpha", workdir="/wt/alpha")
    fake.git_status_stdout = " M some_file.py\n"
    fake.calls.clear()
    result = server.stop_agent("alpha")
    assert "uncommitted" in result.lower() or "force" in result.lower()
    assert not any(c[:2] == ["tmux", "kill-window"] for c in fake.calls)
    status = json.loads((server.AGENTS_DIR / "alpha" / "status.json").read_text())
    assert status["state"] != "stopped"


def test_stop_agent_proceeds_when_worktree_clean(fake: FakeRun) -> None:
    _spawn(name="alpha", workdir="/wt/alpha")
    fake.git_status_stdout = ""
    fake.calls.clear()
    server.stop_agent("alpha")
    assert any(c[:2] == ["tmux", "kill-window"] for c in fake.calls)
    status = json.loads((server.AGENTS_DIR / "alpha" / "status.json").read_text())
    assert status["state"] == "stopped"


def test_stop_agent_force_overrides_dirty_worktree(fake: FakeRun) -> None:
    _spawn(name="alpha", workdir="/wt/alpha")
    fake.git_status_stdout = " M some_file.py\n"
    fake.calls.clear()
    result = server.stop_agent("alpha", force=True)
    assert any(c[:2] == ["tmux", "kill-window"] for c in fake.calls)
    status = json.loads((server.AGENTS_DIR / "alpha" / "status.json").read_text())
    assert status["state"] == "stopped"
    assert "stopped" in result.lower()
