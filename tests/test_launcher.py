"""Tests for scripts/up.py's launcher: idempotent bring-up of the tmux
session, observability dashboard, lead agent, and watchdog/unstick loops.

subprocess.run and urlopen are always faked — no test spawns tmux, claude, or
touches the network. Every mutating tmux verb (new-session/new-window) is
captured, never executed; a dedicated test asserts no kill-* verb is ever
issued, since a live production tmux session may be attached when this runs
for real.
"""
import importlib.util
import socket
from urllib.error import HTTPError, URLError

import pytest

from orchestra import config, launcher


# --- Fakes -------------------------------------------------------------


class FakeRun:
    """Captures subprocess.run calls; simulates tmux has-session/list-windows.

    Mirrors tests/test_server.py's FakeRun, extended with `has_session_rc`
    and `window_names` so callers can script tmux's view of the world.
    """

    def __init__(self, has_session_rc: int = 0, window_names=None) -> None:
        self.calls: list[list[str]] = []
        self.has_session_rc = has_session_rc
        self.window_names: list[str] = list(window_names) if window_names else []

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        r = R()
        if cmd[:2] == ["tmux", "has-session"]:
            r.returncode = self.has_session_rc
        elif cmd[:2] == ["tmux", "list-windows"]:
            r.returncode = 0
            r.stdout = "\n".join(self.window_names)
        return r

    def commands(self) -> list[str]:
        return [" ".join(c) for c in self.calls]


class _FakeResponse:
    def close(self) -> None:
        return None


class FakeUrlopen:
    """Keyed by URL to `None` (success), an Exception instance (always
    raised), or a list of either (popped once per call; last entry sticky —
    drives 'healthy after N polls')."""

    def __init__(self) -> None:
        self.responses: dict[str, object] = {}
        self.calls: list[tuple[str, float | None]] = []

    def set(self, url: str, outcome: object) -> None:
        self.responses[url] = outcome

    def __call__(self, url, timeout=None):
        self.calls.append((url, timeout))
        outcome = self.responses.get(url)
        if isinstance(outcome, list):
            step = outcome.pop(0) if len(outcome) > 1 else outcome[0]
        else:
            step = outcome
        if isinstance(step, Exception):
            raise step
        return _FakeResponse()


class SpawnRecorder:
    def __init__(self, outcome: str = "Spawned agent 'lead' in tmux window orchestra:lead") -> None:
        self.calls: list[tuple] = []
        self.outcome = outcome

    def __call__(self, name, task_prompt, workdir, *, model):
        self.calls.append((name, task_prompt, workdir, model))
        return self.outcome


# --- Fixtures ------------------------------------------------------------


@pytest.fixture()
def fake_run(monkeypatch) -> FakeRun:
    f = FakeRun()
    monkeypatch.setattr(launcher, "RUN", f)
    return f


@pytest.fixture()
def fake_urlopen(monkeypatch) -> FakeUrlopen:
    f = FakeUrlopen()
    monkeypatch.setattr(launcher, "URLOPEN", f)
    return f


@pytest.fixture()
def fake_sleep(monkeypatch) -> list:
    calls: list[float] = []
    monkeypatch.setattr(launcher, "SLEEP", lambda d: calls.append(d))
    return calls


