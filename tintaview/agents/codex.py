"""Codex CLI adapter.

Codex's hook file uses the same nested shape as Claude's, but lives at
``~/.codex/hooks.json`` — never in ``config.toml``. The one exception (writing the
``codex_hooks`` / ``hooks`` feature flag into ``config.toml``) is the installer's job,
not this adapter's: this module only renders the hooks.json entries.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from tintaview.core import events

from .base import HookBinding, NestedHooksAdapter, register


class CodexAdapter(NestedHooksAdapter):
    key = "codex"
    display_name = "Codex CLI"
    session_id_field = "session_id"
    default_confirm_detection = "event"

    def default_home(self) -> Path:
        return Path.home() / ".codex"

    def version(self) -> str | None:
        # Used by the installer to decide which feature-flag spelling to write
        # (`codex_hooks` on early builds vs. `hooks` on newer ones). Best-effort: a
        # missing binary or a hung process must never break the wizard.
        try:
            result = subprocess.run(
                ["codex", "--version"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        output = (result.stdout or result.stderr or "").strip()
        return output or None

    @property
    def bindings(self) -> tuple[HookBinding, ...]:
        return (
            HookBinding("SessionStart", events.SESSION_START),
            HookBinding("SessionEnd", events.SESSION_END),
            HookBinding("UserPromptSubmit", events.WORKING),
            HookBinding("PreToolUse", events.TOOL_START),
            HookBinding("PostToolUse", events.TOOL_END),
            HookBinding("PermissionRequest", events.CONFIRM),
            HookBinding("Stop", events.IDLE),
        )

    def hooks_config_path(self, scope: str = "user", project_dir: Path | None = None) -> Path:
        if scope == "project":
            base = project_dir or Path.cwd()
            return base / ".codex" / "hooks.json"
        return self.default_home() / "hooks.json"

    def setup_notes(self) -> list[str]:
        return [
            "Codex hooks are experimental and version-gated: early builds need "
            "[features] codex_hooks = true in config.toml; newer builds have hooks on "
            "by default with hooks = false to disable.",
            "Early builds did not support hooks on Windows — the fallback there is the "
            "`notify` program, which only fires on agent-turn-complete (idle only).",
        ]


register(CodexAdapter())
