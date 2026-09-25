"""Claude Code adapter.

Claude Code has real "waiting for you" signals, so ``confirm_detection`` defaults to
``"event"`` — no stall heuristic needed here, unlike Cursor. Two of them, both bound:

- ``PermissionRequest`` fires the moment a permission dialog opens, with the tool's
  whole ``tool_input`` — and it is the **only** one that fires for ``AskUserQuestion``
  (measured on 2.1.281: a question left open 17 s produced no ``Notification`` at all).
- ``Notification`` with matcher ``permission_prompt`` fires from a 6-second timer, with
  only "Claude needs your permission". Kept for builds that predate
  ``PermissionRequest``; on current ones the state store keeps the earlier, fuller
  request rather than letting this overwrite it.
"""

from __future__ import annotations

from pathlib import Path

from tintaview.core import events

from .base import HookBinding, NestedHooksAdapter, register


class ClaudeAdapter(NestedHooksAdapter):
    key = "claude"
    display_name = "Claude Code"
    session_id_field = "session_id"
    default_confirm_detection = "event"

    def default_home(self) -> Path:
        return Path.home() / ".claude"

    @property
    def bindings(self) -> tuple[HookBinding, ...]:
        return (
            HookBinding("SessionStart", events.SESSION_START),
            HookBinding("SessionEnd", events.SESSION_END),
            HookBinding("UserPromptSubmit", events.WORKING),
            HookBinding("PreToolUse", events.TOOL_START, matcher="*"),
            HookBinding("PostToolUse", events.TOOL_END, matcher="*"),
            HookBinding("PermissionRequest", events.CONFIRM, matcher="*"),
            HookBinding("Notification", events.CONFIRM, matcher="permission_prompt"),
            HookBinding("Notification", events.IDLE, matcher="idle_prompt"),
            HookBinding("Stop", events.IDLE),
        )

    def hooks_config_path(self, scope: str = "user", project_dir: Path | None = None) -> Path:
        if scope == "project":
            base = project_dir or Path.cwd()
            return base / ".claude" / "settings.json"
        return self.default_home() / "settings.json"

    def setup_notes(self) -> list[str]:
        return [
            "Uses Claude Code's PermissionRequest hook (plus Notification, matcher "
            "permission_prompt, for older builds) for confirm and Stop for idle.",
        ]


register(ClaudeAdapter())
