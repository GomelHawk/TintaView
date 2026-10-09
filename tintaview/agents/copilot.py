"""GitHub Copilot CLI adapter.

Copilot reads personal hooks from every ``*.json`` file in ``~/.copilot/hooks/``
(``%USERPROFILE%\\.copilot\\hooks\\`` on Windows), so TintaView writes a file of its own,
``tintaview.json``, and never merges into anything the user wrote. The shape is flat and
versioned like Cursor's — ``{"version": 1, "hooks": {"<Event>": [{...}]}}`` — but each
entry names the shell it is for (``bash`` / ``powershell``) and may carry a ``matcher``.

Event names are written in **PascalCase on purpose.** Copilot sends a different payload
depending on how the event is spelled: camelCase gets ``sessionId``/``toolName``,
PascalCase gets Claude's ``session_id``/``tool_name``/``tool_input`` — which is what
`tv-hook` scrapes and `core/request.py` parses, so no agent-specific parsing is needed.

Measured on Copilot CLI 1.0.95 (WSL) and 1.0.94 (Windows), against real sessions:

- Questions (``AskUserQuestion``) and permission prompts each fire ``Notification``, with
  ``notification_type`` ``elicitation_dialog`` / ``permission_prompt`` and a readable
  ``message`` ("Fetch URL: https://example.com"). A ``matcher`` on the notification type
  is honoured, so other notifications (``agent_idle``, ``shell_completed`` …) never
  reach the confirm path.
- ``PermissionRequest`` is **not** used: it fires before Copilot's own rules run, so it
  arrived for every auto-approved tool call and would have painted them red.
- ``SessionEnd`` fired on ``/exit`` (``reason: user_exit``) on 1.0.95. An earlier Windows
  build also sent one after every turn (``reason: complete``); the session then reopens
  on the next prompt, which costs nothing but a moment with no Copilot dot in the panel.
- The payload arrives on stdin for both shells, including a ``.cmd`` run by PowerShell.
- Copilot treats a non-zero exit from a ``preToolUse`` hook as *deny*; `tv-hook` always
  exits 0, and a timeout fails open.
"""

from __future__ import annotations

from pathlib import Path

from tintaview.core import events

from .base import AgentAdapter, HookBinding, register


class CopilotAdapter(AgentAdapter):
    key = "copilot"
    display_name = "GitHub Copilot CLI"

    def default_home(self) -> Path:
        return Path.home() / ".copilot"

    @property
    def bindings(self) -> tuple[HookBinding, ...]:
        return (
            HookBinding("SessionStart", events.SESSION_START),
            HookBinding("SessionEnd", events.SESSION_END),
            HookBinding("UserPromptSubmit", events.WORKING),
            HookBinding("PreToolUse", events.TOOL_START),
            HookBinding("PostToolUse", events.TOOL_END),
            HookBinding("Notification", events.CONFIRM, matcher="permission_prompt"),
            HookBinding("Notification", events.CONFIRM, matcher="elicitation_dialog"),
            HookBinding("Stop", events.IDLE),
        )

    def hooks_config_path(self, scope: str = "user", project_dir: Path | None = None) -> Path:
        if scope == "project":
            base = project_dir or Path.cwd()
            return base / ".github" / "hooks" / "tintaview.json"
        return self.default_home() / "hooks" / "tintaview.json"

    def render_hooks(self, hook_command: str) -> dict:
        # The shell follows the hook script, not this machine: on the Windows half of a
        # WSL split the script is the distro's `tv-hook.sh`, and that is what runs it.
        # PowerShell needs `&` to *run* a (possibly quoted) path rather than echo it.
        # `hook_command` is "<script path, quoted if it has spaces> copilot".
        windows = hook_command.rsplit(" ", 1)[0].strip('"').lower().endswith(".cmd")
        shell = "powershell" if windows else "bash"
        prefix = "& " if windows else ""
        hooks: dict[str, list[dict]] = {}
        for binding in self.bindings:
            entry: dict = {
                "type": "command",
                shell: f"{prefix}{hook_command} {binding.event}",
                "timeoutSec": binding.timeout,
            }
            if binding.matcher is not None:
                entry["matcher"] = binding.matcher
            hooks.setdefault(binding.native_event, []).append(entry)
        return {"version": 1, "hooks": hooks}

    def setup_notes(self) -> list[str]:
        return [
            "TintaView's Copilot hooks live in a file of their own "
            "(~/.copilot/hooks/tintaview.json); nothing you wrote is edited.",
            "Needs a Copilot CLI recent enough for personal hooks and the Notification "
            "event (measured on 1.0.94+); run `copilot update` if the lights don't react.",
        ]


register(CopilotAdapter())
