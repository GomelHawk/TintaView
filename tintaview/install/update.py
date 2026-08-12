"""``tintaview update`` — self-update from the GitHub Releases API.

Per docs/PLAN.md §8.4: config and every agent's hook configuration are never touched by
an update (hooks point at the stable ``tv-hook`` path, not at anything version-specific),
so this module only ever downloads and runs the release's own install script —
``install.ps1`` on Windows, ``install.sh`` on Linux/macOS, both of which are idempotent
and upgrade in place. It never opens ``config.toml`` or any agent's settings file.

Safety rules that are non-negotiable, not just best-effort:

- An installer/script is **never** executed before its SHA-256 is checked against a
  checksums file published alongside it in the same release. A mismatch deletes the
  download and aborts loudly — this is the one place a supply-chain slip would be
  catastrophic, so "fail closed" beats "fail helpful".
- ``check_only`` never downloads anything, regardless of whether an update exists.
- Every network failure, rate limit, or missing release degrades to a clear, printed
  message and a non-zero exit — never an uncaught traceback reaching the user's shell.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .. import __version__

log = logging.getLogger(__name__)

GITHUB_REPO = "GomelHawk/TintaView"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
USER_AGENT = "TintaView-self-updater"

#: Every filename this module will accept as "the" checksums file for a release, tried
#: in order. build.yml (docs/PLAN.md §8.5) is expected to publish exactly one of these
#: alongside the installer/script.
_CHECKSUM_ASSET_NAMES = ("SHA256SUMS", "SHA256SUMS.txt", "checksums.txt", "sha256sums.txt")

_DOWNLOAD_TIMEOUT = 60.0


# --------------------------------------------------------------------------- version compare


def _parse_version(text: str) -> tuple[int, ...]:
    """Turn "v1.2.3", "1.2.3-rc1", "0.10.0" etc. into a comparable int tuple.

    A naive string compare would put "0.9.0" *after* "0.10.0" (lexicographic '1' < '9'
    is false the other way around — "0.10.0" < "0.9.0" as strings), which is exactly
    backwards. Comparing parsed integer tuples gets this right regardless of how many
    digits any component has.
    """
    text = text.strip()
    if text.startswith(("v", "V")):
        text = text[1:]
    core = text.split("+", 1)[0].split("-", 1)[0]  # drop build/pre-release metadata
    parts: list[int] = []
    for piece in core.split("."):
        digits = ""
        for ch in piece:
            if not ch.isdigit():
                break
            digits += ch
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def compare_versions(a: str, b: str) -> int:
    """-1 if a<b, 0 if a==b, 1 if a>b — numeric, not lexicographic."""
    pa, pb = _parse_version(a), _parse_version(b)
    n = max(len(pa), len(pb))
    pa = pa + (0,) * (n - len(pa))
    pb = pb + (0,) * (n - len(pb))
    if pa < pb:
        return -1
    if pa > pb:
        return 1
    return 0


# --------------------------------------------------------------------------- GitHub API


def latest_release(timeout: float = 10.0) -> dict[str, Any] | None:
    """The parsed "latest release" JSON from the GitHub API, or None on any problem
    (network error, rate limit, no releases yet, unexpected shape). Never raises."""
    req = urllib.request.Request(
        GITHUB_API_URL,
        headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        log.warning("update check failed: GitHub returned HTTP %s (%s)", exc.code, exc.reason)
        return None
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        log.warning("update check failed: %s", exc)
        return None

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        log.warning("update check failed: unreadable response (%s)", exc)
        return None

    if not isinstance(data, dict) or "tag_name" not in data:
        log.warning("update check failed: unexpected response shape")
        return None
    return data


# --------------------------------------------------------------------------- download + verify


def _asset_url(assets: list[dict[str, Any]], name: str) -> str | None:
    for asset in assets:
        if asset.get("name") == name:
            url = asset.get("browser_download_url")
            return str(url) if url else None
    return None


def _find_checksums_asset(assets: list[dict[str, Any]]) -> str | None:
    names = {asset.get("name") for asset in assets}
    for candidate in _CHECKSUM_ASSET_NAMES:
        if candidate in names:
            return candidate
    return None


def _download(url: str, dest: Path, timeout: float = _DOWNLOAD_TIMEOUT) -> None:
    """Raises OSError (which covers urllib.error.URLError/HTTPError) on any failure."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as out:
        shutil.copyfileobj(resp, out)


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_checksums(text: str, filename: str) -> str | None:
    """Find `filename`'s hex digest in a standard `sha256sum`-style checksums file
    (``<hex>  <name>`` or ``<hex> *<name>`` for binary mode)."""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        digest, name = parts[0], parts[-1].lstrip("*")
        if name == filename or name.endswith(f"/{filename}"):
            return digest
    return None


