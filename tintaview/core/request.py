"""What a waiting agent is asking for, read out of the hook payload it posted.

The confirm event is the one hook the shim sends its **whole stdin** for
(`curl --data-binary @-`, see `hooks/tv-hook.sh`) instead of a session id pulled out by
`sed`. It fires once per prompt, not once per tool call, so it can afford that, and it
is the only event whose payload says something a person needs to read: the exact command,
the file, the URL, or every question and option of a multiple-choice prompt.

Claude Code's `PermissionRequest` and Codex's `PermissionRequest` share one shape —
``tool_name`` plus a ``tool_input`` object — so one reader serves both, keyed on the tool,
never on the agent. Claude's `Notification` carries only a ``message`` sentence, and is
read for that alone.

Everything here is display-only and never raises: a payload that isn't JSON, is cut off,
or is shaped like nothing we know still yields a session id and an empty description, so
the confirm itself — the part that drives the lights — always goes through.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from ..i18n import t

#: Most of a posted payload the daemon will read. A `Write` of a large file puts the whole
#: file in ``tool_input.content``; nothing past this point is ever shown, and reading it
#: all would let one hook occupy an HTTP worker for no reason.
MAX_BODY_BYTES = 1024 * 1024

#: Longest `detail` kept. Sized for the escalation command's most likely destination: a
#: Telegram message is capped at 4096 characters, and the command also sends the sentence
#: in `TINTAVIEW_MESSAGE` alongside it.
MAX_DETAIL_CHARS = 3500

#: Longest short question kept — the one the tray balloon quotes (Windows truncates a
#: balloon near 256 characters anyway).
MAX_QUESTION_CHARS = 200

#: Joins the lines of a multi-line command and the questions of a multiple-choice prompt.
#: `detail` is always a **single line**: `cmd` cuts `%TINTAVIEW_DETAIL%` off at the first
#: newline, which would silently lose everything after it on Windows.
LINE_SEP = " ⏎ "

#: Session ids are matched against the same safe set the shim enforces, so a posted id
#: can never be something the GET path would have refused.
_SAFE_SID = re.compile(r"[A-Za-z0-9._-]{1,128}")

#: The fields an agent's payload names its session with, in the order they are tried
#: (Cursor calls it ``conversation_id``; everyone else ``session_id``).
_SID_FIELDS = ("session_id", "conversation_id")

#: Salvage for a payload too large to parse whole: these flat fields come before
#: ``tool_input`` in every agent's payload, so they survive the cut.
_FLAT_FIELD = r'"{name}"\s*:\s*"([^"\\]*)"'

#: Tools that ask the user something rather than asking to be allowed to do something:
#: Claude's `AskUserQuestion` (via `PermissionRequest`) and Codex's `request_user_input`.
_QUESTION_TOOLS = frozenset({"AskUserQuestion", "request_user_input", "AskQuestion"})

#: Tools whose one interesting argument is a path, in the key they carry it under.
_PATH_KEYS = ("file_path", "notebook_path", "path")


@dataclass(frozen=True)
class HookRequest:
    """One confirm payload, reduced to what the state store keeps."""

    sid: str = ""
    #: Short, for the balloon (≤ `MAX_QUESTION_CHARS`).
    question: str = ""
    #: The whole request on one line (≤ `MAX_DETAIL_CHARS`): what `TINTAVIEW_DETAIL` gets.
    detail: str = ""
    tool: str = ""
    cwd: str = ""


def clean(raw: Any, limit: int) -> str:
    """Printable, one line, at most `limit` characters. Never raises.

    Newlines become `LINE_SEP` rather than spaces: a multi-line command read as one run-on
    line is hard to tell apart from a different command.
    """
    if not raw:
        return ""
    text = str(raw).replace("\r\n", "\n").replace("\r", "\n")
    text = LINE_SEP.join(" ".join(part.split()) for part in text.split("\n") if part.strip())
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def parse(body: bytes) -> HookRequest:
    """Read a posted hook payload. Never raises; an unreadable one is an empty request."""
    try:
        text = body.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - display-only; the confirm must still go through
        return HookRequest()
    try:
        payload = json.loads(text)
    except ValueError:
        return _salvage(text)
    if not isinstance(payload, dict):
        return HookRequest()
    try:
        return _from_payload(payload)
    except Exception:  # noqa: BLE001 - an unexpected shape costs the description, nothing else
        return HookRequest(sid=_sid(payload))


def _sid(payload: dict) -> str:
    for name in _SID_FIELDS:
        value = payload.get(name)
        if isinstance(value, str) and _SAFE_SID.fullmatch(value):
            return value
    return ""


def _salvage(text: str) -> HookRequest:
    """A payload cut off at `MAX_BODY_BYTES` (or otherwise broken): keep the session id
    and the tool's name, which every agent writes before the large ``tool_input``."""
    found: dict[str, str] = {}
    for name in (*_SID_FIELDS, "tool_name", "message", "cwd"):
        match = re.search(_FLAT_FIELD.format(name=name), text)
        if match:
            found[name] = match.group(1)
    return _from_payload(found) if found else HookRequest()


