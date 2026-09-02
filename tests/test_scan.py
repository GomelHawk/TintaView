"""`stats/_scan.py`: the shared recent-file walk and per-file memo.

Both halves of AGENTS.md's rule for transcript scans over a WSL-split UNC path — only
files modified inside the window are read, and unchanged files are not re-parsed — live
here, so this is where they are pinned.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tintaview.stats._scan import WEEK_S, FileMemo, recent_files

NOW = 1_800_000_000.0


def _touch(path: Path, age_s: float, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.utime(path, (NOW - age_s, NOW - age_s))
    return path


class TestRecentFiles:
    def test_keeps_only_matching_files_inside_the_window(self, tmp_path):
        fresh = _touch(tmp_path / "a" / "s1.jsonl", age_s=3600)
        _touch(tmp_path / "a" / "s2.jsonl", age_s=WEEK_S + 60)  # a minute too old
        _touch(tmp_path / "a" / "notes.txt", age_s=60)  # wrong suffix

        found = recent_files(tmp_path, "*.jsonl", WEEK_S, now=NOW)

        assert found == [fresh]

    def test_missing_root_yields_nothing(self, tmp_path):
        assert recent_files(tmp_path / "nope", "*.jsonl", now=NOW) == []

    def test_skip_dir_prunes_subtrees_before_descending(self, tmp_path):
        keep = _touch(tmp_path / "2026" / "09" / "01" / "rollout-a.jsonl", age_s=60)
        _touch(tmp_path / "2019" / "01" / "01" / "rollout-b.jsonl", age_s=60)  # fresh but pruned
        visited: list[str] = []

        def skip(dirpath: str, name: str) -> bool:
            visited.append(name)
            return name == "2019"

        found = recent_files(tmp_path, "rollout-*.jsonl", now=NOW, skip_dir=skip)

        assert found == [keep]
        # Nothing under the pruned year was ever listed.
        assert "2019" in visited
        assert not any(str(p).startswith(str(tmp_path / "2019")) for p in found)


class TestFileMemo:
    def test_reparses_only_when_the_file_changes(self, tmp_path):
        path = _touch(tmp_path / "t.jsonl", age_s=60, text="one\n")
        memo: FileMemo[int] = FileMemo()
        calls = 0

        def parse(p: Path) -> int:
            nonlocal calls
            calls += 1
            return len(p.read_text(encoding="utf-8").splitlines())

        assert memo.get(path, parse) == 1
        assert memo.get(path, parse) == 1
        assert calls == 1  # unchanged file: served from the memo

        # Appending changes the size (and mtime), so the next get re-parses.
        with open(path, "a", encoding="utf-8") as f:
            f.write("two\n")
        assert memo.get(path, parse) == 2
        assert calls == 2

    def test_prune_forgets_files_outside_the_recent_set(self, tmp_path):
        a = _touch(tmp_path / "a.jsonl", age_s=60)
        b = _touch(tmp_path / "b.jsonl", age_s=60)
        memo: FileMemo[str] = FileMemo()
        memo.get(a, lambda p: p.name)
        memo.get(b, lambda p: p.name)
        assert len(memo) == 2

        memo.prune([a])

        assert len(memo) == 1
        # Re-fetching the forgotten one parses again.
        calls = 0

        def parse(p: Path) -> str:
            nonlocal calls
            calls += 1
            return p.name

        memo.get(b, parse)
        assert calls == 1

    def test_vanished_file_raises_like_the_parse_would(self, tmp_path):
        memo: FileMemo[str] = FileMemo()
        with pytest.raises(OSError):
            memo.get(tmp_path / "gone.jsonl", lambda p: p.name)