def _download_and_verify(url: str, checksums_url: str, dest_dir: Path, filename: str) -> Path | None:
    """Download `filename` and its checksums file, verify, and return the verified path.

    Returns None (after deleting the download) on any mismatch or download failure —
    the caller is expected to have already printed why. Never runs anything.
    """
    target = dest_dir / filename
    checksums_path = dest_dir / "CHECKSUMS"

    try:
        _download(url, target)
        _download(checksums_url, checksums_path)
    except OSError as exc:
        print(f"Download failed: {exc}")
        target.unlink(missing_ok=True)
        return None

    checksums_text = checksums_path.read_text(encoding="utf-8", errors="ignore")
    expected = _parse_checksums(checksums_text, filename)
    if not expected:
        print(f"No checksum for {filename} found in the release's checksums file — "
              "refusing to run an unverified download.")
        target.unlink(missing_ok=True)
        return None

    actual = _sha256_of(target)
    if actual.lower() != expected.lower():
        print(
            f"SHA-256 mismatch for {filename}: expected {expected}, got {actual}. "
            "Deleting the download and aborting — an unverified installer/script is never run."
        )
        target.unlink(missing_ok=True)
        return None

    print(f"SHA-256 verified for {filename}.")
    return target


# --------------------------------------------------------------------------- platform actions


def _fetch_install_script(assets: list[dict[str, Any]], script_name: str) -> Path | None:
    """Download and SHA-256-verify one of the release's install scripts.

    Returns None (having printed why) if the asset, its checksums file, or the checksum
    itself is missing or wrong — never a path to something unverified.
    """
    script_url = _asset_url(assets, script_name)
    if not script_url:
        print(f"Could not find {script_name} in the release assets — it may still be building; try again shortly.")
        return None

    checksums_name = _find_checksums_asset(assets)
    if not checksums_name:
        print("No checksums file found in the release assets — refusing to run an unverified script.")
        return None
    checksums_url = _asset_url(assets, checksums_name)
    if not checksums_url:
        print(f"{checksums_name} was listed but has no download URL — refusing to run an unverified script.")
        return None

    tmp_dir = Path(tempfile.mkdtemp(prefix="tintaview-update-"))
    return _download_and_verify(script_url, checksums_url, tmp_dir, script_name)


def _install_prefix() -> Path | None:
    """The install prefix this copy of TintaView lives under, or None if it isn't one.

    install.ps1 lays out ``<prefix>/venv``, so the prefix is the parent of the virtual
    environment `sys.prefix` points at. Returning it lets an update reinstall into a
    non-default location instead of silently creating a second copy under
    ``%LOCALAPPDATA%`` (install.ps1's default). A run straight from a checkout, or from a
    venv the installer did not create, has no prefix to speak of — None leaves install.ps1
    on its own default, which is the right answer there.
    """
    venv = Path(sys.prefix).resolve()
    if venv.name.lower() != "venv":
        return None
    return venv.parent


