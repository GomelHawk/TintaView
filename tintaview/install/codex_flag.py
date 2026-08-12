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
        # Unknown version — the common case on a WSL-split install, where TintaView runs
        # on Windows and `codex` only exists inside the distro. Writing the *legacy* flag
        # here used to be the safe guess, on the theory that newer Codex ignores unknown
        # feature keys. It does not: it prints a deprecation warning on every single
        # invocation. Since modern Codex has hooks on by default, the only version that
        # gains anything from a flag is a narrow old range — not worth nagging everyone
        # else forever, so prefer the modern spelling and clear any legacy key.
        return _write_flag(config_path, before, FLAG_MODERN,
                           "Codex version unknown — writing the current hooks flag.")

    if parsed < MIN_HOOKS_VERSION:
        return FlagPlan(config_path, "unsupported", before, before,
                        f"Codex {version} predates lifecycle hooks (needs "
                        f"{'.'.join(map(str, MIN_HOOKS_VERSION))}+). Upgrade Codex, or use "
                        "the notify fallback, which only reports idle.")

    if parsed >= FLAG_NOT_NEEDED_FROM:
        # Hooks are on by default here, so there is nothing to enable — but a stale
        # `codex_hooks` from an older install (or an older TintaView) still makes Codex
        # print a deprecation warning on every run, so clear it out if it's there.
        return _clear_legacy_flag(
            config_path, before,
            f"Codex {version} has hooks enabled by default — no flag needed.",
        )

    return _write_flag(config_path, before, FLAG_LEGACY,
                       f"Codex {version} needs the hooks feature flag enabled.")


def _clear_legacy_flag(path: Path, before: str, noop_reason: str) -> FlagPlan:
    """Remove a deprecated ``[features] codex_hooks`` entry, if present."""
    import tomlkit

    if not before.strip():
        return FlagPlan(path, "noop", before, before, noop_reason)

    doc = tomlkit.parse(before)
    features = doc.get("features")
    if features is None or FLAG_LEGACY not in features:
        return FlagPlan(path, "noop", before, before, noop_reason)

    del features[FLAG_LEGACY]
    # Drop the table too if removing that key emptied it, rather than leaving a bare
    # `[features]` header behind in the user's hand-maintained file.
    if not len(features):
        del doc["features"]
    return FlagPlan(
        path, "update", before, tomlkit.dumps(doc),
        f"`[features] {FLAG_LEGACY}` is deprecated and Codex warns about it on every run; "
        f"hooks are enabled by default on this version, so the flag is simply removed.",
    )


def _write_flag(path: Path, before: str, flag: str, reason: str) -> FlagPlan:
    import tomlkit

    doc = tomlkit.parse(before) if before.strip() else tomlkit.document()
    features = doc.get("features")
    if features is None:
        features = tomlkit.table()
        doc["features"] = features

    # Writing the modern key must also retire the legacy one, or Codex keeps warning
    # about the deprecated entry even though the correct flag is now present.
    legacy_present = flag == FLAG_MODERN and FLAG_LEGACY in features
    if features.get(flag) is True and not legacy_present:
        return FlagPlan(path, "noop", before, before, "Already enabled.")
    if legacy_present:
        del features[FLAG_LEGACY]

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
