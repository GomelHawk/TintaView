"""The Windows-side half of the WSL split install (AGENTS.md, "WSL split install").

When the daemon and tray run on Windows but the agents (and therefore the hooks) live
inside a WSL distro, this module is what lets a Windows-native wizard reach into that
distro without anything needing to be installed there first:

- content is written by piping it over ``wsl.exe``'s stdin, never by assuming a shared
  filesystem path (there usually isn't one worth relying on);
- when TintaView *is* already installed inside the distro, its own ``tintaview hooks
  install`` is preferred, because it understands the agents' file formats without this
  module having to duplicate that;
- otherwise, hook installs are planned and applied from the Windows side against the
  distro's config files through their ``\\\\wsl.localhost\\<distro>\\...`` UNC path —
  visible to Windows even when nothing inside the distro is running — reusing
  :mod:`tintaview.install.hooks` exactly as a native install would.

Every public function here degrades: a WSL that isn't running, a distro that's stopped,
or a `wsl.exe` that doesn't exist are normal, expected conditions on a machine that
doesn't have WSL set up (yet), not bugs — callers get a :class:`WslError` with a message
safe to show as-is, never a bare `subprocess`/`OSError` traceback.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

from . import codex_flag, detect
from . import hooks as hooks_mod

#: One-shot config commands; generous enough for a distro that's cold-starting.
_TIMEOUT = 20.0


class WslError(RuntimeError):
    """A WSL operation didn't complete — missing `wsl.exe`, a distro that isn't running,
    a timeout, or the remote command itself failing. The message is written to be shown
    to a non-technical user verbatim; callers should catch this instead of letting a
    `subprocess`/`OSError` traceback surface.
    """


# --------------------------------------------------------------------------- low level


def list_distros() -> list[str]:
    """Distro names visible from Windows. Empty (not an error) when WSL isn't set up."""
    return detect.wsl_distros()


def _wsl_exe() -> str:
    exe = shutil.which("wsl.exe") or shutil.which("wsl")
    if not exe:
        raise WslError(
            "WSL doesn't seem to be available on this machine (wsl.exe was not found)."
        )
    return exe


