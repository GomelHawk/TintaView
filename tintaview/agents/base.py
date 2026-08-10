"""The agent-adapter contract.

One adapter per coding agent (Claude Code, Codex CLI, Cursor). An adapter knows three
things TintaView can't generalise: where the agent keeps its config, what its hook
events are called, and which stdin field carries the session id.

The hook *merge* logic deliberately lives elsewhere (``tintaview.install.hooks``) — an
adapter only renders what TintaView's entries should look like in that agent's native
format; it never writes to disk.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from pathlib import Path

#: Substring that marks a hook entry as owned by TintaView. The merge logic uses this
#: and nothing else to decide what it may remove, so it must appear in every command we
#: write and must never appear in a user's own hook.
HOOK_SENTINEL = "tv-hook"


@dataclass(frozen=True)
class HookBinding:
    """One agent-native event wired to one TintaView event."""

    native_event: str  # e.g. "PreToolUse" / "beforeShellExecution"
    event: str  # a tintaview.core.events constant
    matcher: str | None = None  # agent-specific filter, e.g. "permission_prompt"
    timeout: int = 5


class AgentAdapter(abc.ABC):
    """Everything TintaView needs to know about one agent."""

    key: str = "agent"  # config key / CLI value / ?agent= query value
    display_name: str = "Agent"
    #: stdin field carrying the session id. Cursor uses conversation_id, not session_id.
    session_id_field: str = "session_id"
    #: How this agent signals "waiting for the user": a real event, or the stall
    #: heuristic. Overridable per install via config.
    default_confirm_detection: str = "event"

    # --- discovery --------------------------------------------------------

    @abc.abstractmethod
    def default_home(self) -> Path:
        """The agent's data directory (``~/.claude``, ``~/.codex``, ``~/.cursor``)."""

    def detect(self) -> bool:
        """Is this agent installed for the current user? Used to pre-tick the wizard."""
        return self.default_home().exists()

    def version(self) -> str | None:
        """Best-effort agent version, or None. Codex uses it to pick the hook flag."""
        return None

    # --- hook wiring ------------------------------------------------------

    @property
    @abc.abstractmethod
    def bindings(self) -> tuple[HookBinding, ...]:
        """Native event -> TintaView event mapping for this agent."""

    @abc.abstractmethod
    def hooks_config_path(self, scope: str = "user", project_dir: Path | None = None) -> Path:
        """The file whose hooks TintaView edits, for ``user`` or ``project`` scope."""

    @abc.abstractmethod
    def render_hooks(self, hook_command: str) -> dict:
        """TintaView's hook entries in this agent's native structure.

        `hook_command` is the full command line (stable tv-hook path + agent key); the
        adapter appends the per-binding event argument. The result is merged into the
        user's existing config by ``tintaview.install.hooks`` — it is *not* the whole file.
        """

    def setup_notes(self) -> list[str]:
        """Caveats the wizard should show for this agent (feature flags, gaps, …)."""
        return []


_REGISTRY: dict[str, AgentAdapter] = {}


def register(adapter: AgentAdapter) -> AgentAdapter:
    _REGISTRY[adapter.key] = adapter
    return adapter


def get(key: str) -> AgentAdapter | None:
    _load_builtins()
    return _REGISTRY.get(key)


def all_agents() -> list[AgentAdapter]:
    _load_builtins()
    return [_REGISTRY[k] for k in sorted(_REGISTRY)]


def _load_builtins() -> None:
    if _REGISTRY:
        return
    from . import claude, codex, cursor  # noqa: F401  (import registers them)
