"""Tests for the self-updater: version comparison, checksum verification, and the
overall `run_update` flow. Nothing here may touch the real network — every GitHub API
call and every download goes through a monkeypatched stand-in.
"""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from tintaview import __version__
from tintaview.install import update as U

# --------------------------------------------------------------------------- version compare


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("0.9.0", "0.10.0", -1),  # the whole point: lexicographic compare gets this backwards
        ("0.10.0", "0.9.0", 1),
        ("1.2.3", "1.2.3", 0),
        ("1.2.3", "1.2.4", -1),
        ("2.0.0", "1.9.9", 1),
        ("v1.0.0", "1.0.0", 0),  # a leading "v" tag must not affect comparison
        ("1.0", "1.0.0", 0),  # missing components compare as zero
        ("1.0.0-rc1", "1.0.0", 0),  # pre-release metadata is stripped before comparing
    ],
)
def test_compare_versions(a: str, b: str, expected: int) -> None:
    assert U.compare_versions(a, b) == expected


# --------------------------------------------------------------------------- latest_release


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._payload


def _release_payload(tag: str = "v9.9.9", assets: list[dict[str, Any]] | None = None) -> bytes:
    return json.dumps({"tag_name": tag, "assets": assets or []}).encode("utf-8")


def test_latest_release_parses_a_normal_response(monkeypatch):
    monkeypatch.setattr(
        U.urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(_release_payload())
    )
    release = U.latest_release()
    assert release is not None
    assert release["tag_name"] == "v9.9.9"


def test_latest_release_network_error_returns_none(monkeypatch, caplog):
    def boom(req, timeout=None):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(U.urllib.request, "urlopen", boom)
    assert U.latest_release() is None


def test_latest_release_rate_limited_returns_none(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.HTTPError(url="x", code=403, msg="rate limited", hdrs=None, fp=None)

    monkeypatch.setattr(U.urllib.request, "urlopen", boom)
    assert U.latest_release() is None


def test_latest_release_missing_release_returns_none(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.HTTPError(url="x", code=404, msg="not found", hdrs=None, fp=None)

    monkeypatch.setattr(U.urllib.request, "urlopen", boom)
    assert U.latest_release() is None


def test_latest_release_garbage_body_returns_none(monkeypatch):
    monkeypatch.setattr(
        U.urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(b"not json at all")
    )
    assert U.latest_release() is None


# --------------------------------------------------------------------------- checksum verification


def test_parse_checksums_finds_matching_line():
    text = "aaaa  other-file.txt\nbbbb  install.sh\n"
    assert U._parse_checksums(text, "install.sh") == "bbbb"
    assert U._parse_checksums(text, "missing.exe") is None


def test_download_and_verify_mismatch_aborts_and_removes_file(tmp_path, monkeypatch):
    calls: list[str] = []

    def fake_download(url: str, dest: Path, timeout: float = 60.0) -> None:
        calls.append(url)
        if dest.name == "install.sh":
            dest.write_text("#!/bin/sh\necho hi\n")
        else:
            # checksum in the file does NOT match the real content above.
            dest.write_text(f"{'0' * 64}  install.sh\n")

    monkeypatch.setattr(U, "_download", fake_download)

    result = U._download_and_verify(
        "https://example.invalid/install.sh",
        "https://example.invalid/SHA256SUMS",
        tmp_path,
        "install.sh",
    )

    assert result is None
    assert not (tmp_path / "install.sh").exists(), "a mismatched download must be deleted"
    assert len(calls) == 2


def test_download_and_verify_success_returns_path(tmp_path, monkeypatch):
    import hashlib

    content = b"#!/bin/sh\necho hi\n"
    digest = hashlib.sha256(content).hexdigest()

    def fake_download(url: str, dest: Path, timeout: float = 60.0) -> None:
        if dest.name == "install.sh":
            dest.write_bytes(content)
        else:
            dest.write_text(f"{digest}  install.sh\n")

    monkeypatch.setattr(U, "_download", fake_download)

    result = U._download_and_verify(
        "https://example.invalid/install.sh",
        "https://example.invalid/SHA256SUMS",
        tmp_path,
        "install.sh",
    )

    assert result == tmp_path / "install.sh"
    assert result.exists()


# --------------------------------------------------------------------------- run_update


def test_check_only_never_downloads(monkeypatch, capsys):
    newer = f"{U._parse_version(__version__)[0]}.{U._parse_version(__version__)[1] + 1}.0"
    monkeypatch.setattr(U, "latest_release", lambda timeout=10.0: {"tag_name": newer, "assets": []})

    def boom(*args, **kwargs):
        raise AssertionError("check_only must never download anything")

    monkeypatch.setattr(U, "_download", boom)
    monkeypatch.setattr(U, "_download_and_verify", boom)

    rc = U.run_update(check_only=True)
    assert rc == 0
    out = capsys.readouterr().out
    assert "update is available" in out.lower()


def test_up_to_date_reports_and_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr(U, "latest_release", lambda timeout=10.0: {"tag_name": __version__, "assets": []})
    rc = U.run_update(check_only=False)
    assert rc == 0
    assert "up to date" in capsys.readouterr().out.lower()


def test_network_error_exits_nonzero_with_readable_message(monkeypatch, capsys):
    monkeypatch.setattr(U, "latest_release", lambda timeout=10.0: None)
    rc = U.run_update(check_only=False)
    assert rc == 1
    out = capsys.readouterr().out
    assert out.strip() != ""
    assert "could not check" in out.lower()


def test_run_update_never_raises_on_missing_assets(monkeypatch):
    newer = f"{U._parse_version(__version__)[0]}.{U._parse_version(__version__)[1] + 1}.0"
    monkeypatch.setattr(U, "latest_release", lambda timeout=10.0: {"tag_name": newer, "assets": []})
    monkeypatch.setattr(U.sys, "platform", "linux")
    rc = U.run_update(check_only=False)
    assert rc == 1  # no install.sh asset found -> a clear failure, not a traceback
