"""Tests for the POSIX hook shim itself (`tintaview/hooks/tv-hook.sh`).

Every other test in this suite exercises the daemon's *handlers*; this one runs the
script agents actually invoke, against a real HTTP server on a random port, and asserts
the request line it produces. It exists because the shim is the hottest path in the
product (~5 ms, `sh` + `curl` + `sed`, no JSON parser by design) and the easiest place
for a regression to hide: nothing else here would notice if it stopped extracting a
session id, or started sending the question on every tool call.

Skipped on Windows, which runs `tv-hook.cmd` — a different script with different
limitations (see its header), and no POSIX shell to run this one with.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="tv-hook.sh needs a POSIX shell; Windows uses tv-hook.cmd"
)

HOOK = Path(__file__).resolve().parents[1] / "tintaview" / "hooks" / "tv-hook.sh"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.server.requests.append(self.path)  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def daemon():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    server.requests = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=2)


def _run(daemon, agent: str, event: str, payload: dict | str | None) -> dict[str, list[str]]:
    """Run the shim once and return the query it sent, parsed."""
    body = payload if isinstance(payload, str) else json.dumps(payload or {})
    result = subprocess.run(
        ["sh", str(HOOK), agent, event],
        input=body,
        text=True,
        capture_output=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "TINTAVIEW_URL": f"http://127.0.0.1:{daemon.server_port}",
            "TINTAVIEW_CURL": "curl",
        },
        timeout=15,
    )
    assert result.returncode == 0, result.stderr  # a hook must never fail the agent's turn
    assert daemon.requests, "the shim sent nothing"
    return parse_qs(urlparse(daemon.requests[-1]).query)


def test_the_session_id_is_extracted_and_sent(daemon):
    query = _run(daemon, "claude", "tool-start", {"session_id": "abc-123", "cwd": "/tmp"})

    assert urlparse(daemon.requests[-1]).path == "/v1/event/tool-start"
    assert query["agent"] == ["claude"] and query["sid"] == ["abc-123"]


def test_cursor_uses_its_own_session_field(daemon):
    query = _run(daemon, "cursor", "working", {"conversation_id": "conv-9"})

    assert query["sid"] == ["conv-9"]


def test_a_payload_without_a_session_id_falls_back_to_one_bucket(daemon):
    query = _run(daemon, "claude", "idle", {"cwd": "/tmp"})

    assert query["sid"] == ["default"]


def test_the_confirm_event_carries_claudes_question(daemon):
    query = _run(daemon, "claude", "confirm", {
        "session_id": "abc-123",
        "hook_event_name": "Notification",
        "message": "Claude needs your permission to use Bash",
    })

    assert query["sid"] == ["abc-123"]
    assert query["question"] == ["Claude needs your permission to use Bash"]


def test_the_confirm_event_carries_codexs_tool_name(daemon):
    """Codex's PermissionRequest payload has no sentence — `tool_name` is what there is."""
    query = _run(daemon, "codex", "confirm", {
        "session_id": "sess-1", "hook_event_name": "PermissionRequest",
        "tool_name": "shell", "tool_input": {"command": "rm -rf /tmp/x"},
    })

    assert query["question"] == ["shell"]


def test_no_other_event_ever_sends_a_question(daemon):
    """The extra read costs two processes, so it may only happen on the rare event —
    `tool-start`/`tool-end` fire on every single tool call."""
    for event in ("tool-start", "tool-end", "working", "idle", "session-start"):
        query = _run(daemon, "claude", event, {
            "session_id": "abc-123", "message": "Claude needs your permission to use Bash",
        })
        assert "question" not in query, event


def test_an_agent_with_no_question_field_sends_none(daemon):
    """Cursor has no confirm hook at all, so nothing should be scraped for it."""
    query = _run(daemon, "cursor", "confirm", {"conversation_id": "conv-9",
                                               "message": "not ours to read"})

    assert "question" not in query


def test_a_question_with_url_metacharacters_is_encoded_not_dropped(daemon):
    """Curl does the percent-encoding (`-G --data-urlencode`), so a sentence with an
    ampersand or a space cannot break the request line — which is the whole reason the
    shim does not hand-build this query."""
    query = _run(daemon, "claude", "confirm", {
        "session_id": "abc-123",
        "message": "permission to run `curl a=1&b=2` in /tmp?",
    })

    assert query["question"] == ["permission to run `curl a=1&b=2` in /tmp?"]
    assert query["sid"] == ["abc-123"], "the question leaked into another parameter"


def test_a_confirm_without_the_field_still_reports_the_session(daemon):
    query = _run(daemon, "claude", "confirm", {"session_id": "abc-123"})

    assert query["sid"] == ["abc-123"]
    assert "question" not in query


def test_a_malformed_payload_is_not_an_error(daemon):
    """Whatever an agent sends, the hook exits 0 and the daemon still hears the event."""
    query = _run(daemon, "claude", "confirm", '{"session_id": "abc-123", "message": ')

    assert query["sid"] == ["abc-123"]
