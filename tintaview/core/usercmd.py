"""The user's own shell commands: `escalation.command`, `notify.command` and
`stats.alert_command`.

All three are one config field each, holding whatever the user's setup needs (`curl`,
`ntfy`, a PowerShell one-liner), started through the shell and never waited for.
TintaView's own state reaches them in the **environment**, never interpolated into the
string, and the same variables are always set — empty when there is nothing to say — so
one script can serve all three and tell them apart by `TINTAVIEW_STATUS`.

No Qt here: what a command is handed, and how it is started, can be tested (and read)
without a tray.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from collections.abc import Mapping
from typing import Any

log = logging.getLogger(__name__)


def cmd_safe(value: str, windows: bool | None = None) -> str:
    """An agent-derived value made safe to expand inside ``"…%VAR%…"`` under ``cmd.exe``.

    The command runs through the shell, and on Windows that is ``cmd``, which
    substitutes ``%VAR%`` into the command line **before** parsing it. With the agent's
    own command in `TINTAVIEW_DETAIL`, a ``"`` in it closes the user's quoted argument
    and whatever follows (``& del …``) runs — measured through this exact `Popen` path:
    ``grep "a" x.txt" & echo INJECTED & "`` executed the ``echo``. With every ``"``
    turned into ``'``, the same value stayed one literal argument, ``& | < > ^ %``
    included. POSIX shells never re-parse an expanded ``"$VAR"``, so the value is left
    exactly as the agent wrote it there.

    This only holds while the user's command keeps the variable **inside double
    quotes**, which the README says in as many words: unquoted, ``&`` in the text is a
    command separator again.

    A value that *ends* in a backslash gets a space after it, for the step after
    ``cmd``: the program's own argument parser reads ``\\"`` as a literal quote, so
    ``"…%TINTAVIEW_CWD%"`` with a directory ending in ``\\`` left the argument open and
    let the next one's text spill into the command line.
    """
    if windows is None:
        windows = sys.platform == "win32"
    if not windows:
        return value
    value = value.replace('"', "'")
    return value + " " if value.endswith("\\") else value


def confirm_variables(waiting: list[str], question: str, message: str,
                      asking: Mapping[str, Any] | None = None) -> dict[str, str]:
    """`escalation.command`'s environment. Everything but the first two comes, in the
    end, from what an agent wrote — hence `cmd_safe`."""
    asking = asking or {}
    return {
        "TINTAVIEW_STATUS": "confirm",
        "TINTAVIEW_AGENTS": ", ".join(waiting),
        "TINTAVIEW_QUESTION": cmd_safe(question),
        "TINTAVIEW_MESSAGE": cmd_safe(message),
        "TINTAVIEW_DETAIL": cmd_safe(str(asking.get("detail") or "")),
        "TINTAVIEW_TOOL": cmd_safe(str(asking.get("request_tool") or "")),
        "TINTAVIEW_CWD": cmd_safe(str(asking.get("cwd") or "")),
    }


def notify_variables(agent_label: str, message: str, cwd: str) -> dict[str, str]:
    """`notify.command`'s environment: the message and the directory come from the agent."""
    return {
        "TINTAVIEW_STATUS": "notify",
        "TINTAVIEW_AGENTS": agent_label,
        "TINTAVIEW_MESSAGE": cmd_safe(message),
        "TINTAVIEW_CWD": cmd_safe(cwd),
        "TINTAVIEW_QUESTION": "",
        "TINTAVIEW_DETAIL": "",
        "TINTAVIEW_TOOL": "",
    }


def limit_variables(agent_label: str, label: str, pct: int, message: str) -> dict[str, str]:
    """`stats.alert_command`'s environment: the confirm variables, present and empty,
    plus `TINTAVIEW_LIMIT` (the row's label) and `TINTAVIEW_PCT` (a whole number)."""
    return {
        "TINTAVIEW_STATUS": "limit",
        "TINTAVIEW_AGENTS": agent_label,
        "TINTAVIEW_MESSAGE": cmd_safe(message),
        "TINTAVIEW_LIMIT": cmd_safe(label),
        "TINTAVIEW_PCT": str(pct),
        "TINTAVIEW_QUESTION": "",
        "TINTAVIEW_DETAIL": "",
        "TINTAVIEW_TOOL": "",
        "TINTAVIEW_CWD": "",
    }


def spawn(command: str, variables: Mapping[str, str]) -> None:
    """Start one of the user's own commands, detached, with ``variables`` added to the
    environment, and never wait for it or report on it. Never raises."""
    env = dict(os.environ)
    env.update(variables)
    try:
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            # No console flash: the tray runs windowed, and `shell=True` here means
            # cmd.exe, which would otherwise pop a window for a `curl` one-liner.
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            # Its own process group, so a command that outlives the tray isn't killed
            # by the Ctrl+C that stops a dev run.
            kwargs["start_new_session"] = True
        subprocess.Popen(command, shell=True, env=env, **kwargs)  # noqa: S602 - the user's own command, by design
    except Exception:
        # A broken command is the user's to fix, and telling them about it in a modal
        # at 2am is worse than the log line they can find when they look. Never the
        # command itself: it is where the user keeps a bot token.
        log.exception("a user command (%d characters) failed to start", len(command))