def _from_payload(payload: dict) -> HookRequest:
    sid = _sid(payload)
    cwd = clean(payload.get("cwd"), 500)
    tool = clean(payload.get("tool_name"), 100)
    tool_input = payload.get("tool_input")
    if not tool:
        # Claude's Notification: a sentence and nothing else.
        return HookRequest(sid=sid, question=clean(payload.get("message"), MAX_QUESTION_CHARS),
                           cwd=cwd)
    detail = clean(describe(tool, tool_input if isinstance(tool_input, dict) else {}),
                   MAX_DETAIL_CHARS)
    return HookRequest(sid=sid, question=clean(detail, MAX_QUESTION_CHARS), detail=detail,
                       tool=tool, cwd=cwd)


def describe(tool: str, tool_input: dict) -> str:
    """One readable line for a tool call, e.g. ``Create a file — $ touch x``.

    Keyed on the tool, not the agent: Claude and Codex both call their shell tool
    ``Bash`` and put the command in ``tool_input.command``. Anything unrecognised is
    shown as its name plus compact JSON rather than dropped — an MCP tool's arguments
    are still what the user is being asked to allow.
    """
    if tool in _QUESTION_TOOLS:
        return _describe_questions(tool_input)
    command = tool_input.get("command")
    if command:
        if isinstance(command, list):  # older Codex payloads: argv, not a string
            command = " ".join(str(part) for part in command)
        description = tool_input.get("description")
        return f"{description} — $ {command}" if description else f"$ {command}"
    for key in _PATH_KEYS:
        if tool_input.get(key):
            return f"{tool} {tool_input[key]}"
    for key in ("url", "query"):
        if tool_input.get(key):
            return f"{tool} {tool_input[key]}"
    if not tool_input:
        return tool
    return f"{tool} {json.dumps(tool_input, ensure_ascii=False, separators=(',', ':'))}"


def _describe_questions(tool_input: dict) -> str:
    """Every question with its options: ``1/2 Pick a colour. — Red / Green ⏎ 2/2 …``.

    Option descriptions are left out on purpose — they roughly double the length and
    the labels are what gets answered. Claude flags a multiple-answer question with
    ``multiSelect``; Codex's payload has no such flag, so its questions never say so.
    """
    questions = [q for q in tool_input.get("questions") or [] if isinstance(q, dict)]
    parts = []
    for number, question in enumerate(questions, 1):
        text = question.get("question") or question.get("prompt") or ""
        if question.get("multiSelect") or question.get("allowMultiple"):
            text = f"{text} {t('request.pick_any')}"
        labels = " / ".join(
            str(option.get("label", ""))
            for option in question.get("options") or []
            if isinstance(option, dict) and option.get("label")
        )
        line = f"{text} — {labels}" if labels else text
        parts.append(f"{number}/{len(questions)} {line}" if len(questions) > 1 else line)
    return LINE_SEP.join(parts) or str(tool_input.get("title") or "")
