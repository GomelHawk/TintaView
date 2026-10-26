"""JetBrains Junie CLI adapter.

Junie CLI (not the Junie inside a JetBrains IDE) reads Claude-shaped hooks from
``~/.junie/config.json`` — the same nested ``{"matcher": ..., "hooks": [{"type":
"command", ...}]}`` entries as Claude and Codex, under a top-level ``hooks`` key, with
the file's other settings left alone. A project's own ``.junie/config.json`` is ignored by
Junie on purpose, so only the user file is ever written.

Measured on Junie CLI 26.9.22 (a non-interactive run with a hook that dumped its stdin) and
26.10.20 (interactive sessions, which is what has questions and dialogs):
every event carries ``session_id`` (``session-261026-165347-9jgc``), ``cwd`` and
``project_path``, so `tv-hook` and `core/request.py` need no Junie-specific parsing.
``PreToolUse`` fires for every tool, ``submit`` included; **there is no PostToolUse**, so
there is no tool-end and ``PreToolUse`` is bound to ``working`` rather than ``tool-start``
— a tool name that nothing ever clears would read "busy with Bash" while the model thinks.

A question is detected: Junie's ``ask_user`` tool reaches the hooks as ``PreToolUse`` with
``tool_name: "AskUserQuestion"`` (Claude's name) and the full ``questions`` in ``tool_input``,
22 ms before the question is drawn (measured, an interactive session). It is bound to
``confirm`` by a second ``PreToolUse`` entry matched on that name, and the general
``working`` entry is matched on everything *but* it — ``matcher`` is a full-match regex
there (``sub`` does not match ``submit``; alternation and ``^(?!x$).*`` do), and entries run
in config order, so a plain ``working`` entry would otherwise land on the same event and
undo the question. Nothing fires when the question is answered: the next hook is the
following ``PreToolUse``/``Stop``, 5-6 s later, and that is what clears it.

An approval dialog is detected by ``PermissionRequest`` — **bound only as an async hook.** Junie's
docs say a synchronous hook that exits 0 *without* a blocking decision **automatically
approves** the action and skips the dialog (Claude's does not), and measured on 26.10.20 that
is exactly what happens: with a plain capture hook a read outside the project raised no dialog
(``PermissionRequest`` fired 28 ms after ``PreToolUse``, no "Requesting human" in the log), and
with ``"async": true`` the dialog appeared and the hook still ran. `tv-hook` always exits 0 and
prints nothing, so a *synchronous* binding would approve every command the user is meant to
see — `render_hooks` must keep writing ``async`` for this event, and a test pins it. Nothing
fires when the dialog is answered; the next ``PreToolUse``/``Stop`` clears it.

Measured payload for a read outside the project: ``tool_name: "Edit"``, ``permission_reason:
"touch_file"`` and an **empty** ``tool_input`` (Junie logs "Failed to serialize action for
PermissionRequest hook" for that action type), so the message there says little more than
"Edit". Other actions (a command, an MCP tool) were not captured.

Not bound: Claude's ``"*"`` as a ``matcher``. It is a regex, so ``"*"`` is invalid; omit the matcher
  to match every tool.

``StopFailure`` (a rate limit, an auth or billing error) ends a turn without a ``Stop``, so
it is bound to ``idle``. That one was not triggered when measuring.

Junie CLI installs natively on Windows *and* inside a WSL distro, each reading its own
``~/.junie/config.json``; a WSL-split install writes both (`install.wsl.WINDOWS_SIDE_AGENTS`).

The IDE host (ACP / server) fires **no** hooks yet, per Junie's own docs — only the
interactive TUI and the batch host do — so a Junie running inside a JetBrains IDE is not
seen at all.
"""

from __future__ import annotations

from pathlib import Path

from tintaview.core import events

from .base import HookBinding, NestedHooksAdapter, register

#: Junie's question tool, as its `PreToolUse` hook names it.
QUESTION_TOOL = "AskUserQuestion"
#: Every tool *except* the question: a full-match regex (see the module docstring).
_NOT_A_QUESTION = f"^(?!{QUESTION_TOOL}$).*"


class JunieAdapter(NestedHooksAdapter):
    key = "junie"
    display_name = "Junie CLI"
    session_id_field = "session_id"
    default_confirm_detection = "event"

    def default_home(self) -> Path:
        return Path.home() / ".junie"

    @property
    def bindings(self) -> tuple[HookBinding, ...]:
        return (
            HookBinding("SessionStart", events.SESSION_START),
            HookBinding("SessionEnd", events.SESSION_END),
            HookBinding("UserPromptSubmit", events.WORKING),
            HookBinding("PreToolUse", events.WORKING, matcher=_NOT_A_QUESTION),
            HookBinding("PreToolUse", events.CONFIRM, matcher=QUESTION_TOOL),
            HookBinding("PermissionRequest", events.CONFIRM, run_async=True),
            HookBinding("Stop", events.IDLE),
            HookBinding("StopFailure", events.IDLE),
        )

    def hooks_config_path(self, scope: str = "user", project_dir: Path | None = None) -> Path:
        # Junie ignores a project's `.junie/config.json` hooks, so project scope would
        # write a file that never runs — the user file is the only one that counts.
        del scope, project_dir
        return self.default_home() / "config.json"

    def setup_notes(self) -> list[str]:
        return [
            "Junie CLI only (hooks are Early Access): a Junie inside a JetBrains IDE fires "
            "no hooks yet, so it is not detected.",
            "Questions (PreToolUse for AskUserQuestion) and approval prompts "
            "(PermissionRequest) are detected. The PermissionRequest hook is written as "
            "async: Junie approves an action on its own when a synchronous one exits 0.",
        ]


register(JunieAdapter())
