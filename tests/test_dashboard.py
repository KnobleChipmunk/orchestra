"""Tests for scripts/dashboard.sh — a one-screen blackboard overview.

The script reads $ORCHESTRA_AGENTS_DIR, prints one row per <name>/status.json
(name, state, age of `updated`, last non-empty agent.log line), skipping dirs
without a status.json. Invoked as a subprocess so no bash logic runs in-process.
"""
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dashboard.sh"


def _write_agent(agents_dir: Path, name: str, state: str, updated: datetime,
                  log_lines: list[str]) -> None:
    d = agents_dir / name
    d.mkdir(parents=True, exist_ok=True)
    status = {
        "agent": name,
        "state": state,
        "last_action": "Stop",
        "blocker_reason": None,
        "needs": None,
        "summary": None,
        "question": None,
        "updated": updated.isoformat(),
    }
    (d / "status.json").write_text(json.dumps(status, indent=2))
    (d / "agent.log").write_text("\n".join(log_lines) + "\n")


def _run_dashboard(agents_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env={"ORCHESTRA_AGENTS_DIR": str(agents_dir), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_script_exists_and_is_executable() -> None:
    assert SCRIPT.exists(), f"{SCRIPT} does not exist"
    assert SCRIPT.stat().st_mode & 0o111, f"{SCRIPT} is not executable"


def test_shows_both_agents_states_and_last_log_line(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    _write_agent(tmp_path, "alpha", "working", now - timedelta(seconds=12),
                 ["starting up", "editing files", "running tests"])
    _write_agent(tmp_path, "beta", "blocked", now - timedelta(minutes=3),
                 ["reading docs", "missing openapi.yaml"])

    result = _run_dashboard(tmp_path)

    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "alpha" in out
    assert "working" in out
    assert "running tests" in out
    assert "beta" in out
    assert "blocked" in out
    assert "missing openapi.yaml" in out


def test_skips_dirs_without_status_json(tmp_path: Path) -> None:
    _write_agent(tmp_path, "alpha", "idle", datetime.now(timezone.utc), ["hello"])
    (tmp_path / "not_an_agent").mkdir()
    (tmp_path / "not_an_agent" / "readme.txt").write_text("nothing to see here")

    result = _run_dashboard(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "alpha" in result.stdout
    assert "not_an_agent" not in result.stdout


def test_prints_header_row(tmp_path: Path) -> None:
    _write_agent(tmp_path, "alpha", "idle", datetime.now(timezone.utc), ["hello"])

    result = _run_dashboard(tmp_path)

    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) >= 2
    header = lines[0].lower()
    assert "agent" in header or "name" in header
    assert "state" in header


def test_age_is_human_readable_seconds(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    _write_agent(tmp_path, "alpha", "working", now - timedelta(seconds=5), ["hi"])

    result = _run_dashboard(tmp_path)

    row = next(line for line in result.stdout.splitlines() if "alpha" in line)
    assert "s" in row  # e.g. "5s"


def test_age_is_human_readable_minutes(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    _write_agent(tmp_path, "alpha", "working", now - timedelta(minutes=3), ["hi"])

    result = _run_dashboard(tmp_path)

    row = next(line for line in result.stdout.splitlines() if "alpha" in line)
    assert "m" in row  # e.g. "3m"


def test_no_wrapping_long_log_line_is_truncated(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    long_line = "x" * 500
    _write_agent(tmp_path, "alpha", "working", now, [long_line])

    result = _run_dashboard(tmp_path)

    row = next(line for line in result.stdout.splitlines() if "alpha" in line)
    assert len(row) <= 300  # comfortably under any real terminal width


def test_empty_agents_dir_does_not_error(tmp_path: Path) -> None:
    result = _run_dashboard(tmp_path)
    assert result.returncode == 0, result.stderr


def test_missing_agents_dir_does_not_error(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist"
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env={"ORCHESTRA_AGENTS_DIR": str(missing), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
