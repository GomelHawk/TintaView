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


def test_failed_check_says_which_problem_it_was(monkeypatch, capsys):
    """"Network error, rate limit, or no releases" covers three problems with three
    different answers — and the most common one isn't the user's fault at all."""
    monkeypatch.setattr(U, "latest_release", lambda timeout=10.0: None)

    def fail_with(code: int):
        def _raise(req, timeout=None):
            raise urllib.error.HTTPError(U.GITHUB_API_URL, code, "boom", {}, None)
        return _raise

    monkeypatch.setattr(U.urllib.request, "urlopen", fail_with(404))
    assert U.run_update(check_only=False) == 1
    out = capsys.readouterr().out.lower()
    assert "no releases have been published" in out
    assert "isn't an error with your install" in out

    monkeypatch.setattr(U.urllib.request, "urlopen", fail_with(403))
    assert U.run_update(check_only=False) == 1
    assert "rate-limiting" in capsys.readouterr().out.lower()

    def unreachable(req, timeout=None):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(U.urllib.request, "urlopen", unreachable)
    assert U.run_update(check_only=False) == 1
    assert "could not reach github" in capsys.readouterr().out.lower()


def test_run_update_never_raises_on_missing_assets(monkeypatch):
    newer = f"{U._parse_version(__version__)[0]}.{U._parse_version(__version__)[1] + 1}.0"
    monkeypatch.setattr(U, "latest_release", lambda timeout=10.0: {"tag_name": newer, "assets": []})
    monkeypatch.setattr(U.sys, "platform", "linux")
    rc = U.run_update(check_only=False)
    assert rc == 1  # no install.sh asset found -> a clear failure, not a traceback


# --------------------------------------------------------------------------- Windows path


def _windows_release_assets() -> list[dict[str, Any]]:
    base = "https://example.invalid/download"
    return [
        {"name": "install.ps1", "browser_download_url": f"{base}/install.ps1"},
        {"name": "TintaView-9.9.9-win64.zip", "browser_download_url": f"{base}/zip"},
        {"name": "SHA256SUMS.txt", "browser_download_url": f"{base}/SHA256SUMS.txt"},
    ]


@pytest.fixture
def verified_script(monkeypatch, tmp_path):
    """Stub `_download_and_verify` out to a path that "passed" verification.

    The verification itself is covered above; these tests are about what gets *run*
    afterwards, which is where a Windows-specific regression would hide.
    """
    script = tmp_path / "install.ps1"
    script.write_text("# stub\n", encoding="utf-8")
    monkeypatch.setattr(U, "_download_and_verify", lambda *a, **k: script)
    return script


def test_windows_update_runs_install_ps1(monkeypatch, verified_script, capsys):
    launched: list[list[str]] = []
    monkeypatch.setattr(U.subprocess, "Popen", lambda argv, **kw: launched.append(argv))
    monkeypatch.setattr(U.shutil, "which", lambda name: r"C:\pwsh\powershell.exe")
    monkeypatch.setattr(U.sys, "prefix", "/not/an/installed/venv")

    rc = U._update_windows("9.9.9", _windows_release_assets())

    assert rc == 0
    assert len(launched) == 1
    argv = launched[0]
    assert argv[0] == r"C:\pwsh\powershell.exe"
    assert str(verified_script) in argv
    assert "-Silent" in argv, "the self-updater must never open an interactive wizard"
    # Windows client SKUs default to a Restricted execution policy, which refuses to run
    # any .ps1 from disk at all -- without this the update silently does nothing.
    assert argv[argv.index("-ExecutionPolicy") + 1] == "Bypass"
    assert "-NoProfile" in argv
    assert not any(str(a).lower().endswith(".exe") for a in argv[1:]), (
        "there is no .exe installer any more; the update path must go through install.ps1"
    )


def test_windows_update_passes_its_own_install_prefix(monkeypatch, verified_script, tmp_path):
    launched: list[list[str]] = []
    monkeypatch.setattr(U.subprocess, "Popen", lambda argv, **kw: launched.append(argv))
    monkeypatch.setattr(U.shutil, "which", lambda name: "powershell.exe")

    # install.ps1's layout: <prefix>/venv, so sys.prefix is the venv and the install
    # prefix is its parent.
    prefix = tmp_path / "PortableInstall"
    (prefix / "venv").mkdir(parents=True)
    monkeypatch.setattr(U.sys, "prefix", str(prefix / "venv"))

    rc = U._update_windows("9.9.9", _windows_release_assets())

    assert rc == 0
    argv = launched[0]
    # A non-default install location must upgrade itself, not spawn a second copy under
    # %LOCALAPPDATA% (which is install.ps1's default prefix).
    assert argv[argv.index("-Prefix") + 1] == str(prefix.resolve())


def test_windows_update_from_a_checkout_passes_no_prefix(monkeypatch, verified_script, tmp_path):
    """A dev run outside an installed venv must not claim its cwd is an install prefix."""
    launched: list[list[str]] = []
    monkeypatch.setattr(U.subprocess, "Popen", lambda argv, **kw: launched.append(argv))
    monkeypatch.setattr(U.shutil, "which", lambda name: "powershell.exe")
    monkeypatch.setattr(U.sys, "prefix", str(tmp_path / "some-checkout" / ".venv"))

    assert U._update_windows("9.9.9", _windows_release_assets()) == 0
    assert "-Prefix" not in launched[0]


def test_windows_update_without_install_ps1_asset_fails_cleanly(monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise AssertionError("nothing may be launched when the asset is missing")

    monkeypatch.setattr(U.subprocess, "Popen", boom)
    assets = [{"name": "SHA256SUMS.txt", "browser_download_url": "https://example.invalid/s"}]

    rc = U._update_windows("9.9.9", assets)

    assert rc == 1
    assert "install.ps1" in capsys.readouterr().out


def test_windows_update_never_launches_an_unverified_script(monkeypatch, capsys):
    """A failed checksum must stop the update dead -- this is the supply-chain gate."""
    def boom(*args, **kwargs):
        raise AssertionError("an unverified script must never be executed")

    monkeypatch.setattr(U.subprocess, "Popen", boom)
    monkeypatch.setattr(U, "_download_and_verify", lambda *a, **k: None)

    assert U._update_windows("9.9.9", _windows_release_assets()) == 1


def test_run_update_dispatches_to_the_windows_path(monkeypatch):
    newer = f"{U._parse_version(__version__)[0]}.{U._parse_version(__version__)[1] + 1}.0"
    monkeypatch.setattr(U, "latest_release", lambda timeout=10.0: {"tag_name": newer, "assets": []})
    monkeypatch.setattr(U.sys, "platform", "win32")

    seen: list[str] = []
    monkeypatch.setattr(U, "_update_windows", lambda v, a: seen.append(v) or 0)

    assert U.run_update(check_only=False) == 0
    assert seen == [newer]
