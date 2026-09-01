"""``tintaview update`` — self-update from the GitHub Releases API.

Per AGENTS.md ("Updating"): config and every agent's hook configuration are never touched by
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
#: The *list* endpoint, which — unlike `/releases/latest` — includes pre-releases.
#: Only the beta channel reads it. One page is plenty: a pre-release newer than
#: everything on the first page of 30 would have to be older than 30 later releases.
GITHUB_RELEASES_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases?per_page=30"
USER_AGENT = "TintaView-self-updater"

#: `update.channel` values. Anything else in a hand-edited config is treated as
#: "stable" — same forgiving-parse policy as `engine.mode` and `ui.language`.
CHANNEL_STABLE = "stable"
CHANNEL_BETA = "beta"
CHANNELS: tuple[str, ...] = (CHANNEL_STABLE, CHANNEL_BETA)

#: Every filename this module will accept as "the" checksums file for a release, tried
#: in order. build.yml (AGENTS.md, "CI and release") is expected to publish exactly one of these
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

    Release components only — the pre-release suffix is `_parse_prerelease`'s job.
    """
    text = _strip_v(text)
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


def _strip_v(text: str) -> str:
    text = text.strip()
    return text[1:] if text[:1] in ("v", "V") else text


def _parse_prerelease(text: str) -> str:
    """The pre-release suffix of a tag ("rc1" for "v1.2.0-rc1"), "" for a final release.

    Build metadata (`+abc`) is dropped: semver says it never affects precedence.
    """
    text = _strip_v(text).split("+", 1)[0]
    _core, _, pre = text.partition("-")
    return pre


def _prerelease_key(pre: str) -> tuple:
    """Sort key for a pre-release suffix, per semver identifier rules: numeric
    identifiers rank below alphanumeric ones and compare numerically, so `rc2` follows
    `rc1` and `beta.9` precedes `beta.10` (which a plain string compare gets backwards).
    """
    out = []
    for ident in pre.split("."):
        out.append((0, int(ident), "") if ident.isdigit() else (1, 0, ident))
    return tuple(out)


def compare_versions(a: str, b: str) -> int:
    """-1 if a<b, 0 if a==b, 1 if a>b — numeric, not lexicographic.

    Pre-releases are ordered, not discarded: `1.2.0-rc1 < 1.2.0-rc2 < 1.2.0`. That
    matters for the beta channel, which installs pre-release tags — treating the suffix
    as noise (as this did before there was a beta channel) would pin anyone running an
    rc to that rc forever, since every later rc *and* the final release would compare
    equal to it and so never count as "newer".
    """
    pa, pb = _parse_version(a), _parse_version(b)
    n = max(len(pa), len(pb))
    pa = pa + (0,) * (n - len(pa))
    pb = pb + (0,) * (n - len(pb))
    if pa != pb:
        return -1 if pa < pb else 1

    pre_a, pre_b = _parse_prerelease(a), _parse_prerelease(b)
    if pre_a == pre_b:
        return 0
    # A release with no suffix outranks the same release with one: 1.2.0 > 1.2.0-rc1.
    if not pre_a:
        return 1
    if not pre_b:
        return -1
    ka, kb = _prerelease_key(pre_a), _prerelease_key(pre_b)
    if ka == kb:
        return 0
    return -1 if ka < kb else 1


def normalize_channel(channel: str | None) -> str:
    """`update.channel` as one of `CHANNELS`; anything unrecognised means stable."""
    value = (channel or "").strip().lower()
    return value if value in CHANNELS else CHANNEL_STABLE


# --------------------------------------------------------------------------- GitHub API


def _get_json(url: str, timeout: float) -> Any | None:
    """Parsed JSON from a GitHub API URL, or None on any problem (network error, rate
    limit, unreadable body). Never raises."""
    req = urllib.request.Request(
        url,
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
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        log.warning("update check failed: unreadable response (%s)", exc)
        return None


def latest_release(timeout: float = 10.0, channel: str = CHANNEL_STABLE) -> dict[str, Any] | None:
    """The release this install should update to, or None on any problem (network
    error, rate limit, no releases yet, unexpected shape). Never raises.

    `channel="stable"` asks GitHub for `/releases/latest`, which already excludes drafts
    and pre-releases. `channel="beta"` reads the release *list* instead and picks the
    highest version on it, pre-releases included — GitHub returns that list in
    publication order, which is not version order once a patch to an older line ships
    after a newer pre-release, so the pick is by parsed version rather than by position.
    """
    if normalize_channel(channel) == CHANNEL_BETA:
        return _latest_beta_release(timeout)

    data = _get_json(GITHUB_API_URL, timeout)
    if not isinstance(data, dict) or "tag_name" not in data:
        log.warning("update check failed: unexpected response shape")
        return None
    return data


def _latest_beta_release(timeout: float) -> dict[str, Any] | None:
    """Highest-versioned non-draft release, pre-releases included."""
    data = _get_json(GITHUB_RELEASES_URL, timeout)
    if not isinstance(data, list):
        log.warning("update check failed: unexpected response shape")
        return None

    # Drafts are excluded but pre-releases are not: shipping the unpublished one is the
    # single thing this channel must never do, and offering the pre-release is the
    # single thing it exists to do.
    candidates = [
        r for r in data
        if isinstance(r, dict) and r.get("tag_name") and not r.get("draft")
    ]
    if not candidates:
        log.warning("update check failed: no published releases")
        return None

    best = candidates[0]
    for release in candidates[1:]:
        if compare_versions(str(release["tag_name"]), str(best["tag_name"])) > 0:
            best = release
    return best


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


def configured_channel() -> str:
    """`update.channel` from the config, or "stable" if it can't be read.

    Read here rather than passed in from every call site so `tintaview update` honours
    the setting with no extra flag. Failure is never fatal — an unreadable config means
    the safe channel, not a failed update.
    """
    try:
        from tintaview.core import config as config_mod

        return normalize_channel(config_mod.load().update.channel)
    except Exception:  # noqa: BLE001 - a bad config must not break updating
        log.warning("could not read update.channel; using %s", CHANNEL_STABLE)
        return CHANNEL_STABLE


def run_update(check_only: bool = False, channel: str | None = None) -> int:
    channel = normalize_channel(channel) if channel is not None else configured_channel()
    release = latest_release(channel=channel)
    if release is None:
        print(_check_failure_reason())
        return 1

    latest_version = str(release.get("tag_name") or "").strip()
    if not latest_version:
        print("The latest GitHub release has no version tag — nothing to compare against.")
        return 1
    latest_version = _strip_v(latest_version)

    if compare_versions(__version__, latest_version) >= 0:
        print(f"TintaView {__version__} is up to date (latest {channel} release: {latest_version}).")
        return 0

    print(f"An update is available: {__version__} -> {latest_version}")
    if channel == CHANNEL_BETA and _parse_prerelease(latest_version):
        print(
            "This is a pre-release, offered because `update.channel = \"beta\"` is set "
            "in your config. Set it back to \"stable\" for released versions only."
        )
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
