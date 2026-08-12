"""Tests for the agent adapters and the tv-hook.sh shim.

Two things are under test:

1. The Python adapters (``tintaview.agents.{claude,codex,cursor}``): each renders
   TintaView's hook entries in its agent's native shape, and every rendered command
   must carry ``HOOK_SENTINEL`` and end in a valid ``tintaview.core.events`` name — the
   merge logic in ``tintaview.install.hooks`` (not written yet) relies on both.
2. ``tv-hook.sh`` itself, exercised as a real subprocess against a throwaway
   ``http.server`` — this is the ~5ms-per-tool-call code path, so it's worth verifying
   against the real shell rather than only reading it.
"""

from __future__ import annotations

import http.server
import os
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from tintaview.agents import claude, codex, cursor
from tintaview.agents.base import HOOK_SENTINEL, all_agents, get
from tintaview.core import events

REPO_ROOT = Path(__file__).resolve().parent.parent
TV_HOOK_SH = REPO_ROOT / "tintaview" / "hooks" / "tv-hook.sh"
TV_HOOK_CMD = REPO_ROOT / "tintaview" / "hooks" / "tv-hook.cmd"

HOOK_COMMAND = f"{TV_HOOK_SH} claude"  # a stand-in for the installed stable path


# --------------------------------------------------------------------------- registry


def test_all_agents_registers_exactly_the_three_builtins():
    keys = {a.key for a in all_agents()}
    assert keys == {"claude", "codex", "cursor"}


def test_get_returns_none_for_unknown_key():
    assert get("nonexistent-agent") is None


# --------------------------------------------------------------------------- bindings


@pytest.mark.parametrize("adapter", [claude.ClaudeAdapter(), codex.CodexAdapter(), cursor.CursorAdapter()])
def test_every_binding_event_is_a_known_tintaview_event(adapter):
    for binding in adapter.bindings:
        assert binding.event in events.EVENTS, f"{adapter.key}: {binding.native_event} -> {binding.event!r}"


def test_cursor_session_id_field_is_conversation_id():
    assert cursor.CursorAdapter().session_id_field == "conversation_id"


def test_claude_and_codex_session_id_field_is_session_id():
    assert claude.ClaudeAdapter().session_id_field == "session_id"
    assert codex.CodexAdapter().session_id_field == "session_id"


def test_cursor_default_confirm_detection_is_stall():
    assert cursor.CursorAdapter().default_confirm_detection == "stall"


def test_claude_and_codex_default_confirm_detection_is_event():
    assert claude.ClaudeAdapter().default_confirm_detection == "event"
    assert codex.CodexAdapter().default_confirm_detection == "event"


# --------------------------------------------------------------------------- render_hooks


def _all_commands(native: dict) -> list[str]:
    """Pull every rendered command string out of a native hooks dict, regardless of
    which of the two shapes (Claude/Codex nested, Cursor flat) it uses."""
    commands = []
    for entries in native["hooks"].values():
        for entry in entries:
            if "hooks" in entry:  # Claude/Codex nested shape
                commands.extend(h["command"] for h in entry["hooks"])
            else:  # Cursor flat shape
                commands.append(entry["command"])
    return commands


@pytest.mark.parametrize("adapter", [claude.ClaudeAdapter(), codex.CodexAdapter(), cursor.CursorAdapter()])
def test_render_hooks_commands_carry_sentinel_and_end_in_valid_event(adapter):
    native = adapter.render_hooks(HOOK_COMMAND)
    commands = _all_commands(native)
    assert commands, "render_hooks produced no commands at all"
    for command in commands:
        assert HOOK_SENTINEL in command
        assert command.split()[-1] in events.EVENTS


def test_claude_render_hooks_native_shape():
    native = claude.ClaudeAdapter().render_hooks(HOOK_COMMAND)
    assert "hooks" in native and "version" not in native  # no top-level version, unlike Cursor

    pre = native["hooks"]["PreToolUse"]
    assert pre == [
        {
            "matcher": "*",
            "hooks": [{"type": "command", "command": f"{HOOK_COMMAND} tool-start", "timeout": 5}],
        }
    ]

    # Notification carries two matchers -> two separate entries in the same list.
    notif = native["hooks"]["Notification"]
    assert len(notif) == 2
    assert {e["matcher"] for e in notif} == {"permission_prompt", "idle_prompt"}
    confirm_entry = next(e for e in notif if e["matcher"] == "permission_prompt")
    assert confirm_entry["hooks"][0]["command"] == f"{HOOK_COMMAND} confirm"

    # SessionStart etc. have no matcher key at all.
    session_start = native["hooks"]["SessionStart"]
    assert session_start == [
        {"hooks": [{"type": "command", "command": f"{HOOK_COMMAND} session-start", "timeout": 5}]}
    ]


