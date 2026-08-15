"""Every ORCHESTRA_* setting resolves in exactly one place.

Two properties are under test here:

1. Each resolver reads its environment variable at CALL time and falls back to
   a documented default. Resolvers are functions rather than module constants
   precisely so a caller can change the environment and see it take effect.
2. The shell defaults in scripts/config.sh agree with the Python defaults.
   That pair is the one place a default is necessarily spelled twice (bash
   cannot import Python, and dashboard.sh runs with no venv on PATH), so the
   agreement is asserted rather than assumed.

No test here asserts a literal default PATH value — only that env overrides
win, that unset falls back, and that the two implementations agree. Pinning
the literal would make the default impossible to change without editing tests,
which is the coupling this whole module exists to remove.
"""
import subprocess
import sys
from pathlib import Path

import pytest

from orchestra import config

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_SH = REPO_ROOT / "scripts" / "config.sh"


# --- resolvers read the environment at call time -------------------------

def test_agents_dir_uses_env_when_set(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(config.AGENTS_DIR_ENV, str(tmp_path))
    assert config.agents_dir() == tmp_path


def test_agents_dir_falls_back_to_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(config.AGENTS_DIR_ENV, raising=False)
    assert config.agents_dir() == config.default_agents_dir()


def test_agents_dir_treats_empty_string_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty ORCHESTRA_AGENTS_DIR is a misconfiguration, not a request to
    use the process's current directory as the blackboard."""
    monkeypatch.setenv(config.AGENTS_DIR_ENV, "")
    assert config.agents_dir() == config.default_agents_dir()


def test_default_agents_dir_derives_from_home(monkeypatch: pytest.MonkeyPatch,
                                              tmp_path: Path) -> None:
    """Resolved per call, so it tracks $HOME rather than baking in whatever
    home the interpreter happened to import under."""
    monkeypatch.setenv("HOME", str(tmp_path))
    resolved = config.default_agents_dir()
    assert resolved.parent == tmp_path
    assert resolved.is_absolute()


def test_tmux_session_uses_env_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(config.TMUX_SESSION_ENV, "other-session")
    assert config.tmux_session() == "other-session"


def test_tmux_session_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(config.TMUX_SESSION_ENV, raising=False)
    assert config.tmux_session() == config.DEFAULT_TMUX_SESSION


def test_agent_name_uses_env_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(config.AGENT_NAME_ENV, "alpha")
    assert config.agent_name() == "alpha"


def test_agent_name_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(config.AGENT_NAME_ENV, raising=False)
    assert config.agent_name() == config.DEFAULT_AGENT_NAME


def test_notify_target_is_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """No default: an agent with nobody to notify (the lead) is the normal
    case, so absence has to stay distinguishable from any name."""
    monkeypatch.delenv(config.NOTIFY_TARGET_ENV, raising=False)
    assert config.notify_target() is None


def test_notify_target_uses_env_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(config.NOTIFY_TARGET_ENV, "lead")
    assert config.notify_target() == "lead"


def test_nudge_cmd_uses_env_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(config.NUDGE_CMD_ENV, "/usr/local/bin/nudge")
    assert config.nudge_cmd() == "/usr/local/bin/nudge"


def test_nudge_cmd_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(config.NUDGE_CMD_ENV, raising=False)
    assert config.nudge_cmd() == config.DEFAULT_NUDGE_CMD


# --- events URL keeps its three-way contract -----------------------------

def test_events_url_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(config.EVENTS_URL_ENV, raising=False)
    assert config.events_url() == config.DEFAULT_EVENTS_URL


def test_events_url_uses_env_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(config.EVENTS_URL_ENV, "http://host:9999/events")
    assert config.events_url() == "http://host:9999/events"


def test_events_url_empty_string_disables_emission(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unlike every other setting, "" here is meaningful: it is the documented
    opt-out for observability emission, not a misconfiguration."""
    monkeypatch.setenv(config.EVENTS_URL_ENV, "")
    assert config.events_url() is None


# --- dashboard ports stay unprefixed on purpose --------------------------

def test_ports_fall_back_to_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(config.SERVER_PORT_ENV, raising=False)
    monkeypatch.delenv(config.CLIENT_PORT_ENV, raising=False)
    assert config.server_port() == config.DEFAULT_SERVER_PORT
    assert config.client_port() == config.DEFAULT_CLIENT_PORT


def test_ports_use_env_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(config.SERVER_PORT_ENV, "4100")
    monkeypatch.setenv(config.CLIENT_PORT_ENV, "5200")
    assert config.server_port() == 4100
    assert config.client_port() == 5200


def test_invalid_port_raises_instead_of_silently_defaulting(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(config.SERVER_PORT_ENV, "not-a-port")
    with pytest.raises(ValueError, match="not a valid port number"):
        config.server_port()


def test_port_env_names_are_unprefixed() -> None:
    """SERVER_PORT/CLIENT_PORT are passed straight through to the upstream
    observability repo's start-system.sh, which reads those exact names.
    Prefixing them with ORCHESTRA_ would break that hand-off."""
    assert config.SERVER_PORT_ENV == "SERVER_PORT"
    assert config.CLIENT_PORT_ENV == "CLIENT_PORT"


# --- the shell half agrees with the Python half --------------------------

def _shell_defaults(home: Path) -> dict[str, str]:
    """Source scripts/config.sh with a controlled HOME and no ORCHESTRA_*
    variables set, then report what it resolved. Runs under bash 3.2 (no
    associative arrays, no mapfile) so it works on a stock macOS too."""
    script = (
        f'set -u\n'
        f'. {CONFIG_SH}\n'
        'printf "%s\\n" "$ORCHESTRA_AGENTS_DIR"\n'
        'printf "%s\\n" "$ORCHESTRA_TMUX_SESSION"\n'
        'printf "%s\\n" "$ORCHESTRA_NUDGE_CMD"\n'
    )
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True, text=True, check=False,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    agents_dir, tmux_session, nudge_cmd = result.stdout.splitlines()
    return {"agents_dir": agents_dir, "tmux_session": tmux_session, "nudge_cmd": nudge_cmd}


def test_shell_and_python_agree_on_agents_dir(monkeypatch: pytest.MonkeyPatch,
                                              tmp_path: Path) -> None:
    """The default blackboard path is the one value necessarily written twice.
    Assert the two spellings agree instead of hoping a future edit touches
    both."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(config.AGENTS_DIR_ENV, raising=False)
    assert _shell_defaults(tmp_path)["agents_dir"] == str(config.agents_dir())


def test_shell_and_python_agree_on_tmux_session(monkeypatch: pytest.MonkeyPatch,
                                                tmp_path: Path) -> None:
    monkeypatch.delenv(config.TMUX_SESSION_ENV, raising=False)
    assert _shell_defaults(tmp_path)["tmux_session"] == config.tmux_session()


def test_shell_and_python_agree_on_nudge_cmd(monkeypatch: pytest.MonkeyPatch,
                                             tmp_path: Path) -> None:
    monkeypatch.delenv(config.NUDGE_CMD_ENV, raising=False)
    assert _shell_defaults(tmp_path)["nudge_cmd"] == config.nudge_cmd()


def test_shell_config_respects_preset_env(tmp_path: Path) -> None:
    """Sourcing config.sh must not clobber a value the operator already set."""
    script = f'set -u\n. {CONFIG_SH}\nprintf "%s\\n" "$ORCHESTRA_AGENTS_DIR"\n'
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True, text=True, check=False,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin",
             "ORCHESTRA_AGENTS_DIR": "/tmp/explicit-blackboard"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "/tmp/explicit-blackboard"


# --- consumers source their defaults from config, not their own literals --

def test_no_module_hardcodes_a_blackboard_default() -> None:
    """The regression guard for the defect this module fixes: nine files each
    spelled the same fallback path. Only config.py may name it now.

    Scans source rather than behavior because the failure mode is a NEW file
    reintroducing its own literal, which no behavioral test would catch.
    """
    offenders = []
    searched = list((REPO_ROOT / "src" / "orchestra").glob("*.py"))
    searched += list((REPO_ROOT / "scripts").glob("*.py"))
    searched += list((REPO_ROOT / "scripts").glob("*.sh"))
    for path in searched:
        if path.name in {"config.py", "config.sh"}:
            continue
        text = path.read_text()
        if "ORCHESTRA_AGENTS_DIR" in text and "config" not in text:
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == [], (
        f"these files resolve ORCHESTRA_AGENTS_DIR without going through the "
        f"shared config: {offenders}"
    )


def test_config_module_is_stdlib_only() -> None:
    """launcher.py imports config at module scope and must stay importable
    without fastmcp (docstring rule at the top of launcher.py). Importing
    config in a bare interpreter proves it pulls in nothing third-party."""
    src = REPO_ROOT / "src"
    result = subprocess.run(
        [sys.executable, "-c",
         (f"import sys; sys.path.insert(0, {str(src)!r}); import orchestra.config; "
          "assert 'fastmcp' not in sys.modules")],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
