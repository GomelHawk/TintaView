"""Claude Code adapter.

Claude Code has a real "waiting for approval" signal (``Notification`` with matcher
``permission_prompt``), so ``confirm_detection`` defaults to ``"event"`` — no stall
heuristic needed here, unlike Cursor.
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
            "Uses Claude Code's Notification hook (matcher permission_prompt) for confirm "
            "and Stop for idle — both are already stable in released Claude Code builds.",
        ]


register(ClaudeAdapter())
