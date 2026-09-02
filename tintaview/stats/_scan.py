"""Shared file walk + per-file memo for the transcript-reading usage providers.

The Claude and Codex providers both rebuild their numbers from JSONL transcripts on
every 5-minute poll. AGENTS.md's rule for those scans — *scan only files modified in the
last 7 days and cache by mtime* — exists because in a WSL-split install the transcripts
sit behind a ``\\\\wsl.localhost\\...`` UNC path, where every ``stat`` and every read is a
round trip. Both halves of that rule live here so the providers can't drift apart:

- :func:`recent_files` walks a tree once, lets the caller prune whole subtrees before
  descending (Codex's ``YYYY/MM/DD`` session directories), and keeps only files whose
  mtime is inside the window. A file untouched for a week cannot contain a line from
  the last week, so the per-line timestamp filter the providers apply afterwards is
  unaffected.
- :class:`FileMemo` caches whatever a provider parsed out of a file, keyed on
  ``(mtime_ns, size)``. An unchanged file therefore costs one ``stat`` per poll instead
  of a full read; a file Claude Code or Codex is still appending to is re-parsed as soon
  as it grows.

Stdlib only — this is imported by the headless core's providers.
"""

from __future__ import annotations

import fnmatch
import os
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path

#: The window both providers show, and therefore the widest file mtime worth reading.
WEEK_S = 7 * 86400


def recent_files(
    root: Path,
    pattern: str,
    max_age_s: float = WEEK_S,
    *,
    now: float | None = None,
    skip_dir: Callable[[str, str], bool] | None = None,
) -> list[Path]:
    """Files under `root` whose basename matches `pattern` and whose mtime is recent.

    `skip_dir(dirpath, dirname)` returning True prunes that subtree before it is
    descended — the cheap way to avoid walking years of Codex date directories. A root
    that does not exist, or a directory that cannot be listed (a sleeping WSL distro),
    yields nothing rather than raising: the providers treat "no files" as "no data".
    """
    now = time.time() if now is None else now
    cutoff = now - max_age_s
    out: list[Path] = []
    # os.walk swallows listing errors itself (onerror=None); the top-level check only
    # keeps a UNC root that is offline from producing an OSError out of `is_dir`.
    try:
        if not root.is_dir():
            return out
    except OSError:
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        if skip_dir is not None:
            dirnames[:] = [d for d in dirnames if not skip_dir(dirpath, d)]
        for name in filenames:
            if not fnmatch.fnmatch(name, pattern):
                continue
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full)
            except OSError:
                continue  # vanished between listing and stat — skip, not fatal
            if st.st_mtime >= cutoff:
                out.append(Path(full))
    return out


class FileMemo[T]:
    """Per-file parse cache keyed on ``(mtime_ns, size)``.

    `get()` re-runs `parse` only when the file changed since it was last seen; `prune()`
    forgets files that dropped out of the recent set so the memo cannot grow for the
    life of the process. Safe to share between the stats worker threads.
    """

    def __init__(self) -> None:
        self._entries: dict[str, tuple[int, int, T]] = {}
        self._lock = threading.Lock()

    def get(self, path: Path, parse: Callable[[Path], T]) -> T:
        """The cached value for `path`, or `parse(path)` if it changed or is new.

        `OSError` from the `stat` (the file vanished) propagates, like `parse` would.
        """
        st = os.stat(path)
        key = str(path)
        with self._lock:
            entry = self._entries.get(key)
        if entry is not None and entry[0] == st.st_mtime_ns and entry[1] == st.st_size:
            return entry[2]
        value = parse(path)
        with self._lock:
            self._entries[key] = (st.st_mtime_ns, st.st_size, value)
        return value

    def prune(self, keep: Iterable[Path]) -> None:
        """Forget every file not in `keep`."""
        wanted = {str(p) for p in keep}
        with self._lock:
            for key in [k for k in self._entries if k not in wanted]:
                del self._entries[key]

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
