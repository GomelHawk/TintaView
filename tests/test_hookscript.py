"""Tests for the POSIX hook shim itself (`tintaview/hooks/tv-hook.sh`).

Every other test in this suite exercises the daemon's *handlers*; this one runs the
script agents actually invoke, against a real HTTP server on a random port, and asserts
the request line it produces. It exists because the shim is the hottest path in the
product (~5 ms, `sh` + `curl` + `sed`, no JSON parser by design) and the easiest place
for a regression to hide: nothing else here would notice if it stopped extracting a
session id, or started posting the payload on every tool call.

Skipped on Windows, which runs `tv-hook.cmd` — a different script with different
limitations (see its header), and no POSIX shell to run this one with.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
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
        self._record(b"")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        self._record(self.rfile.read(length))

    def _record(self, body: bytes) -> None:
        self.server.requests.append(self.path)  # type: ignore[attr-defined]
        self.server.calls.append({  # type: ignore[attr-defined]
            "method": self.command, "path": self.path, "body": body,
            "expect": self.headers.get("Expect"),
        })
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def daemon():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    server.requests = []  # type: ignore[attr-defined]
    server.calls = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=2)


def _invoke(daemon, agent: str, event: str, payload: dict | str | None):
    """Run the shim once; return the finished process."""
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
    return result


def _run(daemon, agent: str, event: str, payload: dict | str | None) -> dict[str, list[str]]:
    """Run the shim once and return the query it sent, parsed."""
    _invoke(daemon, agent, event, payload)
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


def test_the_confirm_event_posts_the_whole_payload(daemon):
    """The daemon parses it (`core/request.py`); the shim scrapes nothing out of it."""
    payload = {
        "session_id": "abc-123", "hook_event_name": "PermissionRequest",
        "tool_name": "Bash",
        "tool_input": {"command": "rm -rf build/", "description": "Clean the build dir"},
    }
    body = json.dumps(payload)

    _invoke(daemon, "claude", "confirm", body)

    call = daemon.calls[-1]
    assert call["method"] == "POST"
    assert urlparse(call["path"]).path == "/v1/event/confirm"
    assert parse_qs(urlparse(call["path"]).query) == {"agent": ["claude"]}
    assert call["body"] == body.encode()


def test_codex_posts_its_payload_too(daemon):
    _invoke(daemon, "codex", "confirm", {
        "session_id": "sess-1", "hook_event_name": "PermissionRequest",
        "tool_name": "Bash", "tool_input": {"command": "touch /home/u/x"},
    })

    call = daemon.calls[-1]
    assert call["method"] == "POST"
    assert json.loads(call["body"])["tool_input"]["command"] == "touch /home/u/x"


def test_no_other_event_ever_posts(daemon):
    """Only the confirm event may take the payload path — `tool-start`/`tool-end` fire
    on every single tool call and keep their single-`sed` GET."""
    for event in ("tool-start", "tool-end", "working", "idle", "session-start", "session-end"):
        _run(daemon, "claude", event, {
            "session_id": "abc-123", "message": "Claude needs your permission to use Bash",
        })
        call = daemon.calls[-1]
        assert (call["method"], call["body"]) == ("GET", b""), event
        assert "question" not in parse_qs(urlparse(call["path"]).query), event


def test_the_confirm_hook_prints_nothing(daemon):
    """A PermissionRequest hook that wrote a decision to stdout would answer the user's
    prompt for them. The shim must stay silent whatever the daemon replies."""
    result = _invoke(daemon, "claude", "confirm", {"session_id": "abc-123"})

    assert result.stdout == ""


def test_a_large_payload_is_not_held_up_by_expect_100_continue(daemon):
    """curl sends `Expect: 100-continue` on a larger body and waits up to a second for a
    reply the daemon (HTTP/1.0) never gives — the whole `-m 1` budget. A `Write` of a big
    file is exactly that payload, so the shim turns the header off.

    1.5 MB because curl 8.x only sends the header above 1 MB (older builds did it above
    1 KB): measured with curl 8.5, this body without `-H "Expect:"` timed out at 1.06 s
    and never arrived, and with it took 0.01 s."""
    payload = {"session_id": "abc-123", "tool_name": "Write",
               "tool_input": {"file_path": "/tmp/big.txt", "content": "x" * 1_500_000}}

    started = time.monotonic()
    _invoke(daemon, "claude", "confirm", payload)
    elapsed = time.monotonic() - started

    call = daemon.calls[-1]
    assert call["expect"] is None
    assert len(call["body"]) > 1_500_000, "the body arrived cut off"
    assert elapsed < 0.9, f"the shim took {elapsed:.2f}s"


def test_a_payload_with_shell_metacharacters_arrives_untouched(daemon):
    """curl reads stdin itself (`--data-binary @-`), so no shell variable ever holds the
    command and there is nothing to quote, strip or escape."""
    body = json.dumps({"session_id": "abc-123", "tool_name": "Bash",
                       "tool_input": {"command": "curl 'a=1&b=2' | grep \"$HOME\" > /tmp/x"}})

    _invoke(daemon, "claude", "confirm", body)

    assert daemon.calls[-1]["body"] == body.encode()


def test_a_malformed_payload_is_not_an_error(daemon):
    """Whatever an agent sends, the hook exits 0 and the daemon still hears the event."""
    _invoke(daemon, "claude", "confirm", '{"session_id": "abc-123", "message": ')

    assert daemon.calls[-1]["body"] == b'{"session_id": "abc-123", "message": '
