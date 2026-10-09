"""Deploying the hook script itself (as opposed to wiring it into an agent's config).

Kept separate from :mod:`tintaview.install.hooks`, which edits the *agents'* files: this
one only writes TintaView's own two files, the hook binary and the environment file that
tells it where the daemon lives. Both the wizard and `tintaview hooks install` call it,
so the advice `doctor` prints ("run `tintaview hooks install` to write it") is true.
"""

from __future__ import annotations

import logging
import shlex
import sys
from pathlib import Path, PurePosixPath

from ..core import config as config_mod
from .detect import PLATFORM_WINDOWS, PLATFORM_WSL, Environment

log = logging.getLogger(__name__)

_HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"

#: `script_state` answers. `unknown` means the installed copy could not be read (a
#: stopped distro, a locked file) — never treated as outdated, the same rule as
#: `install.wsl.hook_check`: unknown is not missing.
SCRIPT_CURRENT = "current"
SCRIPT_OUTDATED = "outdated"
SCRIPT_MISSING = "missing"
SCRIPT_UNKNOWN = "unknown"


def install_hook_script(cfg: config_mod.Config, env: Environment) -> Path:
    """Copy the packaged `tv-hook.sh`/`.cmd` to `config.hook_bin_path()`, make it
    executable, and write `hook.env` alongside it.

    `TINTAVIEW_CURL` is `curl.exe` whenever the hook runs from inside WSL (`env.platform
    == "wsl"`) — it then runs in the *Windows* network namespace and reaches a daemon on
    the Windows side with no firewall rule needed — or on native Windows, where `curl.exe`
    is simply the platform's own curl. Everywhere else it's plain `curl`.
    """
    # `env.platform`, not `sys.platform`, decides the extension — and dest must be
    # built from the same decision rather than delegating to hook_bin_path() (which
    # picks by the *host's* sys.platform), or the two could disagree and this would
    # write a `.sh` script's contents into a path named `tv-hook.cmd` or vice versa.
    hook_name = "tv-hook.cmd" if env.platform == PLATFORM_WINDOWS else "tv-hook.sh"
    src = _HOOKS_DIR / hook_name
    dest = config_mod.config_dir() / "bin" / hook_name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    if sys.platform != "win32":
        dest.chmod(dest.stat().st_mode | 0o111)

    curl = "curl.exe" if env.platform in (PLATFORM_WINDOWS, PLATFORM_WSL) else "curl"
    url = f"http://{cfg.server.host}:{cfg.server.port}"
    env_path = config_mod.hook_env_path()
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(f"TINTAVIEW_URL={url}\nTINTAVIEW_CURL={curl}\n", encoding="utf-8")
    return dest


def _normalized(text: str) -> str:
    """Line endings don't count: `write_text` turns `\n` into `\r\n` on Windows, and
    the packaged `.cmd` may arrive either way, so only the content itself is compared."""
    return text.replace("\r\n", "\n")


def packaged_script(name: str) -> str:
    """The `tv-hook.sh`/`tv-hook.cmd` this TintaView version ships."""
    return (_HOOKS_DIR / name).read_text(encoding="utf-8")


def script_state(installed: Path | PurePosixPath, distro: str | None = None) -> str:
    """Whether the hook script at `installed` matches the packaged one.

    The installed copy is never rewritten by an update — every agent's config points at
    its stable path — so after an upgrade it keeps running the *old* shim until someone
    reruns `setup`/`hooks install`. A Codex-only install was never even prompted to.
    Comparing whole contents rather than a version line means a fix to the script needs
    no bump to be noticed. `distro` set: `installed` is a POSIX path inside that distro,
    read over `wsl.exe` like everything else on that side of a split install.
    """
    try:
        if distro:
            from . import wsl

            path_arg = shlex.quote(str(installed))
            out = wsl.run_in(distro, ["sh", "-c", f"[ -f {path_arg} ] && cat {path_arg}"
                                      f" || printf %s {_MISSING_MARK}"])
            if out == _MISSING_MARK:
                return SCRIPT_MISSING
            current = out
        else:
            path = Path(installed)
            if not path.exists():
                return SCRIPT_MISSING
            current = path.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - WslError, OSError, a decode error
        log.info("could not read the installed hook script %s: %r", installed, exc)
        return SCRIPT_UNKNOWN
    expected = packaged_script(PurePosixPath(str(installed)).name if distro
                               else Path(installed).name)
    return SCRIPT_CURRENT if _normalized(current) == _normalized(expected) else SCRIPT_OUTDATED


#: Printed instead of a script's contents when it doesn't exist; can never be one.
_MISSING_MARK = "tintaview-hook-script-missing"


def refresh_if_outdated(cfg: config_mod.Config, env: Environment | None = None) -> bool:
    """Startup half of the check: rewrite an installed-but-outdated hook script in place.

    Only ever the script, never `hook.env` (which holds the user's daemon URL) and never
    an agent's config: those point at the script's stable path, so replacing its contents
    is exactly what an upgrade should have done and touches nothing the user owns. A
    *missing* script is left alone — that is an install, which the hook check and the
    wizard already own. True when a file was rewritten.
    """
    from . import wsl

    check = wsl.hook_check(cfg, env)
    if check is None:
        return False
    state = script_state(check.hook_bin, check.distro)
    if state != SCRIPT_OUTDATED:
        return False
    name = PurePosixPath(str(check.hook_bin)).name if check.distro else Path(check.hook_bin).name
    script = packaged_script(name)
    if check.distro:
        wsl._write_remote_file(check.distro, str(check.hook_bin), script, executable=True)
    else:
        dest = Path(check.hook_bin)
        dest.write_text(script, encoding="utf-8")
        if sys.platform != "win32":
            dest.chmod(dest.stat().st_mode | 0o111)
    log.info("refreshed the outdated hook script %s", check.hook_bin)
    return True
