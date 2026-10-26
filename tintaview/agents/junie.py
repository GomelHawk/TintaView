"""JetBrains Junie CLI adapter.

Junie CLI (not the Junie inside a JetBrains IDE) reads Claude-shaped hooks from
``~/.junie/config.json`` — the same nested ``{"matcher": ..., "hooks": [{"type":
"command", ...}]}`` entries as Claude and Codex, under a top-level ``hooks`` key, with
the file's other settings left alone. A project's own ``.junie/config.json`` is ignored by
Junie on purpose, so only the user file is ever written.

Measured on Junie CLI 26.9.22 with a hook that dumped its stdin (a non-interactive run):
every event carries ``session_id`` (``session-261026-165347-9jgc``), ``cwd`` and
``project_path``, so `tv-hook` and `core/request.py` need no Junie-specific parsing.
``PreToolUse`` fires for every tool, ``submit`` included; **there is no PostToolUse**, so
there is no tool-end and ``PreToolUse`` is bound to ``working`` rather than ``tool-start``
— a tool name that nothing ever clears would read "busy with Bash" while the model thinks.

Two things are deliberately **not** bound:

- ``PermissionRequest``. Junie's docs say a hook that exits 0 *without* a blocking decision
  **automatically approves** the action and skips the dialog (Claude's does not). `tv-hook`
  always exits 0 and prints nothing, so binding it would approve every sensitive command
  the user is meant to see. Until there is a way to observe the dialog without answering
  it, approvals and questions are not detected and the lights stay on ``working``.
- A ``matcher`` on ``PreToolUse``. It is a regex, so Claude's ``"*"`` is invalid; omitting
  it matches every tool.

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
            HookBinding("PreToolUse", events.WORKING),
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
            "Approval and question prompts are not detected: Junie auto-approves an action "
            "when its PermissionRequest hook exits 0, so TintaView does not bind that "
            "event. The lights stay on working while Junie waits for you.",
        ]


register(JunieAdapter())
