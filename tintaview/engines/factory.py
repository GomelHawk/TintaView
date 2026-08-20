"""Picks a lighting engine from config. The only place that knows about all three.

Everything else (server, tray, wizard) goes through ``make_engine``/``available_engines``
and never imports ``ChromaEngine``/``OpenRGBEngine``/``NullEngine`` directly — that keeps
the OpenRGB optional dependency out of every module except this one and ``openrgb.py``.
"""

from __future__ import annotations

import logging

from ..core.config import Config
from .base import LightingEngine
from .chroma import ChromaEngine
from .ghub import GHubEngine
from .null import NullEngine
from .openrgb import OpenRGBEngine

log = logging.getLogger(__name__)

#: Every engine the wizard/`` auto`` mode knows about, in the order `available_engines`
#: reports them — independent of `cfg.engine.order`, so the wizard always shows the full
#: picture (detected/not-running/unsupported) regardless of what's configured.
_KNOWN_ENGINES = ("chroma", "ghub", "openrgb", "none")

#: The pickable values of `engine.mode`, in the order both config UIs offer them.
#: "auto" first and default — see `ui.wizard._step_engine` for why pinning one engine is
#: the riskier answer.
ENGINE_MODES: tuple[str, ...] = ("auto", "chroma", "ghub", "openrgb", "none")

#: Human label per mode, shared by the console wizard (`ui.wizard`) and the tray's
#: settings dialog (`ui.settings_dialog`) so an engine is never named one thing in one
#: config UI and something else in the other. Each UI may append its own detail
#: ("running now", "not supported on linux") after the label.
ENGINE_DISPLAY: dict[str, str] = {
    "auto": "Detect automatically (recommended)",
    "chroma": "Razer Chroma",
    "ghub": "Logitech G HUB",
    "openrgb": "OpenRGB",
    "none": "Status only — no lights",
}

#: Which `install.detect.Environment.supports_*` flag gates each engine. Chroma and
#: G HUB are Windows-only SDKs, so on WSL/Linux/macOS "not detected" would be misleading:
#: no amount of starting Synapse or G HUB on *this* machine makes them answer. Duck-typed
#: on `env` so this module needn't import `install.detect`.
ENGINE_SUPPORTED = {
    "chroma": lambda env: env.supports_chroma,
    "ghub": lambda env: env.supports_ghub,
    "openrgb": lambda env: env.supports_openrgb,
}


def engine_supported(name: str, env) -> bool:
    """Can `name` ever work on this platform? True for ungated modes ("auto", "none")."""
    check = ENGINE_SUPPORTED.get(name)
    if check is None:
        return True
    try:
        return bool(check(env))
    except AttributeError:  # an Environment from an older/newer build
        return True


def _build(name: str, cfg: Config) -> LightingEngine | None:
    """One engine instance for `name`, or None for an unknown name (skipped, not fatal —
    a config written by a newer TintaView must not crash an older one)."""
    if name == "chroma":
        return ChromaEngine(cfg.engine.chroma)
    if name == "ghub":
        return GHubEngine(cfg.engine.ghub)
    if name == "openrgb":
        return OpenRGBEngine(cfg.engine.openrgb)
    if name == "none":
        return NullEngine()
    log.info("engine factory: unknown engine name %r, skipping", name)
    return None


def _safe_probe(engine: LightingEngine) -> bool:
    """probe() must never be allowed to take the daemon down — a buggy or half-installed
    vendor SDK is exactly the case this whole module exists to degrade gracefully from."""
    try:
        return engine.probe()
    except Exception as e:
        log.info("%s probe() raised, treating as unavailable: %r", engine.display_name, e)
        return False


def make_engine(cfg: Config) -> LightingEngine:
    """Build the engine to actually drive, per `cfg.engine.mode`.

    "chroma" / "openrgb" / "none" force that engine outright — the caller asked for it
    explicitly, so we hand it back even if probe() would fail (open() will report the
    real failure, and status tracking still works either way). "auto" probes
    `cfg.engine.order` in turn and returns the first that succeeds, falling back to
    NullEngine so there is always something to return.
    """
    mode = cfg.engine.mode
    if mode == "chroma":
        return ChromaEngine(cfg.engine.chroma)
    if mode == "ghub":
        return GHubEngine(cfg.engine.ghub)
    if mode == "openrgb":
        return OpenRGBEngine(cfg.engine.openrgb)
    if mode == "none":
        return NullEngine()

    for name in cfg.engine.order:
        engine = _build(name, cfg)
        if engine is not None and _safe_probe(engine):
            return engine
    return NullEngine()


def available_engines(cfg: Config) -> list[tuple[str, bool]]:
    """`[(engine name, probe() result), ...]` for every known engine, for the wizard.

    Must never raise — a missing/broken vendor SDK is reported as `False`, not an
    exception, so one bad probe can't blank out the whole wizard page.
    """
    results: list[tuple[str, bool]] = []
    for name in _KNOWN_ENGINES:
        engine = _build(name, cfg)
        if engine is None:
            continue
        results.append((name, _safe_probe(engine)))
    return results