def _update_windows(version: str, assets: list[dict[str, Any]]) -> int:
    del version  # install.ps1 is not versioned by filename; it resolves the release itself

    script_path = _fetch_install_script(assets, "install.ps1")
    if script_path is None:
        return 1

    powershell = shutil.which("powershell.exe") or shutil.which("powershell") or "powershell.exe"
    # -ExecutionPolicy Bypass is required here, not belt-and-braces: the default policy on
    # Windows client SKUs is Restricted, which refuses to run any .ps1 from disk at all.
    # (The file carries no Mark-of-the-Web — urllib wrote it, not a browser — so the
    # execution policy is the only gate actually in the way.)
    argv = [
        powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", str(script_path), "-Silent",
    ]
    prefix = _install_prefix()
    if prefix is not None:
        argv += ["-Prefix", str(prefix)]

    print(f"Running {script_path.name} — it is idempotent and upgrades TintaView in place.")
    print("Config and every agent's hook configuration are untouched by this — the hook path is stable.")
    # Detached, unlike the POSIX path below: install.ps1 stops any interpreter running out
    # of the venv it is about to replace, and on `tintaview update` from the tray or the
    # installed CLI that process is *this* one. Waiting on the script would mean waiting
    # for something that is about to kill the waiter.
    try:
        subprocess.Popen(argv)
    except OSError as exc:
        print(f"Could not launch the installer: {exc}")
        return 1
    print("TintaView will exit so the installer can replace it.")
    return 0


def _update_posix(version: str, assets: list[dict[str, Any]]) -> int:
    del version  # install.sh is not versioned by filename either

    script_path = _fetch_install_script(assets, "install.sh")
    if script_path is None:
        return 1

    print(f"Running `sh {script_path}` — it is idempotent and upgrades TintaView in place.")
    print("Config and every agent's hook configuration are untouched by this — the hook path is stable.")
    try:
        result = subprocess.run(["sh", str(script_path)], check=False)
    except OSError as exc:
        print(f"Could not run the install script: {exc}")
        return 1
    return result.returncode


# --------------------------------------------------------------------------- entry point


def _check_failure_reason() -> str:
    """A specific explanation for a failed update check, not a list of maybes.

    "Network error, rate limit, or no releases found" covers three problems with three
    different answers — and the most common one by far, a project that simply has not cut
    a release yet, is not something the user can act on at all. One extra request on the
    failure path buys a message that says which it was.
    """
    req = urllib.request.Request(
        GITHUB_API_URL,
        headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT},
    )
    try:
        urllib.request.urlopen(req, timeout=10.0)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return (
                f"No releases have been published for {GITHUB_REPO} yet, so there is "
                "nothing to update to. This isn't an error with your install."
            )
        if exc.code in (403, 429):
            return (
                "GitHub is rate-limiting update checks from this machine right now. "
                "Try again in a few minutes."
            )
        return f"Update check failed: GitHub returned HTTP {exc.code} ({exc.reason})."
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return f"Could not reach GitHub to check for updates: {exc}."
    return (
        "Could not read the latest release from GitHub — the response wasn't in the "
        f"expected form. Check https://github.com/{GITHUB_REPO}/releases yourself."
    )


def run_update(check_only: bool = False) -> int:
    release = latest_release()
    if release is None:
        print(_check_failure_reason())
        return 1

    latest_version = str(release.get("tag_name") or "").strip()
    if not latest_version:
        print("The latest GitHub release has no version tag — nothing to compare against.")
        return 1
    latest_version = latest_version[1:] if latest_version[:1] in ("v", "V") else latest_version

    if compare_versions(__version__, latest_version) >= 0:
        print(f"TintaView {__version__} is up to date (latest release: {latest_version}).")
        return 0

    print(f"An update is available: {__version__} -> {latest_version}")
    print(
        "Config and every agent's hook configuration are never touched by an update — "
        "hooks point at the stable tv-hook path, not at anything version-specific."
    )

    if check_only:
        return 0

    assets = release.get("assets") or []
    if not isinstance(assets, list):
        assets = []
    if sys.platform == "win32":
        return _update_windows(latest_version, assets)
    return _update_posix(latest_version, assets)
