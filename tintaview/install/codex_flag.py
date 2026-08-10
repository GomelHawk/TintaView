"""Codex's hooks feature flag — the one place TintaView edits a user's `config.toml`.

Codex gates hooks behind a feature flag whose name and default moved between releases:
early builds that shipped the feature needed ``[features] codex_hooks = true``; later ones
have hooks on by default and use ``[features] hooks = false`` to turn them *off*. So the
right edit depends on the installed version, and on a new enough Codex there is no edit to
make at all.

`tomlkit` is used rather than a plain dump because this file is hand-written and full of
the user's own settings and comments — round-tripping it must preserve them exactly.
Same contract as :mod:`tintaview.install.hooks`: plan, show, then apply.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

#: First Codex release with the lifecycle hooks feature. Below this, hooks aren't
#: available at all and the installer falls back to the `notify` program.
MIN_HOOKS_VERSION = (0, 114)
#: From this release on, hooks are enabled by default and no flag needs writing.
FLAG_NOT_NEEDED_FROM = (0, 130)

FLAG_LEGACY = "codex_hooks"
FLAG_MODERN = "hooks"


@dataclass
class FlagPlan:
    path: Path
    action: str  # create | update | noop | unsupported
    before: str
    after: str
    reason: str

    @property
    def changes(self) -> bool:
        return self.action in ("create", "update")


def parse_version(text: str | None) -> tuple[int, ...] | None:
    """Pull a version tuple out of `codex --version` output, or None if unparseable."""
    if not text:
        return None
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    if not m:
        return None
    return tuple(int(g) for g in m.groups() if g is not None)


def plan(config_path: Path, version: str | None) -> FlagPlan:
    """Decide what (if anything) to write into the user's Codex config.toml."""
    parsed = parse_version(version)
    try:
        before = config_path.read_text(encoding="utf-8")
    except OSError:
        before = ""

    if parsed is None:
        # Unknown version: write the legacy flag, which newer Codex ignores as an unknown
        # feature key. Being wrong this way costs a stray line; being wrong the other way
        # means hooks silently never fire.
        return _write_flag(config_path, before, FLAG_LEGACY,
                           "Codex version unknown — enabling the legacy hooks flag, which "
                           "newer versions simply ignore.")

    if parsed < MIN_HOOKS_VERSION:
        return FlagPlan(config_path, "unsupported", before, before,
                        f"Codex {version} predates lifecycle hooks (needs "
                        f"{'.'.join(map(str, MIN_HOOKS_VERSION))}+). Upgrade Codex, or use "
                        "the notify fallback, which only reports idle.")

    if parsed >= FLAG_NOT_NEEDED_FROM:
        return FlagPlan(config_path, "noop", before, before,
                        f"Codex {version} has hooks enabled by default — no flag needed.")

    return _write_flag(config_path, before, FLAG_LEGACY,
                       f"Codex {version} needs the hooks feature flag enabled.")


def _write_flag(path: Path, before: str, flag: str, reason: str) -> FlagPlan:
    import tomlkit

    doc = tomlkit.parse(before) if before.strip() else tomlkit.document()
    features = doc.get("features")
    if features is None:
        features = tomlkit.table()
        doc["features"] = features

    if features.get(flag) is True:
        return FlagPlan(path, "noop", before, before, "Already enabled.")

    features[flag] = True
    after = tomlkit.dumps(doc)
    action = "create" if not before.strip() else "update"
    return FlagPlan(path, action, before, after, reason)


def diff(p: FlagPlan) -> str:
    import difflib

    if not p.changes:
        return ""
    return "".join(difflib.unified_diff(
        p.before.splitlines(keepends=True), p.after.splitlines(keepends=True),
        fromfile=f"{p.path} (current)", tofile=f"{p.path} (after)", n=3))


def apply(p: FlagPlan) -> Path | None:
    """Write the flag, backing the file up first. Returns the backup path."""
    if not p.changes:
        return None
    import datetime as _dt

    from .hooks import BACKUP_SUFFIX, _prune_backups

    p.path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if p.path.exists():
        stamp = _dt.datetime.now().strftime("%Y%m%dT%H%M%S")
        backup = p.path.with_name(p.path.name + BACKUP_SUFFIX + stamp)
        backup.write_bytes(p.path.read_bytes())
        _prune_backups(p.path)

    tmp = p.path.with_name(p.path.name + ".tintaview-tmp")
    tmp.write_text(p.after, encoding="utf-8")
    os.replace(tmp, p.path)
    log.info("codex feature flag %s: %s (backup: %s)", p.action, p.path, backup)
    return backup
