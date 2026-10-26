"""What Junie CLI has spent, read from its own session folders.

Junie CLI bills to the JetBrains account — the same credit pool as the IDE's AI Assistant
(its docs: only a bring-your-own-key run is billed to the model provider instead) — and
records the cost of every session locally, per model::

    <junie home>/sessions/<session id>/summary.json    mainAgent.modelUsage[].cost
                                                       subagents[].modelUsage[].cost
                                                       updatedAt (epoch ms)
    <junie home>/sessions/<session id>/settings.json   model.providerCode

`cost` is in the same credits the IDE's widget shows (measured 2026-10-26: PhpStorm said
9.36 of 10.00 credits left, i.e. 0.64 used this period, and the sessions recorded on this
machine summed to 0.53 — the rest being usage with no local record). `providerCode` tells
who is billed: ``jetbrains-ai`` is the account; anything else (``proxy:…``, a BYOK
provider) is not, and is left out. Only sessions from a Junie new enough to write
`summary.json` have a cost at all, so older history is simply absent.

Junie CLI can be installed on both Windows and in a WSL distro, each with its own
`sessions` folder, so the caller hands over every home to look in and the totals are
summed. This is a **local estimate**, never mixed into the official quota percentage: the
quota already includes this spend once an IDE has synced it.

Stdlib only, like the rest of `stats/`.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..core.config import Config, expand
from ._scan import FileMemo, recent_files

#: `settings.json` `model.providerCode` of a session billed to the JetBrains account.
BILLED_PROVIDER = "jetbrains-ai"

#: Per `summary.json`: (updated epoch s, credits, providerCode). Shared across polls so an
#: unchanged session costs one `stat`, which matters behind a WSL-split UNC path.
_MEMO: FileMemo[tuple[float, float, str]] = FileMemo()


def session_roots(cfg: Config) -> list[Path]:
    """Every Junie home to read, without a drive letter or username spelled out here.

    The configured `agents.junie.home` first (in a WSL split the wizard writes the
    distro's UNC path there), then the adapter's own default, which on the Windows half of
    a split is the Windows profile — the two installs a split machine can have.
    """
    from ..agents import base as agents_base

    homes: list[Path] = []
    configured = cfg.agent_config("junie").home
    if configured:
        homes.append(expand(configured))
    adapter = agents_base.get("junie")
    if adapter is not None:
        homes.append(adapter.default_home())
    out: list[Path] = []
    for home in homes:
        if home not in out:
            out.append(home)
    return out


def _usage_cost(agent: Any) -> float:
    if not isinstance(agent, dict):
        return 0.0
    total = 0.0
    for entry in agent.get("modelUsage") or []:
        if isinstance(entry, dict):
            try:
                total += float(entry.get("cost") or 0.0)
            except (TypeError, ValueError):
                continue
    return total


def _parse_summary(path: Path) -> tuple[float, float, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("not an object")
    credits = _usage_cost(data.get("mainAgent"))
    for sub in data.get("subagents") or []:
        credits += _usage_cost(sub)
    provider = ""
    try:
        settings = json.loads((path.parent / "settings.json").read_text(encoding="utf-8"))
        model = settings.get("model") if isinstance(settings, dict) else None
        provider = str(model.get("providerCode") or "") if isinstance(model, dict) else ""
    except (OSError, ValueError):
        pass  # no settings: billing unknown, which `credits_since` treats as "not ours"
    return float(data.get("updatedAt") or 0.0) / 1000.0, credits, provider


def credits_since(homes: Iterable[Path], since: float, *, now: float | None = None) -> tuple[float, int]:
    """`(credits, sessions)` billed to the JetBrains account by sessions updated since `since`.

    Never raises: an unreadable home or session (a sleeping distro, a half-written file)
    contributes nothing. Only the top level of each session folder is listed — its
    checkpoints and task folders are not walked.
    """
    now = time.time() if now is None else now
    total, count = 0.0, 0
    seen: list[Path] = []
    for home in homes:
        root = home / "sessions"
        top = os.path.normpath(str(root))
        files = recent_files(
            root, "summary.json", max(now - since, 0.0) + 3600.0, now=now,
            skip_dir=lambda dirpath, _name, top=top: os.path.normpath(dirpath) != top,
        )
        seen.extend(files)
        for path in files:
            try:
                updated, credits, provider = _MEMO.get(path, _parse_summary)
            except (OSError, ValueError):
                continue
            if provider == BILLED_PROVIDER and updated >= since:
                total += credits
                count += 1
    _MEMO.prune(seen)
    return total, count
