"""The hook merge rewrites files the user owns, so these tests are about what it must
never do: lose a user's own hooks, lose unrelated settings, or write twice for one install.

Fake adapters are used deliberately — this exercises the merge logic against both config
shapes in the wild (Claude/Codex nest commands inside an inner "hooks" list; Cursor puts
the command on the entry itself) without coupling to the real adapters.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tintaview.agents.base import HOOK_SENTINEL, AgentAdapter, HookBinding
from tintaview.install import hooks as H

HOOK_BIN = Path("/home/u/.tintaview/bin/tv-hook.sh")


class NestedAdapter(AgentAdapter):
    """Claude/Codex shape."""

    key = "claude"
    display_name = "Claude Code"

    def __init__(self, path: Path) -> None:
        self._path = path

    def default_home(self) -> Path:
        return self._path.parent

    @property
    def bindings(self) -> tuple[HookBinding, ...]:
        return (
            HookBinding("SessionStart", "session-start"),
            HookBinding("PreToolUse", "tool-start", matcher="*"),
        )

    def hooks_config_path(self, scope: str = "user", project_dir: Path | None = None) -> Path:
        return self._path

    def render_hooks(self, hook_command: str) -> dict:
        out: dict = {}
        for b in self.bindings:
            entry: dict = {"hooks": [{"type": "command",
                                      "command": f"{hook_command} {b.event}",
                                      "timeout": b.timeout}]}
            if b.matcher:
                entry["matcher"] = b.matcher
            out.setdefault(b.native_event, []).append(entry)
        return {"hooks": out}


class FlatAdapter(AgentAdapter):
    """Cursor shape."""

    key = "cursor"
    display_name = "Cursor"
    session_id_field = "conversation_id"

    def __init__(self, path: Path) -> None:
        self._path = path

    def default_home(self) -> Path:
        return self._path.parent

    @property
    def bindings(self) -> tuple[HookBinding, ...]:
        return (HookBinding("sessionStart", "session-start"),)

    def hooks_config_path(self, scope: str = "user", project_dir: Path | None = None) -> Path:
        return self._path

    def render_hooks(self, hook_command: str) -> dict:
        return {
            "version": 1,
            "hooks": {b.native_event: [{"command": f"{hook_command} {b.event}",
                                        "type": "command"}]
                      for b in self.bindings},
        }


@pytest.fixture
def cfg_file(tmp_path: Path) -> Path:
    return tmp_path / "settings.json"


def _apply(plan: H.HookPlan) -> dict:
    H.apply(plan)
    return json.loads(plan.path.read_text())


def test_creates_file_when_missing(cfg_file: Path):
    plan = H.plan_install(NestedAdapter(cfg_file), HOOK_BIN)
    assert plan.action == H.ACTION_CREATE
    data = _apply(plan)
    assert HOOK_SENTINEL in json.dumps(data["hooks"]["SessionStart"])
    assert data["hooks"]["PreToolUse"][0]["matcher"] == "*"


def test_preserves_user_hooks_and_unrelated_settings(cfg_file: Path):
    cfg_file.write_text(json.dumps({
        "model": "opus",
        "permissions": {"allow": ["Bash(ls:*)"]},
        "hooks": {
            "SessionStart": [{"hooks": [{"type": "command", "command": "/usr/bin/mine.sh"}]}],
            "Stop": [{"hooks": [{"type": "command", "command": "/usr/bin/other.sh"}]}],
        },
    }, indent=2))

    data = _apply(H.plan_install(NestedAdapter(cfg_file), HOOK_BIN))

    assert data["model"] == "opus"
    assert data["permissions"] == {"allow": ["Bash(ls:*)"]}
    commands = json.dumps(data["hooks"])
    assert "/usr/bin/mine.sh" in commands
    assert "/usr/bin/other.sh" in commands
    assert HOOK_SENTINEL in commands


def test_install_is_idempotent(cfg_file: Path):
    adapter = NestedAdapter(cfg_file)
    H.apply(H.plan_install(adapter, HOOK_BIN))
    second = H.plan_install(adapter, HOOK_BIN)
    assert second.action == H.ACTION_NOOP
    assert second.diff == ""
    assert H.apply(second) is None


def test_reinstall_replaces_rather_than_duplicates(cfg_file: Path):
    adapter = NestedAdapter(cfg_file)
    H.apply(H.plan_install(adapter, HOOK_BIN))
    # A moved install: same agent, different hook path.
    moved = Path("/opt/tintaview/bin/tv-hook.sh")
    data = _apply(H.plan_install(adapter, moved))
    entries = data["hooks"]["SessionStart"]
    assert len(entries) == 1, "reinstall must replace our entry, not stack a second one"
    commands = [h["command"] for entry in entries for h in entry["hooks"]]
    assert any(str(moved) in command for command in commands)


def test_uninstall_removes_only_ours(cfg_file: Path):
    cfg_file.write_text(json.dumps({
        "hooks": {"SessionStart": [
            {"hooks": [{"type": "command", "command": "/usr/bin/mine.sh"}]},
        ]},
    }, indent=2))
    adapter = NestedAdapter(cfg_file)
    H.apply(H.plan_install(adapter, HOOK_BIN))

    data = _apply(H.plan_uninstall(adapter))
    text = json.dumps(data)
    assert HOOK_SENTINEL not in text
    assert "/usr/bin/mine.sh" in text


def test_uninstall_keeps_user_hook_sharing_a_group(cfg_file: Path):
    """A hand-merged config can have our command and the user's inside one group;
    removing ours must not take theirs with it."""
    cfg_file.write_text(json.dumps({
        "hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": "/usr/bin/mine.sh"},
            {"type": "command", "command": f"{HOOK_BIN} claude session-start"},
        ]}]},
    }, indent=2))
    data = _apply(H.plan_uninstall(NestedAdapter(cfg_file)))
    inner = data["hooks"]["SessionStart"][0]["hooks"]
    assert [h["command"] for h in inner] == ["/usr/bin/mine.sh"]


def test_flat_shape_roundtrip(cfg_file: Path):
    adapter = FlatAdapter(cfg_file)
    data = _apply(H.plan_install(adapter, HOOK_BIN))
    assert data["version"] == 1
    assert HOOK_SENTINEL in data["hooks"]["sessionStart"][0]["command"]
    assert H.plan_install(adapter, HOOK_BIN).action == H.ACTION_NOOP

    cleaned = _apply(H.plan_uninstall(adapter))
    assert "hooks" not in cleaned or not cleaned["hooks"]


def test_backup_written_and_pruned(cfg_file: Path):
    cfg_file.write_text(json.dumps({"hooks": {}}))
    adapter = NestedAdapter(cfg_file)
    for i in range(H.KEEP_BACKUPS + 3):
        # Alternate the hook path so every plan is a real change.
        H.apply(H.plan_install(adapter, Path(f"/opt/v{i}/tv-hook.sh")))
    backups = list(cfg_file.parent.glob(cfg_file.name + H.BACKUP_SUFFIX + "*"))
    assert 0 < len(backups) <= H.KEEP_BACKUPS


def test_refuses_to_rewrite_invalid_json(cfg_file: Path):
    cfg_file.write_text("{ this is not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        H.plan_install(NestedAdapter(cfg_file), HOOK_BIN)


def test_status_reports_missing_partial_installed_and_stale(cfg_file: Path):
    adapter = NestedAdapter(cfg_file)
    assert H.status(adapter, HOOK_BIN) == H.STATUS_MISSING

    H.apply(H.plan_install(adapter, HOOK_BIN))
    assert H.status(adapter, HOOK_BIN) == H.STATUS_INSTALLED

    # An agent upgrade that dropped one of our events.
    data = json.loads(cfg_file.read_text())
    del data["hooks"]["PreToolUse"]
    cfg_file.write_text(json.dumps(data, indent=2))
    assert H.status(adapter, HOOK_BIN) == H.STATUS_PARTIAL

    # A moved TintaView install: entries exist but point at a path we no longer use.
    H.apply(H.plan_install(adapter, HOOK_BIN))
    assert H.status(adapter, Path("/somewhere/else/tv-hook.sh")) == H.STATUS_STALE_PATH
