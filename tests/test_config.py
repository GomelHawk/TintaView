"""Tests for tintaview.core.config: the load/dumps round trip and the `engine.order`
migration that runs when a pre-`ghub` config.toml is loaded.

Everything writes into `tmp_path` — none of this may touch a real `~/.tintaview` or
`%LOCALAPPDATA%\\TintaView`.
"""

from __future__ import annotations

from tintaview.core import config as config_mod
from tintaview.core.config import CONFIG_VERSION, Config, EngineConfig


def _write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_ghub_config_round_trips_through_dumps_and_load(tmp_path):
    path = tmp_path / "config.toml"
    cfg = Config(engine=EngineConfig())
    cfg.engine.ghub.dll_path = r"C:\Custom\LogitechLed.dll"
    cfg.engine.ghub.device_types = ["rgb"]
    cfg.engine.ghub.restore_on_release = False

    config_mod.save(cfg, path)
    loaded = config_mod.load(path)

    assert loaded.engine.ghub.dll_path == r"C:\Custom\LogitechLed.dll"
    assert loaded.engine.ghub.device_types == ["rgb"]
    assert loaded.engine.ghub.restore_on_release is False
    assert "[engine.ghub]" in path.read_text(encoding="utf-8")


def test_v1_config_gains_ghub_in_engine_order_on_load(tmp_path):
    # A config.toml as a pre-ghub TintaView would actually have written one: `dumps()`
    # always emits every field explicitly, so `order` here is not "the old default", it
    # is exactly what sits in every real installed config today.
    path = tmp_path / "config.toml"
    _write(path, """
version = 1

[engine]
mode = 'auto'
order = ['chroma', 'openrgb']
""")

    cfg = config_mod.load(path)

    assert cfg.engine.order == ["chroma", "ghub", "openrgb"]
    assert cfg.version == CONFIG_VERSION


def test_migration_inserts_ghub_first_when_chroma_is_absent(tmp_path):
    path = tmp_path / "config.toml"
    _write(path, """
version = 1

[engine]
mode = 'openrgb'
order = ['openrgb']
""")

    cfg = config_mod.load(path)
    assert cfg.engine.order == ["ghub", "openrgb"]


def test_already_migrated_config_is_left_alone(tmp_path):
    """A v2 file (or a v1 file someone hand-edited to remove ghub on purpose) must not
    have ghub silently reinserted every time it's loaded — only a config that predates
    CONFIG_VERSION gets the one-time fix-up."""
    path = tmp_path / "config.toml"
    _write(path, f"""
version = {CONFIG_VERSION}

[engine]
mode = 'auto'
order = ['chroma', 'openrgb']
""")

    cfg = config_mod.load(path)
    assert cfg.engine.order == ["chroma", "openrgb"]


def test_fresh_default_config_already_includes_ghub():
    cfg = Config()
    assert cfg.engine.order == ["chroma", "ghub", "openrgb"]