def run_in(
    distro: str, argv: list[str], input: str | None = None, timeout: float = _TIMEOUT
) -> str:
    """Run `argv` inside `distro` via `wsl.exe -d <distro> -- …`, returning decoded stdout.

    `input`, when given, is piped to the command's stdin (UTF-8) rather than written to
    a file first — how tv-hook.sh and hook.env get installed without a shared filesystem.
    Anything short of a clean exit becomes a :class:`WslError` with an actionable message.
    """
    exe = _wsl_exe()
    cmd = [exe, "-d", distro, "--", *argv]
    try:
        result = subprocess.run(
            cmd,
            input=input.encode("utf-8") if input is not None else None,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise WslError(
            f"{distro} didn't respond in time. It may be starting up — try again in a "
            "moment, or open a terminal in it once to make sure it's running."
        ) from exc
    except OSError as exc:
        raise WslError(f"Could not run a command inside {distro}: {exc}") from exc

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace").strip()
        detail = f": {stderr}" if stderr else f" (exit code {result.returncode})"
        raise WslError(f"That didn't work inside {distro}{detail}")
    return result.stdout.decode("utf-8", "replace")


def distro_home(distro: str) -> str:
    """The Linux `$HOME` inside `distro`, e.g. `/home/dmitry`."""
    out = run_in(distro, ["sh", "-c", 'printf %s "$HOME"']).strip()
    if not out:
        raise WslError(f"Could not work out the home directory inside {distro}.")
    return out


def _write_remote_file(
    distro: str, remote_path: str, content: str, *, executable: bool = False
) -> None:
    """Write `content` to `remote_path` inside `distro`, over stdin — no local mount
    assumed. `mkdir -p` first so a fresh `~/.tintaview/bin` doesn't need a separate call.
    """
    remote_dir = str(PurePosixPath(remote_path).parent)
    script = f"mkdir -p {shlex.quote(remote_dir)} && cat > {shlex.quote(remote_path)}"
    if executable:
        script += f" && chmod +x {shlex.quote(remote_path)}"
    run_in(distro, ["sh", "-c", script], input=content)


_HOOK_SCRIPT = Path(__file__).resolve().parent.parent / "hooks" / "tv-hook.sh"

#: Where a split install's hook files live *inside the distro*, relative to its `$HOME`.
#: `doctor` checks these same paths, so they are named once here rather than spelled out
#: again on the checking side — a split install that reported its own hook script
#: missing (because `doctor` looked at the Windows `config_dir()` instead) is exactly
#: what a second copy of these strings buys you.
REMOTE_HOOK_BIN_REL = ".tintaview/bin/tv-hook.sh"
REMOTE_HOOK_ENV_REL = ".tintaview/hook.env"


def remote_hook_bin(home: str) -> PurePosixPath:
    """The distro-side `tv-hook.sh` path for a distro whose `$HOME` is `home`.

    A `PurePosixPath`, not a `Path`: it names a file inside the distro, not on whatever
    filesystem this process is running on, and must keep forward slashes even when this
    code runs natively on Windows.
    """
    return PurePosixPath(f"{home.rstrip('/')}/{REMOTE_HOOK_BIN_REL}")


def remote_hook_env(home: str) -> PurePosixPath:
    """The distro-side `hook.env` path — see `remote_hook_bin`."""
    return PurePosixPath(f"{home.rstrip('/')}/{REMOTE_HOOK_ENV_REL}")


def install_hook(distro: str, url: str) -> Path:
    """Install `tv-hook.sh` + `hook.env` inside `distro`'s `~/.tintaview/`.

    `TINTAVIEW_CURL` is always `curl.exe` here: called from inside WSL it runs in the
    *Windows* network namespace, so it reaches the daemon on the Windows side (`url`)
    with no firewall rule and no mirrored-networking requirement — the trick this whole
    split relies on. Returns the (POSIX, distro-side) path to the installed script.
    """
    home = distro_home(distro)
    hook_path = remote_hook_bin(home)
    env_path = remote_hook_env(home)

    script = _HOOK_SCRIPT.read_text(encoding="utf-8")
    _write_remote_file(distro, str(hook_path), script, executable=True)
    _write_remote_file(distro, str(env_path), f"TINTAVIEW_URL={url}\nTINTAVIEW_CURL=curl.exe\n")

    return hook_path


def agent_homes_unc(distro: str) -> dict[str, str]:
    """UNC paths to each known agent's home dir inside `distro`.

    Written into the Windows-side config (`[agents.<key>] home = ...`) so the tray can
    read transcripts/stats through `\\\\wsl.localhost\\...` without anything running
    inside the distro. Best-effort: `{}` (not an exception) if the distro can't be reached.
    """
    from ..agents import base as agents_base

    try:
        home = distro_home(distro)
    except WslError:
        return {}

    out: dict[str, str] = {}
    for adapter in agents_base.all_agents():
        remote_posix = f"{home.rstrip('/')}/{adapter.default_home().name}"
        out[adapter.key] = detect.wsl_path_to_unc(distro, remote_posix)
    return out


# --------------------------------------------------------------------------- hook install


class RemotePathAdapter:
    """Wraps a real agent adapter so `hooks_config_path()` returns a pre-computed path.

    Lets `tintaview.install.hooks` plan/apply a hook install — and `install.doctor`
    *check* one — against a config file that isn't where this machine's `Path.home()`
    says it is: a WSL distro's, reached over its `\\\\wsl.localhost\\...` UNC path, or
    any home overridden by `agents.<key>.home`. Everything else (key, bindings,
    render_hooks, ...) passes straight through to the wrapped adapter.
    """

    def __init__(self, inner: Any, path: Path) -> None:
        self._inner = inner
        self._path = path

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def hooks_config_path(self, scope: str = "user", project_dir: Path | None = None) -> Path:
        del scope, project_dir
        return self._path


def agent_config_unc_path(distro: str, home: str, adapter: Any) -> Path:
    """The `\\\\wsl.localhost\\...` path to `adapter`'s hooks config file inside `distro`.

    Derived from the adapter's own `default_home()` / `hooks_config_path()` rather than
    hard-coding each agent's dotdir + filename a second time here: the relative pieces
    (".claude", "settings.json", ...) are computed the same way regardless of what
    `Path.home()` happens to resolve to on the machine actually running this code.
    """
    local_home = adapter.default_home()
    rel_file = adapter.hooks_config_path("user").relative_to(local_home)
    remote_posix = f"{home.rstrip('/')}/{local_home.name}/{rel_file.as_posix()}"
    return Path(detect.wsl_path_to_unc(distro, remote_posix))


def _tintaview_available(distro: str) -> bool:
    try:
        run_in(distro, ["sh", "-c", "command -v tintaview"])
        return True
    except WslError:
        return False


def install_agent_hooks(distro: str, agent_keys: list[str], assume_yes: bool) -> dict:
    """Wire up hooks for `agent_keys` inside `distro`. Returns a summary dict the wizard
    renders: `{"distro", "route", "notes": [...], "plans": {key: HookPlan | str}}`.

    Route "tintaview" (TintaView already installed inside the distro): its own `tintaview
    hooks install` does the merge and is applied immediately with `-y` — there is no way
    to show a diff and wait for a per-agent click across the Windows/WSL boundary inside
    one `wsl.exe` call, so this route trades that off against needing nothing extra
    computed from the Windows side. `plans[key]` is the command's own output text.

    Route "unc" (nothing installed in the distro, preferred — it needs nothing installed
    there): plans are computed here against the distro's config files through their UNC
    path and left for the caller to show + apply with `tintaview.install.hooks`, exactly
    like a native install. `plans[key]` is a real `HookPlan`.

    `assume_yes` only affects the "tintaview" route (it decides *there*, not here); the
    "unc" route always returns plans for the wizard to confirm.
    """
    from ..agents import base as agents_base

    result: dict = {"distro": distro, "route": None, "notes": [], "plans": {}}

    try:
        home = distro_home(distro)
    except WslError as exc:
        result["route"] = "failed"
        result["notes"].append(str(exc))
        return result

    if _tintaview_available(distro):
        result["route"] = "tintaview"
        for key in agent_keys:
            try:
                out = run_in(distro, ["tintaview", "hooks", "install", "--agent", key, "-y"])
                result["plans"][key] = out.strip() or "already up to date"
            except WslError as exc:
                result["plans"][key] = f"failed: {exc}"
        result["notes"].append(
            "TintaView is already installed inside the distro, so its hooks were merged "
            "and applied there directly — there's no way to show a diff and wait for a "
            "click across the Windows/WSL boundary in one step. Run `tintaview hooks "
            "status` inside the distro any time to double-check what was written."
        )
        del assume_yes  # decided remotely by the `-y` above, not here
        return result

    result["route"] = "unc"
    hook_bin = remote_hook_bin(home)
    for key in agent_keys:
        adapter = agents_base.get(key)
        if adapter is None:
            continue
        try:
            unc_path = agent_config_unc_path(distro, home, adapter)
            plan = hooks_mod.plan_install(RemotePathAdapter(adapter, unc_path), hook_bin)
        except (ValueError, OSError) as exc:
            result["plans"][key] = f"failed: {exc}"
            continue
        result["plans"][key] = plan
    result["notes"].append(
        f"Nothing needed to be installed inside {distro} for this — the config files "
        "were reached directly over its \\\\wsl.localhost\\... path."
    )
    return result


def codex_flag_plan_unc(distro: str, home: str) -> codex_flag.FlagPlan:
    """Codex's feature-flag plan (see `install.codex_flag`) against the distro's
    `~/.codex/config.toml`, reached the same UNC way as `agent_config_unc_path`.

    Codex's version is read from inside the distro (`codex --version` on the Windows
    side would report the wrong binary, or none at all) — best-effort, `None` when Codex
    isn't on PATH there, which `codex_flag.plan` already treats as "write the safe
    legacy flag" rather than failing.
    """
    try:
        version = run_in(distro, ["codex", "--version"]).strip() or None
    except WslError:
        version = None
    remote_posix = f"{home.rstrip('/')}/.codex/config.toml"
    unc_path = Path(detect.wsl_path_to_unc(distro, remote_posix))
    return codex_flag.plan(unc_path, version)
