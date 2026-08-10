"""Cursor adapter.

Cursor's hook file has a different native shape from Claude/Codex: a top-level
``version`` and flat per-event lists (no nested ``{"hooks": [...]}`` wrapper, no
``matcher`` key — Cursor filters by tool name inside the command's own stdin payload,
not via a hook-config matcher). Cursor also sends ``conversation_id`` rather than
``session_id``, and has no explicit "waiting for approval" event, so ``confirm`` here
is produced by the stall detector (``tintaview.core.stalldetect``), not by a binding.
"""

from __future__ import annotations

from pathlib import Path

from tintaview.core import events

from .base import AgentAdapter, HookBinding, register


class CursorAdapter(AgentAdapter):
    key = "cursor"
    display_name = "Cursor"
    session_id_field = "conversation_id"
    default_confirm_detection = "stall"

    def default_home(self) -> Path:
        return Path.home() / ".cursor"

    @property
    def bindings(self) -> tuple[HookBinding, ...]:
        return (
            HookBinding("sessionStart", events.SESSION_START),
            HookBinding("sessionEnd", events.SESSION_END),
            HookBinding("beforeSubmitPrompt", events.WORKING),
            HookBinding("preToolUse", events.TOOL_START),
            HookBinding("postToolUse", events.TOOL_END),
            HookBinding("beforeShellExecution", events.TOOL_START),
            HookBinding("afterShellExecution", events.TOOL_END),
            HookBinding("stop", events.IDLE),
        )

    def hooks_config_path(self, scope: str = "user", project_dir: Path | None = None) -> Path:
        if scope == "project":
            base = project_dir or Path.cwd()
            return base / ".cursor" / "hooks.json"
        return self.default_home() / "hooks.json"

    def render_hooks(self, hook_command: str) -> dict:
        # Flat shape: {"version": 1, "hooks": {"<event>": [{"command": ..., ...}]}}
        # — no nested "hooks" array and no "matcher" key, unlike Claude/Codex.
        hooks: dict[str, list[dict]] = {}
        for binding in self.bindings:
            hooks.setdefault(binding.native_event, []).append(
                {
                    "command": f"{hook_command} {binding.event}",
                    "type": "command",
                    "timeout": binding.timeout,
                }
            )
        return {"version": 1, "hooks": hooks}

    def setup_notes(self) -> list[str]:
        return [
            "Cursor has no 'waiting for approval' hook event — confirm is inferred by "
            "the stall detector (no tool-end within stall_seconds of tool-start), not "
            "by a native binding.",
        ]


register(CursorAdapter())
