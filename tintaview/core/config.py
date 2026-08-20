"""TintaView configuration — one TOML file shared by the daemon, tray, wizard and doctor.

Deliberately dependency-free: `tomllib` reads it and a small writer below emits it, so
the config layer works on a bare WSL distro with nothing else installed. Unknown keys
in the file are preserved on load (`Config.extra`) but not round-tripped through nested
tables — the wizard is the writer of record.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

APP_NAME = "TintaView"
DEFAULT_PORT = 8777
#: Bumped to 2 when the `ghub` engine was added — see `_migrate_engine_order` for why
#: a version bump is what triggers the one-time `engine.order` fix-up.
CONFIG_VERSION = 2


# --------------------------------------------------------------------------- paths


def config_dir() -> Path:
    """Where config, logs, the hook script and caches live.

    `TINTAVIEW_HOME` overrides everything — the installer sets it for portable installs
    and the tests rely on it.
    """
    override = os.environ.get("TINTAVIEW_HOME")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    return Path.home() / ".tintaview"


def config_path() -> Path:
    return config_dir() / "config.toml"


def expand(value: str) -> Path:
    r"""Turn a configured path string into a real Path.

    Config files get hand-edited, and the documented defaults are written with a tilde
    (``home = "~/.claude"``), so ``~`` and ``$VAR`` must both expand or a perfectly
    reasonable-looking config silently points at a directory that doesn't exist. UNC
    paths (``\\wsl.localhost\Ubuntu\home\u\.claude``) pass through untouched.
    """
    return Path(os.path.expandvars(os.path.expanduser(value)))


def hook_env_path() -> Path:
    """Read by tv-hook to find the daemon; written at install time."""
    return config_dir() / "hook.env"


def hook_bin_path() -> Path:
    """The stable path every agent's hook config points at.

    Stable across updates by design: upgrading TintaView must never require rewriting
    any agent's configuration file.
    """
    name = "tv-hook.cmd" if sys.platform == "win32" else "tv-hook.sh"
    return config_dir() / "bin" / name


# --------------------------------------------------------------------------- schema


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    watchdog_timeout: int = 600  # seconds of hook silence before a forced release


@dataclass
class ChromaConfig:
    devices: list[str] = field(default_factory=lambda: ["mouse", "headset"])


@dataclass
class GHubConfig:
    """Logitech G HUB, via the LED Illumination SDK DLL G HUB itself installs.

    Unlike OpenRGB, this needs no host/port — the SDK is loaded in-process — so the only
    knobs are where to find it and which capability classes to drive.
    """

    dll_path: str = ""  # empty = auto-discover (LGHUB\sdks, LGHUB root, registry, PATH)
    # empty = %LOCALAPPDATA%\LGHUB\settings.db — read-only by ghub_env for the
    # Integrations list; never written. Override only for tests or a non-default install.
    settings_db: str = ""
    # SDK capability bitmask names (LOGI_DEVICETYPE_*), not device instances like
    # OpenRGB's device_types — the legacy LED SDK has no way to address "the mouse" vs
    # "the keyboard", only "every monochrome/RGB/per-key-RGB device". All three by
    # default, since there is no finer-grained target to narrow it to.
    device_types: list[str] = field(default_factory=lambda: ["monochrome", "rgb", "perkey"])
    restore_on_release: bool = True  # LogiLedSaveCurrentLighting/RestoreLighting round trip


@dataclass
class OpenRGBConfig:
    host: str = "127.0.0.1"
    port: int = 6742
    # Peripherals only by default: a mouse, keyboard or headset sits in your eyeline
    # while you work, which is the whole point. Motherboard, RAM, GPU and case lighting
    # is ambient decoration — driving it makes the whole room flash amber every time an
    # agent runs a tool. Set to [] to mean "every detected device", or add types by name
    # (any openrgb.utils.DeviceType member, e.g. "mousemat", "gpu").
    device_types: list[str] = field(default_factory=lambda: ["mouse", "keyboard", "headset"])
    restore_on_release: bool = True  # snapshot mode+colours on open, put them back on close
    direct_mode_only: bool = True  # skip devices without a Direct mode (no flash wear)


@dataclass
class EngineConfig:
    mode: str = "auto"  # auto | chroma | ghub | openrgb | none
    order: list[str] = field(default_factory=lambda: ["chroma", "ghub", "openrgb"])
    chroma: ChromaConfig = field(default_factory=ChromaConfig)
    ghub: GHubConfig = field(default_factory=GHubConfig)
    openrgb: OpenRGBConfig = field(default_factory=OpenRGBConfig)


@dataclass
class DeviceColorsConfig:
    """Status colours sent to the **lighting hardware**, kept separate from the icon's.

    The brand palette in :class:`ColorsConfig` is designed for a 16px tray icon on a
    screen, where a colour is judged next to its neighbours and subtle hues read fine. An
    RGB LED behind a diffuser is a different medium: whatever sits in the secondary
    channels desaturates the hue, and blue reads far brighter per unit than red. The
    brand red ``#F42D3C`` is RGB(244, 45, 60) — *more blue than green* — and on a mouse
    or headset it comes out visibly pink/purple rather than "stop and look" red.

    So hardware gets fully saturated primaries: unmistakable at a glance, across the room,
    on any diffuser. Set any of these to ``""`` to fall back to the icon's brand colour.

    ``none`` is never actually sent to a device — ``Controller.apply()`` closes the engine
    and restores whatever the device showed before TintaView touched it instead. The field
    exists only as ``ColorsConfig.rgb()``'s fallback default; don't describe it elsewhere
    as a visible "no session" colour.
    """

    idle: str = "#00FF00"  # green
    working: str = "#FFC800"  # amber — pure #FFFF00 skews green on most RGB LEDs
    confirm: str = "#FF0000"  # red
    none: str = "#0080F7"  # unused — see class docstring


@dataclass
class ColorsConfig:
    """Status colours, sampled from the TintaView mark's own gradient.

    The mark runs blue -> teal -> green -> yellow -> orange, and the three real states
    map straight onto it, so the tray icon always looks like the logo in one of its own
    hues rather than an arbitrary traffic light:

        idle    session open, waiting on you   green   #30EA2F  (the mark's green rays)
        working the agent is busy              amber   #FFBB00  (the mark's yellow rays)
        confirm the agent needs you to act     red     #FF0013  (see below)

    ``none`` (no agent session at all) is *not* a fourth visible state: `_icon_for_status`
    in `ui/tray.py` treats it the same as `idle` and just shows the static multicolour
    brand logo (which happens to include blue among its rays, alongside green/yellow, as
    plain decoration — not a status signal), and `Controller.apply()` never paints
    hardware with it at all. `none` below exists only as `rgb()`'s fallback default.

    Red is the one colour the logo doesn't contain — its warm end stops at orange
    (#FA8B07), which is too close to the working yellow to read as "stop and look" at
    16px. So confirm uses a red picked to sit in the same family as the rest of the
    palette while staying unmistakably distinct from the orange dot.

    Each hue is taken straight from the mark, then pushed to a higher saturation and
    value than the logo art uses. The gradient's own tones are tuned for a large mark on
    a white page; at 16-24px against an arbitrary taskbar they read as muted pastels, and
    the *point* of this icon is that its colour is identifiable at a glance without
    looking twice. Hue is unchanged, so it still reads as the logo.
    """

    idle: str = "#30EA2F"
    working: str = "#FFBB00"
    confirm: str = "#FF0013"
    none: str = "#0084FF"  # unused — see class docstring
    blink_ms: int = 400
    device: DeviceColorsConfig = field(default_factory=lambda: DeviceColorsConfig())

    def rgb(self, status: str) -> tuple[int, int, int]:
        """The **tray icon** colour for `status` — the brand palette above."""
        return hex_to_rgb(getattr(self, status, self.none))

    def device_rgb(self, status: str) -> tuple[int, int, int]:
        """The **hardware** colour for `status`, falling back to the icon colour."""
        override = getattr(self.device, status, "")
        return hex_to_rgb(override) if override else self.rgb(status)


@dataclass
class AgentConfig:
    home: str = ""  # agent data dir; empty = adapter default (UNC path in the WSL split)
    confirm_detection: str = "event"  # event | stall | none
    stall_seconds: float = 8.0  # only used when confirm_detection == "stall"
    state_db: str = ""  # Cursor only: path to state.vscdb; empty = auto-detect
    quota_path: str = ""  # JetBrains only: quota XML or IDE dir; empty = auto-detect newest


@dataclass
class StatsConfig:
    poll_seconds: int = 300  # usage APIs rate-limit and the windows are hours long
    enabled: bool = True


@dataclass
class UIConfig:
    chime_on_confirm: bool = False
    collapsed_agents: list[str] = field(default_factory=list)  # flyout sections user collapsed
    #: Interface language for the tray and the usage panel — a code from
    #: `tintaview.i18n.LANGUAGES`. English by default, and English again for anything
    #: unrecognised (`i18n.normalize`), so a typo in a hand-edited config still starts.
    #: Only the *interface*: whatever an agent's own API reports is quoted as it arrived.
    language: str = "en"


@dataclass
class UpdateConfig:
    check: bool = True
    channel: str = "stable"


@dataclass
class Config:
    version: int = CONFIG_VERSION
    server: ServerConfig = field(default_factory=ServerConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    colors: ColorsConfig = field(default_factory=ColorsConfig)
    stats: StatsConfig = field(default_factory=StatsConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    update: UpdateConfig = field(default_factory=UpdateConfig)
    enabled_agents: list[str] = field(default_factory=lambda: ["claude"])
    agents: dict[str, AgentConfig] = field(default_factory=dict)
    path: Path | None = None  # where this was loaded from; None for defaults

    def agent(self, key: str) -> AgentConfig:
        """Config for one agent, falling back to defaults for agents never configured."""
        return self.agents.setdefault(key, AgentConfig())

    def is_enabled(self, key: str) -> bool:
        return key in self.enabled_agents


# --------------------------------------------------------------------------- colours


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    s = value.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        raise ValueError(f"not a hex colour: {value!r}")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


# --------------------------------------------------------------------------- load/save


def _build(cls: type, data: Any):
    """Instantiate a flat dataclass from a plain dict, ignoring unknown keys.

    Unknown keys are dropped rather than raising: a config written by a newer TintaView
    must not stop an older one from starting. Nested tables (engine.chroma, agents.*) are
    assembled explicitly in :func:`load` — `from __future__ import annotations` turns
    field types into strings, so they can't be introspected reliably here.
    """
    if not isinstance(data, dict):
        return cls()
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in names})


def _migrate_engine_order(order: list[str], version: int) -> list[str]:
    """Fix up a pre-`ghub` `engine.order` on load, so upgrading doesn't silently drop
    the new engine out of `auto` mode's probe order.

    `dumps()` writes every field, so essentially every config.toml saved by a version
    before this one has `order = ["chroma", "openrgb"]` sitting in the file explicitly —
    a newer *default* order is not enough, because the explicit value always wins. Only
    runs for a file whose `version` predates `CONFIG_VERSION` and that doesn't already
    list `ghub` (an already-migrated or hand-edited file is left alone either way).
    """
    if version >= CONFIG_VERSION or "ghub" in order:
        return order
    if "chroma" in order:
        idx = order.index("chroma") + 1
        return order[:idx] + ["ghub"] + order[idx:]
    return ["ghub", *order]


def load(path: Path | None = None) -> Config:
    """Load the config, returning defaults when the file is missing or unreadable."""
    p = path or config_path()
    try:
        with open(p, "rb") as fh:
            raw = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        cfg = Config()
        cfg.path = p
        return cfg

    version = int(raw.get("version", CONFIG_VERSION))

    engine_raw = raw.get("engine", {}) or {}
    engine = _build(EngineConfig, engine_raw)
    engine.chroma = _build(ChromaConfig, engine_raw.get("chroma", {}))
    engine.ghub = _build(GHubConfig, engine_raw.get("ghub", {}))
    engine.openrgb = _build(OpenRGBConfig, engine_raw.get("openrgb", {}))

    migrated_order = _migrate_engine_order(list(engine.order), version)
    if migrated_order != engine.order:
        engine.order = migrated_order
        version = CONFIG_VERSION  # the in-memory config now matches the current schema

    agents_raw = dict(raw.get("agents", {}) or {})
    enabled = agents_raw.pop("enabled", None) or ["claude"]
    agents = {k: _build(AgentConfig, v) for k, v in agents_raw.items() if isinstance(v, dict)}

    cfg = Config(
        version=version,
        server=_build(ServerConfig, raw.get("server", {})),
        engine=engine,
        colors=_colors(raw.get("colors", {})),
        stats=_build(StatsConfig, raw.get("stats", {})),
        ui=_build(UIConfig, raw.get("ui", {})),
        update=_build(UpdateConfig, raw.get("update", {})),
        enabled_agents=list(enabled),
        agents=agents,
        path=p,
    )
    return cfg


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    s = str(value)
    # Literal strings ('…') don't process escapes, which keeps Windows and UNC paths
    # readable. Fall back to a basic string when the value contains a quote.
    if "'" not in s and "\n" not in s:
        return f"'{s}'"
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _colors(raw: Any) -> ColorsConfig:
    """Assemble `[colors]` plus its nested `[colors.device]`.

    `_build` drops nested tables (it only fills flat fields), so the device palette has
    to be attached explicitly — same pattern as `engine.chroma`. A config with no
    `[colors.device]` section gets the saturated defaults rather than inheriting the
    brand palette, which is the whole point: hardware and icon want different colours.
    """
    cfg = _build(ColorsConfig, raw)
    if isinstance(raw, dict):
        cfg.device = _build(DeviceColorsConfig, raw.get("device", {}))
    return cfg


def _table(name: str, obj: Any) -> list[str]:
    lines = [f"[{name}]"]
    for f in fields(obj):
        value = getattr(obj, f.name)
        if is_dataclass(value):
            continue  # nested tables are emitted separately, after this one
        lines.append(f"{f.name} = {_toml_value(value)}")
    lines.append("")
    return lines


def dumps(cfg: Config) -> str:
    out: list[str] = [f"# {APP_NAME} configuration — written by `tintaview setup`.", ""]
    out.append(f"version = {cfg.version}")
    out.append("")
    out += _table("server", cfg.server)
    out += _table("engine", cfg.engine)
    out += _table("engine.chroma", cfg.engine.chroma)
    out += _table("engine.ghub", cfg.engine.ghub)
    out += _table("engine.openrgb", cfg.engine.openrgb)
    out += _table("colors", cfg.colors)
    out += _table("colors.device", cfg.colors.device)
    out += _table("stats", cfg.stats)
    out += _table("ui", cfg.ui)
    out += _table("update", cfg.update)
    out.append("[agents]")
    out.append(f"enabled = {_toml_value(cfg.enabled_agents)}")
    out.append("")
    # Every *configured* agent keeps its table, not just the enabled ones. Disabling an
    # agent is a one-click, no-confirmation action in the tray's Settings popup, and
    # `[agents.X]` holds values the user cannot re-derive by hand: a WSL-split `home` is
    # a UNC path the wizard computed over `wsl.exe`, and `state_db`/`quota_path` may be
    # hand-picked. Dropping the table meant unticking an agent and re-ticking it later
    # silently reverted all of that to defaults. Enabled first, so the file still reads
    # in tray order; `_agent_table_keys` is materialised before the loop because
    # `cfg.agent()` inserts into `cfg.agents` as it goes.
    for key in _agent_table_keys(cfg):
        out += _table(f"agents.{key}", cfg.agent(key))
    return "\n".join(out).rstrip() + "\n"


def _agent_table_keys(cfg: Config) -> list[str]:
    """Agent keys needing an `[agents.X]` table: enabled ones first, then any other
    agent that already has stored settings."""
    return [*cfg.enabled_agents, *(k for k in cfg.agents if k not in cfg.enabled_agents)]


def save(cfg: Config, path: Path | None = None) -> Path:
    """Write the config atomically so a crash mid-write can't leave a truncated file."""
    p = path or cfg.path or config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(dumps(cfg), encoding="utf-8")
    os.replace(tmp, p)
    cfg.path = p
    return p
