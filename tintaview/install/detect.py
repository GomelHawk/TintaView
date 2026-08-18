"""Working out what kind of machine this is, and therefore what shape the install takes.

The interesting case is Windows + WSL. Claude Code and Codex usually run *inside* the
distro while the lights, the tray and (for Cursor) the app data live on the Windows side,
so one logical install spans two filesystems: hooks in WSL, daemon and tray on Windows.
Getting this wrong is the difference between "works" and "silently never fires", so it is
detected explicitly and always overridable by the user.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

PLATFORM_WINDOWS = "windows"
PLATFORM_WSL = "wsl"  # running *inside* a WSL distro
PLATFORM_LINUX = "linux"
PLATFORM_MACOS = "macos"

#: Install topologies the wizard can produce.
MODE_NATIVE = "native"  # everything on one machine
MODE_WSL_SPLIT = "wsl-split"  # daemon+tray on Windows, hooks inside a WSL distro


@dataclass
class Environment:
    platform: str
    mode: str
    distro: str | None = None  # WSL distro name when relevant
    wsl_distros: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def is_windows_side(self) -> bool:
        return self.platform == PLATFORM_WINDOWS

    @property
    def supports_chroma(self) -> bool:
        # The Chroma REST SDK is Windows-only. From inside WSL the daemon runs on the
        # Windows side, so Chroma is still reachable there — but not from this process.
        return self.platform == PLATFORM_WINDOWS

    @property
    def supports_ghub(self) -> bool:
        # The LED Illumination SDK DLL G HUB installs is a Windows binary, same
        # reasoning as supports_chroma — including the WSL-split case, where the
        # daemon that would load it runs on the Windows side, not this process.
        return self.platform == PLATFORM_WINDOWS

    @property
    def supports_openrgb(self) -> bool:
        return self.platform in (PLATFORM_WINDOWS, PLATFORM_LINUX, PLATFORM_WSL, PLATFORM_MACOS)


def is_wsl() -> bool:
    """True when this Python is running inside a WSL distro.

    /proc/version carries the Microsoft kernel signature; WSL_DISTRO_NAME is set by WSL2
    but can be absent under some shells, so check both.
    """
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        with open("/proc/version", encoding="utf-8", errors="ignore") as fh:
            return "microsoft" in fh.read().lower()
    except OSError:
        return False


def wsl_distros() -> list[str]:
    """Distro names visible from the Windows side, newest listing order preserved."""
    exe = shutil.which("wsl.exe") or shutil.which("wsl")
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "-l", "-q"], capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    # wsl.exe -l -q emits UTF-16LE on most builds.
    raw = out.stdout
    for encoding in ("utf-16-le", "utf-8"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        return []
    return [line.strip() for line in text.replace("\x00", "").splitlines() if line.strip()]


def detect(override: str | None = None) -> Environment:
    """Detect the environment. `override` forces the platform when detection is wrong."""
    if override:
        plat = override
    elif sys.platform == "win32":
        plat = PLATFORM_WINDOWS
    elif sys.platform == "darwin":
        plat = PLATFORM_MACOS
    elif is_wsl():
        plat = PLATFORM_WSL
    else:
        plat = PLATFORM_LINUX

    env = Environment(platform=plat, mode=MODE_NATIVE)

    if plat == PLATFORM_WINDOWS:
        env.wsl_distros = wsl_distros()
        if env.wsl_distros:
            env.mode = MODE_WSL_SPLIT
            env.distro = env.wsl_distros[0]
            env.notes.append(
                "WSL detected: the daemon and tray install on Windows, and the hooks "
                "install inside the distro where your agents actually run."
            )
    elif plat == PLATFORM_WSL:
        env.mode = MODE_WSL_SPLIT
        env.distro = os.environ.get("WSL_DISTRO_NAME")
        env.notes.append(
            "Running inside WSL. Lighting needs the daemon on the Windows side — run the "
            "Windows installer there; this side only needs the hooks."
        )
    elif plat == PLATFORM_MACOS:
        env.notes.append(
            "macOS has no Razer Chroma SDK and only limited OpenRGB device support — "
            "expect status + usage rather than lighting."
        )
    return env


def windows_home_from_wsl() -> Path | None:
    """The Windows user profile as seen from inside WSL (for Cursor's app data)."""
    if not is_wsl():
        return None
    try:
        out = subprocess.run(
            ["cmd.exe", "/c", "echo %USERPROFILE%"],
            capture_output=True, timeout=10, check=False, cwd="/",
        )
        text = out.stdout.decode("utf-8", "ignore").strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not text or ":" not in text:
        return None
    drive, _, rest = text.partition(":")
    return Path(f"/mnt/{drive.lower()}{rest}".replace("\\", "/"))


def wsl_path_to_unc(distro: str, path: Path | str) -> str:
    r"""A WSL path as Windows sees it: /home/u/.claude -> \\wsl.localhost\Ubuntu\home\u\.claude

    The Windows-side tray reads the agents' data (transcripts, credentials) through this,
    so it is written into the Windows config at install time.
    """
    p = str(path).replace("/", "\\")
    return f"\\\\wsl.localhost\\{distro}{p}"


def describe() -> str:
    """One-line summary for `tintaview doctor`."""
    env = detect()
    bits = [f"platform={env.platform}", f"mode={env.mode}", f"python={platform.python_version()}"]
    if env.distro:
        bits.append(f"distro={env.distro}")
    return " ".join(bits)
