"""Tests for scripts/watchdog.sh — the LEAD-nudging blackboard watchdog.

Runs the script as a subprocess (`--once`, no sleep) against a tmp
blackboard, with ORCHESTRA_NUDGE_CMD pointed at a recorder script so no test
ever touches tmux or scripts/agent_msg.py.
"""
import json
import stat
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "watchdog.sh"


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _write_status(agents_dir: Path, name: str, state: str, updated: datetime) -> None:
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
        "updated": _iso(updated),
    }
    (d / "status.json").write_text(json.dumps(status, indent=2))


def _write_lead_inbox(agents_dir: Path, blocks: list[str]) -> Path:
    d = agents_dir / "lead"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "inbox.md"
    content = "# Inbox for lead\n" + "".join(f"\n{b}\n" for b in blocks)
    p.write_text(content)
    return p


def _report_block(sender: str, ts: datetime, line: str) -> str:
    return f"[{_iso(ts)}] {sender.upper()}: {line}"


def _make_recorder(tmp_path: Path) -> tuple[Path, Path]:
    log_path = tmp_path / "nudges.log"
    recorder = tmp_path / "recorder.sh"
    recorder.write_text(
        "#!/usr/bin/env bash\n"
        f'printf \'%s\\n\' "$*" >> "{log_path}"\n'
    )
    recorder.chmod(recorder.stat().st_mode | stat.S_IEXEC)
    return recorder, log_path


def _run_once(agents_dir: Path, nudge_cmd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), "--once"],
        env={
            "ORCHESTRA_AGENTS_DIR": str(agents_dir),
            "ORCHESTRA_NUDGE_CMD": str(nudge_cmd),
            "PATH": "/usr/bin:/bin",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_script_exists_and_is_executable() -> None:
    assert SCRIPT.exists(), f"{SCRIPT} does not exist"
    assert SCRIPT.stat().st_mode & 0o111, f"{SCRIPT} is not executable"


def test_script_has_bash_syntax_ok() -> None:
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr


def test_stale_working_agent_triggers_nudge(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    _write_status(tmp_path, "d4", "working", now - timedelta(minutes=11))
    recorder, log_path = _make_recorder(tmp_path)

    result = _run_once(tmp_path, recorder)

    assert result.returncode == 0, result.stderr
    assert log_path.exists()
    log = log_path.read_text()
    assert "lead" in log
    assert "d4" in log


def test_fresh_working_agent_does_not_nudge(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    _write_status(tmp_path, "d4", "working", now - timedelta(seconds=30))
    recorder, log_path = _make_recorder(tmp_path)

    result = _run_once(tmp_path, recorder)

    assert result.returncode == 0, result.stderr
    assert not log_path.exists() or log_path.read_text() == ""


def test_stale_unprocessed_done_report_triggers_nudge(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    _write_status(tmp_path, "d4", "done", now)
    report_ts = now - timedelta(minutes=6)
    _write_lead_inbox(
        tmp_path,
        [_report_block("d4", report_ts, "DONE: scripts/watchdog.sh done on branch feature/d4-watchdog")],
    )
    recorder, log_path = _make_recorder(tmp_path)

    result = _run_once(tmp_path, recorder)

    assert result.returncode == 0, result.stderr
    assert log_path.exists()
    log = log_path.read_text()
    assert "lead" in log
    assert "d4" in log


def test_stale_unprocessed_blocked_report_triggers_nudge(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    _write_status(tmp_path, "d2", "blocked", now)
    report_ts = now - timedelta(minutes=10)
    _write_lead_inbox(
        tmp_path,
        [_report_block("d2", report_ts, "BLOCKED: need a decision on schema")],
    )
    recorder, log_path = _make_recorder(tmp_path)

    result = _run_once(tmp_path, recorder)

    assert result.returncode == 0, result.stderr
    log = log_path.read_text()
    assert "d2" in log


def test_recent_unprocessed_done_report_does_not_nudge(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    _write_status(tmp_path, "d4", "done", now)
    report_ts = now - timedelta(seconds=30)
    _write_lead_inbox(
        tmp_path,
        [_report_block("d4", report_ts, "DONE: scripts/watchdog.sh done on branch feature/d4-watchdog")],
    )
    recorder, log_path = _make_recorder(tmp_path)

    result = _run_once(tmp_path, recorder)

    assert result.returncode == 0, result.stderr
    assert not log_path.exists() or log_path.read_text() == ""


def test_done_report_already_processed_does_not_nudge(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    _write_status(tmp_path, "d4", "done", now)
    report_ts = now - timedelta(minutes=6)
    _write_lead_inbox(
        tmp_path,
        [
            _report_block("d4", report_ts, "DONE: scripts/watchdog.sh done on branch feature/d4-watchdog"),
            f"<!-- processed-through: {_iso(now)} -->",
        ],
    )
    recorder, log_path = _make_recorder(tmp_path)

    result = _run_once(tmp_path, recorder)

    assert result.returncode == 0, result.stderr
    assert not log_path.exists() or log_path.read_text() == ""


def test_idle_agent_does_not_nudge(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    _write_status(tmp_path, "d4", "idle", now - timedelta(minutes=30))
    recorder, log_path = _make_recorder(tmp_path)

    result = _run_once(tmp_path, recorder)

    assert result.returncode == 0, result.stderr
    assert not log_path.exists() or log_path.read_text() == ""


def test_empty_agents_dir_does_not_error(tmp_path: Path) -> None:
    recorder, _log_path = _make_recorder(tmp_path)
    result = _run_once(tmp_path, recorder)
    assert result.returncode == 0, result.stderr


def test_missing_agents_dir_does_not_error(tmp_path: Path) -> None:
    recorder, _log_path = _make_recorder(tmp_path)
    missing = tmp_path / "does_not_exist"
    result = _run_once(missing, recorder)
    assert result.returncode == 0, result.stderr
