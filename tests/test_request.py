"""Tests for `tintaview.core.request` — reading a posted confirm payload.

Every fixture under `tests/fixtures/hooks/` is a **real** payload, captured live from the
hook stdin of Claude Code 2.1.281 and Codex 0.147.0 (paths anonymised), not one written
from the docs. That matters here more than anywhere: the whole feature is "quote what the
agent actually sent", and the field names are exactly what a hand-written fixture would
get subtly wrong.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tintaview.core import request
from tintaview.core.request import LINE_SEP, MAX_DETAIL_CHARS, MAX_QUESTION_CHARS, parse

FIXTURES = Path(__file__).parent / "fixtures" / "hooks"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _payload(name: str) -> dict:
    return json.loads(_fixture(name))


# --------------------------------------------------------------------- captured payloads


def test_claudes_command_permission_request():
    req = parse(_fixture("claude_permission_request_bash.json"))

    assert req.sid == _payload("claude_permission_request_bash.json")["session_id"]
    assert req.tool == "Bash"
    assert req.cwd == "/home/user/TintaView"
    assert req.detail == (
        "Print a timestamp from the project venv's Python (permission-prompt capture test)"
        " — $ /home/user/TintaView/.venv/bin/python -c \"import time; "
        "print('tintaview capture test 4:', time.strftime('%T'))\""
    )


def test_claudes_multi_question_prompt_lists_every_question_and_option():
    """The case `Notification` never fires for at all — measured: a question left open
    17 s produced none — so `PermissionRequest` is the only way to see it."""
    req = parse(_fixture("claude_permission_request_ask_user_question.json"))

    assert req.tool == "AskUserQuestion"
    # (The first question's options are separators, "⏎" among them — that capture was
    # asking which one to use — so the line is compared whole rather than split on it.)
    assert req.detail == (
        "1/2 Capture test 1/2 — which single-line separator should TINTAVIEW_DETAIL use "
        "between questions? — ⏎ (Recommended) / | / ;"
        f"{LINE_SEP}2/2 Capture test 2/2 — which parts of a question should the detail "
        "include? (pick any) — Question text / Option labels / Option descriptions / "
        "Pick-any marker"
    )
    assert "The full question sentence" not in req.detail, "option descriptions are left out"


def test_claudes_notification_is_only_a_sentence():
    req = parse(_fixture("claude_notification_permission_prompt.json"))

    assert req.question == "Claude needs your permission"
    assert (req.detail, req.tool) == ("", "")
    assert req.sid == _payload("claude_notification_permission_prompt.json")["session_id"]


def test_codexs_command_permission_request_carries_its_own_sentence():
    req = parse(_fixture("codex_permission_request_bash.json"))

    assert req.tool == "Bash"
    assert req.cwd == "/mnt/c/Users/user"
    assert req.detail == (
        "Do you want to allow creating the requested empty file at "
        "/home/user/tv-capture-codex-test outside the workspace? — "
        "$ touch /home/user/tv-capture-codex-test"
    )


def test_codexs_question_tool_has_no_multi_select_flag():
    """Not bound yet (Codex fires only PreToolUse for it), but the reader already knows
    the shape: `question` per item and no `multiSelect`, so no "(pick any)"."""
    req = parse(_fixture("codex_pre_tool_use_request_user_input.json"))

    assert req.detail == (
        "1/2 Pick a colour. — Red (Recommended) / Green / Blue"
        f"{LINE_SEP}2/2 Pick one or more sizes. — S (Recommended) / M / L"
    )


@pytest.mark.parametrize("name", sorted(p.name for p in FIXTURES.glob("*.json")))
def test_every_captured_payload_yields_its_session(name):
    assert parse(_fixture(name)).sid == _payload(name)["session_id"]


# ------------------------------------------------------------------------- the one line


def test_the_detail_is_always_a_single_line():
    """`cmd` cuts `%TINTAVIEW_DETAIL%` off at the first newline, silently."""
    body = json.dumps({"session_id": "s1", "tool_name": "Bash", "tool_input": {
        "command": "set -e\ncd /srv\r\n\tmake deploy\n\n", "description": "Deploy"}})

    req = parse(body.encode())

    assert req.detail == f"Deploy — $ set -e{LINE_SEP}cd /srv{LINE_SEP}make deploy"
    assert "\n" not in req.detail and "\r" not in req.detail


def test_the_short_question_is_the_detail_capped_for_the_balloon():
    body = json.dumps({"session_id": "s1", "tool_name": "Bash",
                       "tool_input": {"command": "echo " + "x" * 5000}})

    req = parse(body.encode())

    assert len(req.detail) == MAX_DETAIL_CHARS and req.detail.endswith("…")
    assert len(req.question) == MAX_QUESTION_CHARS and req.question.endswith("…")
    assert req.question[:-1] == req.detail[: MAX_QUESTION_CHARS - 1].rstrip()


@pytest.mark.parametrize(("tool", "tool_input", "expected"), [
    ("Edit", {"file_path": "/srv/app.py", "old_string": "a", "new_string": "b"},
     "Edit /srv/app.py"),
    ("Write", {"file_path": "/srv/new.py", "content": "x" * 10_000}, "Write /srv/new.py"),
    ("NotebookEdit", {"notebook_path": "/srv/n.ipynb"}, "NotebookEdit /srv/n.ipynb"),
    ("WebFetch", {"url": "https://example.com/x", "prompt": "summarise"},
     "WebFetch https://example.com/x"),
    ("WebSearch", {"query": "tintaview"}, "WebSearch tintaview"),
    ("Bash", {"command": ["git", "push", "--force"]}, "$ git push --force"),
    ("mcp__gitlab__merge", {"id": 42, "squash": True},
     'mcp__gitlab__merge {"id":42,"squash":true}'),
    ("ExitPlanMode", {}, "ExitPlanMode"),
])
def test_each_kind_of_tool_is_described_by_what_matters_in_it(tool, tool_input, expected):
    assert request.describe(tool, tool_input) == expected


# ------------------------------------------------------------------ never raises, ever


@pytest.mark.parametrize("body", [
    b"", b"not json", b"[1, 2, 3]", b"null", b'"a string"', b"\xff\xfe\x00garbage",
    b'{"session_id": 12, "tool_name": ["Bash"], "tool_input": "rm -rf /"}',
    b'{"session_id": "s1", "tool_name": "AskUserQuestion", "tool_input": {"questions": 7}}',
])
def test_an_unreadable_payload_is_an_empty_request_not_an_error(body):
    req = parse(body)

    assert isinstance(req.detail, str) and isinstance(req.sid, str)


def test_a_payload_cut_off_at_the_size_cap_keeps_its_session_and_tool():
    """A `Write` of a large file is cut at `MAX_BODY_BYTES` by the server, leaving JSON
    that doesn't parse. Every agent writes the flat fields before `tool_input`."""
    full = json.dumps({"session_id": "s1", "cwd": "/srv", "tool_name": "Write",
                       "tool_input": {"file_path": "/srv/big", "content": "x" * 50_000}})

    req = parse(full.encode()[:1000])

    assert (req.sid, req.tool, req.cwd) == ("s1", "Write", "/srv")
    assert req.detail == "Write"


def test_a_session_id_outside_the_safe_set_is_not_used():
    """Same rule the shim's `sed` enforces on the GET path: never a different valid id,
    never anything that could break a request line."""
    req = parse(json.dumps({"session_id": "a b&c=d", "message": "hi"}).encode())

    assert req.sid == ""


def test_cursors_session_field_is_understood_too():
    req = parse(json.dumps({"conversation_id": "conv-9"}).encode())

    assert req.sid == "conv-9"