def test_codex_render_hooks_native_shape_matches_claude_and_uses_permission_request():
    native = codex.CodexAdapter().render_hooks(HOOK_COMMAND)
    assert "hooks" in native and "version" not in native

    # Same nested shape as Claude.
    pre = native["hooks"]["PreToolUse"]
    assert pre == [
        {"hooks": [{"type": "command", "command": f"{HOOK_COMMAND} tool-start", "timeout": 5}]}
    ]

    # PermissionRequest is first-class here, unlike Claude's Notification+matcher.
    confirm = native["hooks"]["PermissionRequest"]
    assert confirm == [
        {"hooks": [{"type": "command", "command": f"{HOOK_COMMAND} confirm", "timeout": 5}]}
    ]
    assert "Notification" not in native["hooks"]


def test_cursor_render_hooks_native_shape_is_flat_with_top_level_version():
    native = cursor.CursorAdapter().render_hooks(HOOK_COMMAND)
    assert native["version"] == 1

    pre = native["hooks"]["preToolUse"]
    assert pre == [{"command": f"{HOOK_COMMAND} tool-start", "type": "command", "timeout": 5}]
    # No "matcher" key anywhere, and no nested "hooks" list under each entry.
    for entries in native["hooks"].values():
        for entry in entries:
            assert "matcher" not in entry
            assert "hooks" not in entry

    # Both tool-call paths (regular tool + shell) map onto the same TintaView events.
    assert native["hooks"]["beforeShellExecution"][0]["command"].endswith(" tool-start")
    assert native["hooks"]["afterShellExecution"][0]["command"].endswith(" tool-end")

    # No confirm binding at all — Cursor has no native "waiting for approval" event.
    assert "confirm" not in native["hooks"]
    all_events = {c.rsplit(" ", 1)[-1] for c in _all_commands(native)}
    assert events.CONFIRM not in all_events


# --------------------------------------------------------------------------- hooks_config_path


def test_hooks_config_path_user_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert claude.ClaudeAdapter().hooks_config_path("user") == tmp_path / ".claude" / "settings.json"
    assert codex.CodexAdapter().hooks_config_path("user") == tmp_path / ".codex" / "hooks.json"
    assert cursor.CursorAdapter().hooks_config_path("user") == tmp_path / ".cursor" / "hooks.json"


def test_hooks_config_path_project_scope(tmp_path):
    project = tmp_path / "myproject"
    assert claude.ClaudeAdapter().hooks_config_path(
        "project", project_dir=project
    ) == project / ".claude" / "settings.json"
    assert codex.CodexAdapter().hooks_config_path(
        "project", project_dir=project
    ) == project / ".codex" / "hooks.json"
    assert cursor.CursorAdapter().hooks_config_path(
        "project", project_dir=project
    ) == project / ".cursor" / "hooks.json"


# --------------------------------------------------------------------------- setup_notes


def test_codex_setup_notes_mention_version_gating_and_windows_fallback():
    notes = " ".join(codex.CodexAdapter().setup_notes())
    assert "codex_hooks" in notes
    assert "hooks = false" in notes
    assert "Windows" in notes
    assert "notify" in notes
    assert "idle" in notes


def test_cursor_setup_notes_mention_no_confirm_event():
    notes = " ".join(cursor.CursorAdapter().setup_notes())
    assert "stall" in notes.lower() or "heuristic" in notes.lower()


# --------------------------------------------------------------------------- tv-hook.sh


class _CapturingHandler(http.server.BaseHTTPRequestHandler):
    """Records every request path it receives instead of serving anything real."""

    received: list[str] = []  # class-level: overwritten per-test via subclassing

    def do_GET(self):  # noqa: N802 (stdlib method name)
        type(self).received.append(self.path)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *_args):  # silence the default stderr logging
        pass


def _make_server() -> tuple[http.server.HTTPServer, type[_CapturingHandler], threading.Thread]:
    received: list[str] = []
    handler_cls = type("Handler", (_CapturingHandler,), {"received": received})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, handler_cls, thread


@pytest.fixture
def capturing_server() -> Iterator[tuple[str, list[str]]]:
    server, handler_cls, thread = _make_server()
    port = server.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}", handler_cls.received
    finally:
        server.shutdown()
        thread.join(timeout=2)


