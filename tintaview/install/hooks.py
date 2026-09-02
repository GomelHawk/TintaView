"""Installing TintaView's hooks into an agent's own configuration file.

This module edits files the user owns and may have hand-written — `~/.claude/settings.json`
in particular usually already contains their own hooks. Three rules make that safe:

1. **We only ever touch entries we wrote.** Ownership is decided by one thing: the command
   contains :data:`~tintaview.agents.base.HOOK_SENTINEL`. Anything else in the file is
   copied through untouched, including keys we know nothing about.
2. **Nothing is written without the user seeing it.** Planning and applying are separate:
   :func:`plan_install` produces a unified diff, the caller shows it and asks, and only
   then does :func:`apply` write.
3. **Every write is backed up and atomic.** A timestamped backup lands next to the file
   first, and the new content is written to a temp file and renamed into place, so a crash
   mid-write can never truncate an agent's config.

Re-running an install is a no-op: the plan comes back with ``action == "noop"`` and an
empty diff, which is also what makes the tray's drift check cheap.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import difflib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from ..agents.base import HOOK_SENTINEL, AgentAdapter

log = logging.getLogger(__name__)

BACKUP_SUFFIX = ".tintaview-backup-"
KEEP_BACKUPS = 5

ACTION_CREATE = "create"
ACTION_UPDATE = "update"
ACTION_REMOVE = "remove"
ACTION_NOOP = "noop"

STATUS_INSTALLED = "installed"
STATUS_MISSING = "missing"
STATUS_PARTIAL = "partial"
STATUS_STALE_PATH = "stale-path"
#: The file is there but we could not read or parse it — a permission problem, a lock,
#: a sleeping WSL distro behind a UNC path, or corrupt JSON. Deliberately *not*
#: `missing`: "missing" sends the user to `hooks install`, which would then plan a
#: CREATE and overwrite a settings file full of their own hooks with only ours.
STATUS_UNREADABLE = "unreadable"


@dataclass
class HookPlan:
    """A proposed edit to one agent's config file. Nothing has been written yet."""

    agent_key: str
    path: Path
    action: str
    before: str
    after: str
    notes: list[str] = field(default_factory=list)

    @property
    def diff(self) -> str:
        if self.action == ACTION_NOOP:
            return ""
        return "".join(
            difflib.unified_diff(
                self.before.splitlines(keepends=True),
                self.after.splitlines(keepends=True),
                fromfile=f"{self.path} (current)",
                tofile=f"{self.path} (after)",
                n=3,
            )
        )

    @property
    def changes(self) -> bool:
        return self.action != ACTION_NOOP


# --------------------------------------------------------------------------- helpers


def hook_command(adapter: AgentAdapter, hook_bin: Path) -> str:
    """The stable command prefix baked into the agent's config.

    Quoted only when it needs to be, because these strings end up in a file the user
    reads: unnecessary quoting makes a hand-check harder than it should be.
    """
    text = str(hook_bin)
    if " " in text:
        text = f'"{text}"'
    return f"{text} {adapter.key}"


