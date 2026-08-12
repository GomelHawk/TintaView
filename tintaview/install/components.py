"""Detecting and installing the pieces an engine needs before it can work.

Choosing an engine in the wizard used to be a pure config edit: TintaView would write
``engine.mode = "openrgb"`` whether or not anything needed to drive OpenRGB was present,
and the first sign of trouble was no lighting at all plus a `doctor` line blaming the SDK
server. There are three separate prerequisites and they fail in ways a user cannot tell
apart from the outside:

1. **openrgb-python**, the client library. Optional in `pyproject.toml`, so an install
   that predates it shipping by default simply cannot talk to OpenRGB. TintaView can fix
   this itself — it is a small pure-Python package and the target is TintaView's own
   virtual environment, not the user's system.
2. **The OpenRGB application.** TintaView can offer to install it through `winget` on
   Windows, but only with explicit confirmation: it is third-party software, the install
   may ask for elevation, and it is emphatically not something to do behind someone's
   back because they picked an option in a list.
3. **Its SDK server**, which is off by default and can only be enabled inside OpenRGB's
   own UI. Nothing here can do that; the most useful thing is to say exactly where it is.

Everything in this module is best-effort and never raises: a prerequisite check that
crashes the wizard would be worse than the missing prerequisite.
"""

from __future__ import annotations

import importlib.util
import logging
import shutil
import subprocess
import sys

log = logging.getLogger(__name__)

#: The winget package that provides the OpenRGB application on Windows.
OPENRGB_WINGET_ID = "OpenRGB.OpenRGB"

_PIP_TIMEOUT = 300  # a small pure-Python wheel, but allow for a slow index
_WINGET_QUERY_TIMEOUT = 60
_WINGET_INSTALL_TIMEOUT = 900  # a real download plus a possible UAC prompt


def openrgb_python_installed() -> bool:
    """Is the `openrgb` client library importable in *this* interpreter?"""
    try:
        return importlib.util.find_spec("openrgb") is not None
    except (ImportError, ValueError):
        return False


def install_openrgb_python() -> tuple[bool, str]:
    """`pip install openrgb-python` into the running environment.

    Returns (ok, message). Uses `sys.executable`, so it lands in TintaView's own venv —
    the same place the app will import from — rather than wherever a bare `pip` points.
    """
    cmd = [sys.executable, "-m", "pip", "install", "--quiet", "--upgrade", "openrgb-python"]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_PIP_TIMEOUT, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not run pip: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        return False, detail[-1] if detail else f"pip exited {result.returncode}"

    # find_spec caches negative results per interpreter run, so a package installed just
    # now stays "missing" until the finders are told to look again.
    importlib.invalidate_caches()
    return True, "openrgb-python installed"


def winget_available() -> bool:
    return sys.platform == "win32" and shutil.which("winget") is not None


def winget_package_installed(package_id: str) -> bool | None:
    """True/False, or None when winget can't answer (not present, timeout, error).

    None matters: it means "unknown", and offering to install something that may already
    be there is better than either assuming it is or assuming it isn't.
    """
    if not winget_available():
        return None
    try:
        result = subprocess.run(
            ["winget", "list", "--id", package_id, "--exact", "--disable-interactivity"],
            capture_output=True, text=True, timeout=_WINGET_QUERY_TIMEOUT, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("winget list failed: %s", exc)
        return None
    if result.returncode != 0:
        return False  # winget exits non-zero when nothing matches
    return package_id.lower() in (result.stdout or "").lower()


def winget_install(package_id: str) -> tuple[bool, str]:
    """Install `package_id` with winget. Returns (ok, message)."""
    if not winget_available():
        return False, "winget isn't available on this machine"
    cmd = [
        "winget", "install", "--id", package_id, "--exact", "--source", "winget",
        "--accept-package-agreements", "--accept-source-agreements",
    ]
    try:
        # Not captured: winget prints a progress bar and may raise a UAC prompt, and a
        # silent multi-minute wait with no output looks like a hang.
        result = subprocess.run(cmd, timeout=_WINGET_INSTALL_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not run winget: {exc}"
    if result.returncode != 0:
        return False, f"winget exited {result.returncode}"
    return True, f"{package_id} installed"
