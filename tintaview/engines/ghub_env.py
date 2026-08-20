"""Measurable facts about the Logitech G HUB side of the LED SDK path.

`ghub.py` drives the lights; this module answers the questions that decide whether
those drives will ever be visible — without loading the SDK DLL or taking control:

1. **Is G HUB actually running?** Finding `sdk_legacy_led_*.dll` on disk only proves
   G HUB was installed once. A stopped `lghub_agent.exe` means every later
   `LogiLedInit` will fail (or succeed and then silently no-op), and `probe()` used to
   pay that cost just to report "available".
2. **Is Windows 11 Dynamic Lighting stealing the devices?** The registry key is the
   only way to know without opening Settings; its absence means "not applicable"
   (Windows 10 / untouched), not "off".
3. **Is TintaView's integration toggle on?** G HUB stores the answer in
   `%LOCALAPPDATA%\\LGHUB\\settings.db` as a JSON blob. The toggle field name has never
   been confirmed against a live dump in this repo, so we report `"unknown"` (or
   `"absent"` when the app entry itself is missing) rather than guessing — same bar as
   the Copilot live-quota provider.

`doctor` and the wizard import this module so they can print *measured* blockers
instead of the static seven-line checklist. Nothing here initialises the SDK.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from ..core.config import GHubConfig, expand

log = logging.getLogger(__name__)

#: Process G HUB's agent keeps alive while the UI is "running". Matching this name —
#: not `lghub.exe` — is what distinguishes "G HUB is up" from "someone left the
#: installer window open".
_GHUB_AGENT = "lghub_agent.exe"

#: Windows 11 Dynamic Lighting master switch. Path is
#: `HKCU\\Software\\Microsoft\\Lighting` — *not* under `CurrentVersion`. Confirmed
#: against Microsoft's own Settings mapping and several third-party tools; absence of
#: the key is normal on Windows 10 and on a clean Windows 11 profile that never opened
#: the Dynamic Lighting page.
_LIGHTING_REG_KEY = r"Software\Microsoft\Lighting"
_AMBIENT_LIGHTING = "AmbientLightingEnabled"
_FOREGROUND_ONLY = "ControlledByForegroundApp"

#: How G HUB registers us after `LogiLedInitWithName("TintaView")`. Matching only this
#: name (not `python.exe`) keeps us from confusing a leftover LGS-era entry with ours.
_APP_NAME = "TintaView"

#: Cap on `tasklist` so a hung Windows process list can't stall `doctor` or `probe()`.
_TASKLIST_TIMEOUT = 2.0


@dataclass(frozen=True)
class GHubEnvironment:
    """Snapshot of everything we can measure without talking to the LED SDK."""

    dll_path: Path | None
    running: bool | None  # None = could not determine (non-Windows, tasklist failed, …)
    dynamic_lighting: bool | None  # True = Windows is claiming the LEDs
    foreground_only: bool | None  # ControlledByForegroundApp; None = key absent
    integration: str  # "on" | "off" | "absent" | "unknown"


def inspect(cfg: GHubConfig) -> GHubEnvironment:
    """One-shot read of every G HUB environmental fact the doctor/wizard need."""
    from .ghub import discover_dll_path

    return GHubEnvironment(
        dll_path=discover_dll_path(cfg),
        running=ghub_running(),
        dynamic_lighting=_dynamic_lighting_on(),
        foreground_only=_foreground_app_controls(),
        integration=_integration_state(cfg),
    )


def ghub_running() -> bool | None:
    """Whether `lghub_agent.exe` is in the process list.

    Returns `None` rather than `False` off Windows or when `tasklist` itself fails —
    "could not tell" and "definitely not running" lead to different doctor messages,
    and collapsing them would send someone whose `tasklist` is blocked down the
    "start G HUB" rabbit hole.
    """
    if sys.platform != "win32":
        return None
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {_GHUB_AGENT}", "/NH"],
            capture_output=True,
            text=True,
            timeout=_TASKLIST_TIMEOUT,
            check=False,
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log.debug("ghub_env: tasklist failed: %r", e)
        return None
    # tasklist prints the image name on a hit and "INFO: No tasks are running..."
    # (localised) on a miss. Matching the executable name is locale-proof; matching the
    # INFO line is not.
    out = (completed.stdout or "") + (completed.stderr or "")
    return _GHUB_AGENT.lower() in out.lower()


def blockers(env: GHubEnvironment) -> list[str]:
    """Human-readable, action-ended lines for whatever is currently wrong.

    Empty means we measured nothing actionable — either everything looks fine, or every
    signal came back `None`/`"unknown"` and the static checklist is the fallback.
    """
    lines: list[str] = []
    if env.dll_path is None:
        lines.append(
            "the Logitech LED Illumination SDK DLL wasn't found — install Logitech G HUB "
            "from https://www.logitechg.com/en-us/innovation/g-hub.html"
        )
    if env.running is False:
        lines.append("Logitech G HUB is not running — start G HUB and leave it running")
    if env.dynamic_lighting is True:
        lines.append(
            "Windows 11 Dynamic Lighting is on — turn it off in Settings > "
            "Personalization > Dynamic lighting"
        )
    if env.integration == "off":
        lines.append(
            "TintaView lighting is disabled in G HUB — enable it under Integrations "
            "(or Games)"
        )
    elif env.integration == "absent":
        lines.append(
            "TintaView is not in G HUB's Integrations list yet — open one agent session "
            "so it registers, then enable lighting for it"
        )
    return lines


# --------------------------------------------------------------------------- Dynamic Lighting


def _read_lighting_dword(name: str) -> int | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _LIGHTING_REG_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _dynamic_lighting_on() -> bool | None:
    value = _read_lighting_dword(_AMBIENT_LIGHTING)
    if value is None:
        return None
    return value != 0


def _foreground_app_controls() -> bool | None:
    value = _read_lighting_dword(_FOREGROUND_ONLY)
    if value is None:
        return None
    return value != 0


# --------------------------------------------------------------------------- settings.db


def _default_settings_db() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "LGHUB" / "settings.db"


def _resolve_settings_db(cfg: GHubConfig) -> Path:
    override = (cfg.settings_db or "").strip()
    return expand(override) if override else _default_settings_db()


def _read_settings_json(db_path: Path) -> dict | None:
    """Pull the newest `DATA.FILE` blob out of G HUB's settings DB.

    Opened read-only and never copied — same rule as Cursor's `state.vscdb`. G HUB holds
    the DB open while running; a write or a copy would either fail or race an in-flight
    save. The toggle field for a single integration is still unconfirmed, so callers must
    treat a successful parse as "we can see the apps list", not "we know the toggle".
    """
    if sys.platform != "win32" and not db_path.exists():
        # Allow tests on Linux to point `settings_db` at a fixture; skip the Windows-only
        # auto path when the file simply isn't there.
        return None
    if not db_path.exists():
        return None
    uri = f"file:{quote(db_path.as_posix(), safe='/:')}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            cur = con.execute(
                "SELECT FILE FROM DATA ORDER BY _id DESC LIMIT 1"
            )
            row = cur.fetchone()
        finally:
            con.close()
    except sqlite3.Error as e:
        log.debug("ghub_env: could not read %s: %r", db_path, e)
        return None
    if not row or row[0] is None:
        return None
    blob = row[0]
    if isinstance(blob, memoryview):
        blob = blob.tobytes()
    if isinstance(blob, bytes):
        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError:
            log.debug("ghub_env: settings blob is not UTF-8")
            return None
    else:
        text = str(blob)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        log.debug("ghub_env: settings blob is not JSON: %r", e)
        return None
    return data if isinstance(data, dict) else None


def _iter_applications(data: dict) -> list[dict]:
    """G HUB has shipped the apps list under a few nestings; accept the known ones."""
    apps = data.get("applications")
    if isinstance(apps, dict):
        inner = apps.get("applications")
        if isinstance(inner, list):
            return [a for a in inner if isinstance(a, dict)]
    if isinstance(apps, list):
        return [a for a in apps if isinstance(a, dict)]
    return []


def _is_tintaview_app(app: dict) -> bool:
    name = str(app.get("name") or "")
    if name.lower() == _APP_NAME.lower():
        return True
    path = str(app.get("applicationPath") or app.get("applicationFolder") or "")
    return "tintaview" in path.lower()


def _integration_state(cfg: GHubConfig) -> str:
    """`"absent"` / `"unknown"` — never `"on"`/`"off"` without a confirmed field name.

    The applications list shape is public; the per-app lighting-toggle field is not.
    Guessing would mis-report a working install as broken the moment Logitech renames
    the key, so we stop at "is TintaView in the list at all".
    """
    data = _read_settings_json(_resolve_settings_db(cfg))
    if data is None:
        return "unknown"
    apps = _iter_applications(data)
    if not apps and "applications" not in data:
        # JSON parsed but has no apps section we recognise — don't claim "absent".
        return "unknown"
    for app in apps:
        if _is_tintaview_app(app):
            # Entry exists; toggle field unconfirmed → unknown, not on/off.
            return "unknown"
    return "absent"