def _read_json(path: Path) -> tuple[dict, str]:
    """Return (parsed, original text). A missing file is an empty config, not an error.

    Only *not found* means empty. Every other `OSError` — no read permission, a file the
    agent has locked, a UNC path into a WSL distro that is asleep — propagates, because
    treating it as "empty" made `plan_install` choose CREATE and replace the user's real
    settings.json with a file containing nothing but TintaView's hooks.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, ""
    if not text.strip():
        return {}, text
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        # Refuse to guess: rewriting a file we couldn't parse would destroy it.
        raise ValueError(f"{path} is not valid JSON ({exc}); fix or move it, then retry") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return data, text


def _is_ours(entry: object) -> bool:
    """Does this hook entry belong to TintaView?

    Serialising the whole entry catches both config shapes — Claude/Codex nest the
    command inside an inner ``hooks`` list, Cursor puts it on the entry itself.
    """
    try:
        return HOOK_SENTINEL in json.dumps(entry)
    except (TypeError, ValueError):
        return False


def _strip_ours(hooks: dict) -> dict:
    """Remove TintaView's entries from an event map, leaving everything else alone.

    Handles the nested shape carefully: a user who merged our snippet by hand may have
    ended up with one matcher group holding both their hook and ours, so inner items are
    filtered individually and a group is only dropped once *we* emptied it.
    """
    cleaned: dict = {}
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            cleaned[event] = entries
            continue
        kept = []
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("hooks"), list):
                inner = [h for h in entry["hooks"] if not _is_ours(h)]
                if not inner:
                    continue  # the whole group was ours
                if len(inner) != len(entry["hooks"]):
                    entry = {**entry, "hooks": inner}
                kept.append(entry)
            elif not _is_ours(entry):
                kept.append(entry)
        if kept:
            cleaned[event] = kept
    return cleaned


def _event_map(rendered: dict) -> dict:
    """Accept either ``{"hooks": {...}}`` or a bare event map from an adapter."""
    inner = rendered.get("hooks")
    return inner if isinstance(inner, dict) else rendered


def _merge(existing: dict, rendered: dict) -> dict:
    """Existing config + our entries, with ours replacing any previous ours."""
    out = dict(existing)  # shallow copy: untouched top-level keys pass straight through

    hooks = out.get("hooks")
    hooks = dict(hooks) if isinstance(hooks, dict) else {}
    hooks = _strip_ours(hooks)

    for event, entries in _event_map(rendered).items():
        current = list(hooks.get(event, []))
        current.extend(entries)
        hooks[event] = current

    out["hooks"] = hooks

    # Cursor's file is versioned and rejects one without it; harmless elsewhere only if
    # the adapter asked for it, so copy any non-"hooks" scalars the adapter rendered.
    for key, value in rendered.items():
        if key != "hooks" and not isinstance(value, dict):
            out.setdefault(key, value)
    return out


def _dump(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------- planning


def plan_install(
    adapter: AgentAdapter,
    hook_bin: Path,
    scope: str = "user",
    project_dir: Path | None = None,
) -> HookPlan:
    path = adapter.hooks_config_path(scope, project_dir)
    existing, before = _read_json(path)
    merged = _merge(existing, adapter.render_hooks(hook_command(adapter, hook_bin)))
    after = _dump(merged)

    if not before:
        action = ACTION_CREATE
    elif after == before:
        action = ACTION_NOOP
    else:
        action = ACTION_UPDATE

    notes = list(adapter.setup_notes())
    if before and action == ACTION_UPDATE and _dump(existing) != before:
        # We reformat as we rewrite; say so up front rather than letting the diff
        # surprise someone who hand-formatted their settings file.
        notes.append(
            "This file will be reformatted to standard 2-space JSON. "
            f"A backup is kept next to it as *{BACKUP_SUFFIX}<timestamp>."
        )
    return HookPlan(adapter.key, path, action, before, after, notes)


def plan_uninstall(
    adapter: AgentAdapter, scope: str = "user", project_dir: Path | None = None
) -> HookPlan:
    path = adapter.hooks_config_path(scope, project_dir)
    existing, before = _read_json(path)
    if not before:
        return HookPlan(adapter.key, path, ACTION_NOOP, "", "", ["Nothing installed."])

    out = dict(existing)
    hooks = out.get("hooks")
    if isinstance(hooks, dict):
        stripped = _strip_ours(dict(hooks))
        if stripped:
            out["hooks"] = stripped
        else:
            out.pop("hooks", None)

    after = _dump(out)
    action = ACTION_NOOP if after == before else ACTION_REMOVE
    return HookPlan(adapter.key, path, action, before, after)


def status(
    adapter: AgentAdapter,
    hook_bin: Path,
    scope: str = "user",
    project_dir: Path | None = None,
) -> str:
    """What state this agent's hooks are in — the tray's drift check.

    ``stale-path`` matters more than it looks: it means our entries point at a hook
    binary that no longer exists (a moved or reinstalled TintaView), which fails silently
    at runtime and would otherwise just look like "the lights stopped working".

    A file that exists but cannot be read or parsed is ``unreadable``, never ``missing``:
    the drift check runs on a timer, and "missing" is the state that offers to rewrite
    the file.
    """
    path = adapter.hooks_config_path(scope, project_dir)
    try:
        existing, before = _read_json(path)
    except (OSError, ValueError):
        return STATUS_UNREADABLE
    if not before:
        return STATUS_MISSING

    hooks = existing.get("hooks") if isinstance(existing.get("hooks"), dict) else {}
    ours = [e for entries in hooks.values() if isinstance(entries, list)
            for e in entries if _is_ours(e)]
    if not ours:
        return STATUS_MISSING

    expected = _event_map(adapter.render_hooks(hook_command(adapter, hook_bin)))
    installed_events = {ev for ev, entries in hooks.items()
                        if isinstance(entries, list) and any(_is_ours(e) for e in entries)}
    if not set(expected) <= installed_events:
        return STATUS_PARTIAL
    # Compare escaped-to-escaped: a bare str(hook_bin) substring check breaks whenever
    # the path contains a backslash (any Windows path), since json.dumps() below doubles
    # each one — a raw path never appears verbatim inside its own escaped JSON rendering.
    needle = json.dumps(str(hook_bin))[1:-1]
    if not any(needle in json.dumps(e) for e in ours):
        return STATUS_STALE_PATH
    return STATUS_INSTALLED


# --------------------------------------------------------------------------- applying


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = _dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    dest = path.with_name(path.name + BACKUP_SUFFIX + stamp)
    dest.write_bytes(path.read_bytes())
    _prune_backups(path)
    return dest


def _prune_backups(path: Path) -> None:
    pattern = path.name + BACKUP_SUFFIX + "*"
    backups = sorted(path.parent.glob(pattern))
    for old in backups[:-KEEP_BACKUPS]:
        with contextlib.suppress(OSError):
            old.unlink()


def apply(plan: HookPlan) -> Path | None:
    """Write a planned change. Returns the backup path, or None when nothing was written.

    The caller is expected to have shown ``plan.diff`` and got an explicit yes first.
    """
    if not plan.changes:
        return None
    plan.path.parent.mkdir(parents=True, exist_ok=True)
    backup = _backup(plan.path)
    tmp = plan.path.with_name(plan.path.name + ".tintaview-tmp")
    tmp.write_text(plan.after, encoding="utf-8")
    os.replace(tmp, plan.path)
    log.info("hooks %s: %s (backup: %s)", plan.action, plan.path, backup)
    return backup
