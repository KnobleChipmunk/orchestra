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