def _run_hook(url: str, agent: str, event: str, stdin_bytes: bytes | None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["TINTAVIEW_URL"] = url
    env["TINTAVIEW_CURL"] = "curl"
    # tv-hook.sh sources hook.env *after* the environment is read, so a real hook.env on
    # this machine overrides both variables set above rather than deferring to them.
    # Clearing TINTAVIEW_HOME alone is not enough — it makes the script fall through to
    # `$HOME/.tintaview/hook.env` instead, which is exactly the file a developer who has
    # actually installed TintaView will have. On a WSL-split install that file says
    # `TINTAVIEW_CURL=curl.exe`, and Windows curl cannot reach a test server bound inside
    # WSL, so every hook test fails for reasons that have nothing to do with the code.
    # Point HOME at an empty directory so neither lookup finds a file at all.
    env.pop("TINTAVIEW_HOME", None)
    with tempfile.TemporaryDirectory() as scratch_home:
        env["HOME"] = scratch_home
        return subprocess.run(
            ["sh", str(TV_HOOK_SH), agent, event],
            input=stdin_bytes,
            env=env,
            capture_output=True,
            timeout=10,
        )


@pytest.mark.skipif(sys.platform == "win32", reason="tv-hook.sh is POSIX sh, not for Windows")
def test_tv_hook_sh_posts_claude_session_id(capturing_server):
    url, received = capturing_server
    payload = b'{"session_id":"abc123","tool":"Bash"}'
    result = _run_hook(url, "claude", "tool-start", payload)
    assert result.returncode == 0
    assert received == ["/v1/event/tool-start?agent=claude&sid=abc123"]


@pytest.mark.skipif(sys.platform == "win32", reason="tv-hook.sh is POSIX sh, not for Windows")
def test_tv_hook_sh_uses_conversation_id_for_cursor(capturing_server):
    url, received = capturing_server
    payload = b'{"conversation_id":"cur-999"}'
    result = _run_hook(url, "cursor", "working", payload)
    assert result.returncode == 0
    assert received == ["/v1/event/working?agent=cursor&sid=cur-999"]


@pytest.mark.skipif(sys.platform == "win32", reason="tv-hook.sh is POSIX sh, not for Windows")
def test_tv_hook_sh_falls_back_to_default_sid_when_field_missing(capturing_server):
    url, received = capturing_server
    result = _run_hook(url, "claude", "idle", b'{"unrelated":"x"}')
    assert result.returncode == 0
    assert received == ["/v1/event/idle?agent=claude&sid=default"]


@pytest.mark.skipif(sys.platform == "win32", reason="tv-hook.sh is POSIX sh, not for Windows")
def test_tv_hook_sh_exits_zero_on_empty_stdin(capturing_server):
    url, received = capturing_server
    result = _run_hook(url, "claude", "idle", b"")
    assert result.returncode == 0
    assert received == ["/v1/event/idle?agent=claude&sid=default"]


@pytest.mark.skipif(sys.platform == "win32", reason="tv-hook.sh is POSIX sh, not for Windows")
def test_tv_hook_sh_exits_zero_when_daemon_unreachable():
    # Nothing listens on this port — curl must fail fast (its own -m 1 timeout) and the
    # hook must still report success, because a hook is never allowed to fail the
    # agent's turn just because the daemon happens to be down.
    result = _run_hook("http://127.0.0.1:1", "claude", "idle", b'{"session_id":"x"}')
    assert result.returncode == 0


@pytest.mark.skipif(sys.platform == "win32", reason="tv-hook.sh is POSIX sh, not for Windows")
def test_tv_hook_sh_url_encodes_unsafe_characters_out_of_sid(capturing_server):
    url, received = capturing_server
    # A pathological session id shouldn't be able to smuggle query-string syntax onto
    # the request line; the safe-character filter should strip it down to something
    # inert rather than the raw value.
    payload = b'{"session_id":"abc&evil=1 space"}'
    result = _run_hook(url, "claude", "tool-start", payload)
    assert result.returncode == 0
    # Exactly one clean request landed — no query-string injection split it into more,
    # and no unsafe character survived into the sid value.
    assert len(received) == 1
    parsed = urlsplit(received[0])
    sid = dict(pair.split("=", 1) for pair in parsed.query.split("&")).get("sid", "")
    assert sid and all(c.isalnum() or c in "._-" for c in sid)


# --------------------------------------------------------------------------- tv-hook.cmd


@pytest.mark.skipif(sys.platform != "win32", reason="tv-hook.cmd is a Windows batch script")
def test_tv_hook_cmd_posts_claude_session_id(capturing_server):
    url, received = capturing_server
    env = dict(os.environ)
    env["TINTAVIEW_URL"] = url
    env["TINTAVIEW_CURL"] = "curl.exe"
    env.pop("TINTAVIEW_HOME", None)
    result = subprocess.run(
        [str(TV_HOOK_CMD), "claude", "tool-start"],
        input=b'{"session_id":"abc123","tool":"Bash"}',
        env=env,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert received == ["/v1/event/tool-start?agent=claude&sid=abc123"]