@pytest.fixture()
def prereqs_ok(monkeypatch, tmp_path):
    """A fully-satisfied prereq environment, so individual tests only need
    to break one thing at a time."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(launcher.shutil, "which",
                         lambda name: "/usr/bin/tmux" if name == "tmux" else None)
    monkeypatch.setenv("HOME", str(tmp_path))
    bun_dir = tmp_path / ".bun" / "bin"
    bun_dir.mkdir(parents=True)
    (bun_dir / "bun").write_text("#!/bin/sh\n")

    lead_md = tmp_path / "LEAD.md"
    lead_md.write_text("LEAD BRIEF TEXT")
    monkeypatch.setattr(launcher, "LEAD_MD", lead_md)

    obs_repo = tmp_path / "obs_repo"
    obs_repo.mkdir()
    monkeypatch.setattr(launcher, "OBS_REPO", obs_repo)

    return tmp_path


@pytest.fixture()
def lead_paths(monkeypatch, tmp_path):
    lead_wt = tmp_path / "lead-wt"
    lead_wt.mkdir(exist_ok=True)
    (lead_wt / ".mcp.json").write_text("{}")
    lead_md = tmp_path / "LEAD.md"
    lead_md.write_text("LEAD BRIEF TEXT")
    monkeypatch.setattr(launcher, "LEAD_WT", lead_wt)
    monkeypatch.setattr(launcher, "LEAD_MD", lead_md)
    return lead_wt, lead_md


def _urls():
    return (f"http://localhost:{launcher.SERVER_PORT}/events/recent",
            f"http://localhost:{launcher.CLIENT_PORT}/")


# --- check_prereqs ---------------------------------------------------------

def test_check_prereqs_all_good_returns_empty_list(prereqs_ok):
    assert launcher.check_prereqs(no_dashboard=False) == []


def test_check_prereqs_not_in_venv_or_unimportable_hints_pip_install_e(prereqs_ok, monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    errors = launcher.check_prereqs(no_dashboard=False)
    assert any("pip install -e" in e for e in errors)


def test_check_prereqs_anthropic_api_key_set_is_error(prereqs_ok, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-be-set")
    errors = launcher.check_prereqs(no_dashboard=False)
    assert any("unset ANTHROPIC_API_KEY" in e for e in errors)


def test_check_prereqs_tmux_missing_is_error(prereqs_ok, monkeypatch):
    monkeypatch.setattr(launcher.shutil, "which", lambda name: None)
    errors = launcher.check_prereqs(no_dashboard=False)
    assert any("tmux" in e for e in errors)


def test_check_prereqs_obs_repo_missing_error_contains_clone_url(prereqs_ok, monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "OBS_REPO", tmp_path / "does_not_exist")
    errors = launcher.check_prereqs(no_dashboard=False)
    assert any(
        "github.com/disler/claude-code-hooks-multi-agent-observability" in e for e in errors
    )


def test_check_prereqs_bun_missing_everywhere_error_has_install_oneliner(
    prereqs_ok, monkeypatch, tmp_path
):
    empty_home = tmp_path / "empty_home"
    empty_home.mkdir()
    monkeypatch.setenv("HOME", str(empty_home))
    errors = launcher.check_prereqs(no_dashboard=False)
    assert any("bun.sh/install" in e for e in errors)


def test_check_prereqs_bun_only_at_dot_bun_path_passes(prereqs_ok):
    # prereqs_ok already only puts bun at ~/.bun/bin/bun, not on PATH
    # (which() is patched to return None for "bun").
    assert launcher.check_prereqs(no_dashboard=False) == []


def test_check_prereqs_no_dashboard_skips_obs_and_bun_checks(prereqs_ok, monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "OBS_REPO", tmp_path / "does_not_exist")
    empty_home = tmp_path / "empty_home2"
    empty_home.mkdir()
    monkeypatch.setenv("HOME", str(empty_home))
    assert launcher.check_prereqs(no_dashboard=True) == []


def test_check_prereqs_lead_md_missing_is_error(prereqs_ok, monkeypatch, tmp_path):
    missing = tmp_path / "nope.md"
    monkeypatch.setattr(launcher, "LEAD_MD", missing)
    errors = launcher.check_prereqs(no_dashboard=False)
    assert any(str(missing) in e and "missing" in e for e in errors)


# --- http_up / dashboard_healthy -------------------------------------------

def test_dashboard_healthy_both_endpoints_up_returns_true(fake_urlopen):
    server_url, client_url = _urls()
    fake_urlopen.set(server_url, None)
    fake_urlopen.set(client_url, None)
    assert launcher.dashboard_healthy() is True


def test_http_up_socket_timeout_returns_false(fake_urlopen):
    fake_urlopen.set("http://x", socket.timeout())
    assert launcher.http_up("http://x") is False


def test_http_up_url_error_returns_false(fake_urlopen):
    fake_urlopen.set("http://x", URLError("connection refused"))
    assert launcher.http_up("http://x") is False


def test_http_up_http_error_500_returns_true(fake_urlopen):
    fake_urlopen.set("http://x", HTTPError("http://x", 500, "err", {}, None))
    assert launcher.http_up("http://x") is True


def test_http_up_calls_urlopen_with_timeout_2_0(fake_urlopen):
    fake_urlopen.set("http://x", None)
    launcher.http_up("http://x")
    assert fake_urlopen.calls[-1] == ("http://x", 2.0)


# --- start_dashboard ---------------------------------------------------------

def test_start_dashboard_already_healthy_returns_up_never_calls_start_system_sh(
    fake_run, fake_urlopen, fake_sleep
):
    server_url, client_url = _urls()
    fake_urlopen.set(server_url, None)
    fake_urlopen.set(client_url, None)

    result = launcher.start_dashboard(dry_run=False)

    assert result.state == "UP"
    assert not any("start-system.sh" in " ".join(c) for c in fake_run.calls)


def test_start_dashboard_down_no_obs_window_creates_window_started_after_3_polls_sleep_called_3x(
    fake_run, fake_urlopen, fake_sleep
):
    server_url, client_url = _urls()
    # initial check + 2 failed loop polls + 1 succeeding loop poll = 3 SLEEPs
    fake_urlopen.set(server_url, [URLError("down")] * 3 + [None])
    fake_urlopen.set(client_url, None)

    result = launcher.start_dashboard(dry_run=False, attempts=10, delay=0.01)

    assert result.state == "STARTED"
    assert len(fake_sleep) == 3
    assert any("new-window" in c and "obs" in c for c in fake_run.commands())


def test_start_dashboard_down_obs_window_exists_polls_only_no_new_window(
    fake_run, fake_urlopen, fake_sleep
):
    fake_run.window_names = ["obs"]
    server_url, client_url = _urls()
    fake_urlopen.set(server_url, [URLError("down"), None])
    fake_urlopen.set(client_url, None)

    result = launcher.start_dashboard(dry_run=False, attempts=5, delay=0.01)

    assert result.state == "STARTED"
    assert not any("new-window" in c and "obs" in c for c in fake_run.commands())


def test_start_dashboard_never_healthy_fails_after_exact_attempts_detail_names_obs_window(
    fake_run, fake_urlopen, fake_sleep
):
    server_url, _client_url = _urls()
    fake_urlopen.set(server_url, [URLError("down")])  # sticky: always fails

    result = launcher.start_dashboard(dry_run=False, attempts=3, delay=0.01)

    assert result.state == "FAILED"
    assert len(fake_sleep) == 3
    assert f"{launcher.TMUX_SESSION}:obs" in result.detail


# --- ensure_session ----------------------------------------------------------

def test_ensure_session_has_session_rc0_skips_new_session(fake_run):
    fake_run.has_session_rc = 0
    launcher.ensure_session(dry_run=False)
    assert not any(c[:2] == ["tmux", "new-session"] for c in fake_run.calls)


def test_ensure_session_has_session_nonzero_creates_new_session(fake_run):
    fake_run.has_session_rc = 1
    launcher.ensure_session(dry_run=False)
    assert ["tmux", "new-session", "-d", "-s", launcher.TMUX_SESSION, "-n", "home"] in fake_run.calls


# --- ensure_window -------------------------------------------------------

def test_ensure_window_name_present_returns_skipped_zero_new_window_calls(fake_run):
    fake_run.window_names = ["watchdog"]
    result = launcher.ensure_window("watchdog", "somecmd", dry_run=False)
    assert result.state == "SKIPPED"
    assert not any(c[:2] == ["tmux", "new-window"] for c in fake_run.calls)


def test_ensure_window_name_absent_creates_new_window_returns_started(fake_run):
    fake_run.window_names = []
    result = launcher.ensure_window("watchdog", "somecmd", dry_run=False)
    assert result.state == "STARTED"
    assert ["tmux", "new-window", "-d", "-t", launcher.TMUX_SESSION,
            "-n", "watchdog", "somecmd"] in fake_run.calls


# --- ensure_lead ---------------------------------------------------------

def test_ensure_lead_window_present_skips_and_never_calls_spawn_fn(fake_run, lead_paths):
    fake_run.window_names = ["lead"]
    spawn_fn = SpawnRecorder()
    result = launcher.ensure_lead(dry_run=False, spawn_fn=spawn_fn)
    assert result.state == "SKIPPED"
    assert spawn_fn.calls == []


def test_ensure_lead_absent_valid_worktree_calls_spawn_fn_once_with_expected_args(
    fake_run, lead_paths
):
    lead_wt, _lead_md = lead_paths
    fake_run.window_names = []
    spawn_fn = SpawnRecorder()

    result = launcher.ensure_lead(dry_run=False, spawn_fn=spawn_fn)

    assert result.state == "STARTED"
    assert len(spawn_fn.calls) == 1
    name, prompt, workdir, model = spawn_fn.calls[0]
    assert name == "lead"
    assert workdir == str(lead_wt)
    assert model == "opus"
    assert "LEAD BRIEF TEXT" in prompt
    assert "Await instructions" in prompt


def test_ensure_lead_missing_worktree_fails_with_worktree_add_instructions_no_spawn(
    fake_run, monkeypatch, tmp_path
):
    monkeypatch.setattr(launcher, "LEAD_WT", tmp_path / "no-such-worktree")
    fake_run.window_names = []
    spawn_fn = SpawnRecorder()

    result = launcher.ensure_lead(dry_run=False, spawn_fn=spawn_fn)

    assert result.state == "FAILED"
    assert "git worktree add" in result.detail
    assert spawn_fn.calls == []


def test_ensure_lead_missing_mcp_json_fails_with_worktree_add_instructions_no_spawn(
    fake_run, monkeypatch, tmp_path
):
    lead_wt = tmp_path / "lead-wt"
    lead_wt.mkdir()
    monkeypatch.setattr(launcher, "LEAD_WT", lead_wt)
    fake_run.window_names = []
    spawn_fn = SpawnRecorder()

    result = launcher.ensure_lead(dry_run=False, spawn_fn=spawn_fn)

    assert result.state == "FAILED"
    assert ".mcp.json" in result.detail
    assert spawn_fn.calls == []


def test_ensure_lead_spawn_fn_duplicate_response_fails_mentioning_respawn_agent(
    fake_run, lead_paths
):
    fake_run.window_names = []
    spawn_fn = SpawnRecorder(
        outcome="Agent 'lead' already exists in state 'idle' — not spawning a duplicate."
    )

    result = launcher.ensure_lead(dry_run=False, spawn_fn=spawn_fn)

    assert result.state == "FAILED"
    assert "respawn_agent" in result.detail


# --- main / summarize ------------------------------------------------------

def test_main_everything_running_exit_0_summary_has_all_components_ports_and_attach_hint(
    fake_run, fake_urlopen, fake_sleep, prereqs_ok, lead_paths, capsys
):
    fake_run.window_names = ["lead", "watchdog", "unstick"]
    server_url, client_url = _urls()
    fake_urlopen.set(server_url, None)
    fake_urlopen.set(client_url, None)

    exit_code = launcher.main([], spawn_fn=SpawnRecorder())
    out = capsys.readouterr().out

    assert exit_code == 0
    for token in ("dashboard", "lead", "watchdog", "unstick",
                  str(launcher.SERVER_PORT), str(launcher.CLIENT_PORT),
                  f"tmux attach -t {launcher.TMUX_SESSION}"):
        assert token in out


def test_main_any_failed_component_exits_1(
    fake_run, fake_urlopen, fake_sleep, prereqs_ok, monkeypatch, tmp_path
):
    fake_run.window_names = ["watchdog", "unstick"]  # lead window absent
    monkeypatch.setattr(launcher, "LEAD_WT", tmp_path / "missing-lead-wt")
    server_url, client_url = _urls()
    fake_urlopen.set(server_url, None)
    fake_urlopen.set(client_url, None)

    assert launcher.main([]) == 1


def test_main_prereq_failure_exits_1_and_nothing_started(fake_run, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-be-set")
    assert launcher.main([]) == 1
    assert fake_run.calls == []


def test_main_no_dashboard_flag_shows_skipped_row_and_never_calls_urlopen(
    fake_run, fake_urlopen, prereqs_ok, lead_paths, capsys
):
    fake_run.window_names = ["lead", "watchdog", "unstick"]

    exit_code = launcher.main(["--no-dashboard"])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert fake_urlopen.calls == []
    assert "SKIPPED" in out and "disabled" in out


def test_main_dry_run_exits_0_zero_mutating_calls_and_prints_would_start(
    fake_run, fake_urlopen, fake_sleep, prereqs_ok, lead_paths, capsys
):
    fake_run.window_names = []
    server_url, client_url = _urls()
    fake_urlopen.set(server_url, URLError("down"))
    fake_urlopen.set(client_url, URLError("down"))
    spawn_fn = SpawnRecorder()

    exit_code = launcher.main(["--dry-run"], spawn_fn=spawn_fn)
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "WOULD START" in out
    assert spawn_fn.calls == []
    mutating = {"new-window", "new-session"}
    assert not any(set(c) & mutating for c in fake_run.calls)


def test_main_dashboard_start_precedes_lead_watchdog_unstick_in_call_order(
    fake_run, fake_urlopen, fake_sleep, prereqs_ok, lead_paths
):
    fake_run.window_names = []
    server_url, client_url = _urls()
    fake_urlopen.set(server_url, None)
    fake_urlopen.set(client_url, None)

    def spawn_fn(name, task_prompt, workdir, *, model):
        fake_run.calls.append(["SPAWN", "lead"])
        return "Spawned agent 'lead'"

    launcher.main([], spawn_fn=spawn_fn)

    commands = fake_run.commands()
    idx_dashboard = next(i for i, c in enumerate(commands) if c.startswith("tmux has-session"))
    idx_spawn = next(i for i, c in enumerate(commands) if c == "SPAWN lead")
    idx_watchdog = next(i for i, c in enumerate(commands) if "new-window" in c and "watchdog" in c)
    idx_unstick = next(i for i, c in enumerate(commands) if "new-window" in c and "unstick" in c)

    assert idx_dashboard < idx_spawn < idx_watchdog < idx_unstick


# --- safety invariant ------------------------------------------------------

def test_main_never_issues_kill_window_kill_session_or_kill_server(
    fake_run, fake_urlopen, fake_sleep, prereqs_ok, lead_paths
):
    fake_run.window_names = []
    server_url, client_url = _urls()
    fake_urlopen.set(server_url, None)
    fake_urlopen.set(client_url, None)

    launcher.main([], spawn_fn=SpawnRecorder())

    forbidden = ("kill-window", "kill-session", "kill-server")
    assert not any(any(f in part for part in c) for c in fake_run.calls for f in forbidden)


def test_summarize_reports_the_resolved_blackboard_path(
    fake_run, fake_urlopen, fake_sleep, prereqs_ok, capsys
):
    """The blackboard is created on demand, so an operator running up.py must
    be told which directory is about to be written to — especially when it
    came from the default rather than from something they set."""
    fake_run.window_names = ["lead", "watchdog", "unstick"]
    server_url, client_url = _urls()
    fake_urlopen.set(server_url, None)
    fake_urlopen.set(client_url, None)

    launcher.main([], spawn_fn=SpawnRecorder())
    out = capsys.readouterr().out

    assert str(launcher.AGENTS_DIR) in out


def test_summarize_says_whether_the_blackboard_path_was_configured(
    fake_run, fake_urlopen, fake_sleep, prereqs_ok, capsys, monkeypatch
):
    fake_run.window_names = ["lead", "watchdog", "unstick"]
    server_url, client_url = _urls()
    fake_urlopen.set(server_url, None)
    fake_urlopen.set(client_url, None)

    monkeypatch.delenv(config.AGENTS_DIR_ENV, raising=False)
    launcher.main([], spawn_fn=SpawnRecorder())
    assert "default" in capsys.readouterr().out

    monkeypatch.setenv(config.AGENTS_DIR_ENV, "/somewhere/explicit")
    launcher.main([], spawn_fn=SpawnRecorder())
    assert config.AGENTS_DIR_ENV in capsys.readouterr().out
