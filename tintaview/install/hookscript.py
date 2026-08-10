"""Deploying the hook script itself (as opposed to wiring it into an agent's config).

Kept separate from :mod:`tintaview.install.hooks`, which edits the *agents'* files: this
one only writes TintaView's own two files, the hook binary and the environment file that
tells it where the daemon lives. Both the wizard and `tintaview hooks install` call it,
so the advice `doctor` prints ("run `tintaview hooks install` to write it") is true.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ..core import config as config_mod
from .detect import PLATFORM_WSL, Environment


def install_hook_script(cfg: config_mod.Config, env: Environment) -> Path:
    """Copy the packaged `tv-hook.sh`/`.cmd` to `config.hook_bin_path()`, make it
    executable, and write `hook.env` alongside it.

    `TINTAVIEW_CURL` is `curl.exe` whenever the hook runs from inside WSL (`env.platform
    == "wsl"`) — it then runs in the *Windows* network namespace and reaches a daemon on
    the Windows side with no firewall rule needed — or on native Windows, where `curl.exe`
    is simply the platform's own curl. Everywhere else it's plain `curl`.
    """
    hook_name = "tv-hook.cmd" if sys.platform == "win32" else "tv-hook.sh"
    src = Path(__file__).resolve().parent.parent / "hooks" / hook_name
    dest = config_mod.hook_bin_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    if sys.platform != "win32":
        dest.chmod(dest.stat().st_mode | 0o111)

    curl = "curl.exe" if (sys.platform == "win32" or env.platform == PLATFORM_WSL) else "curl"
    url = f"http://{cfg.server.host}:{cfg.server.port}"
    env_path = config_mod.hook_env_path()
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(f"TINTAVIEW_URL={url}\nTINTAVIEW_CURL={curl}\n", encoding="utf-8")
    return dest
