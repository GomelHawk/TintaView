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
    cfg.engine.ghub.settings_db = r"C:\Custom\settings.db"
    cfg.engine.ghub.device_types = ["rgb"]
    cfg.engine.ghub.restore_on_release = False

    config_mod.save(cfg, path)
    loaded = config_mod.load(path)

    assert loaded.engine.ghub.dll_path == r"C:\Custom\LogitechLed.dll"
    assert loaded.engine.ghub.settings_db == r"C:\Custom\settings.db"
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


def test_disabling_an_agent_keeps_its_stored_settings(tmp_path):
    """`dumps()` must write a table for every *configured* agent, not just the enabled
    ones. Unticking an agent in the tray's Settings popup is one click with no diff, and
    `[agents.X]` holds values the user can't re-derive by hand — a WSL-split `home` is a
    UNC path the wizard computed over `wsl.exe`.
    """
    path = tmp_path / "config.toml"
    cfg = Config()
    cfg.path = path
    cfg.enabled_agents = ["claude", "cursor"]
    cfg.agent("cursor").state_db = "/somewhere/state.vscdb"
    cfg.agent("claude").home = r"\\wsl.localhost\Ubuntu\home\u\.claude"

    cfg.enabled_agents = ["claude"]  # the user unticks Cursor
    config_mod.save(cfg)

    reloaded = config_mod.load(path)
    assert reloaded.enabled_agents == ["claude"]
    assert reloaded.agent("cursor").state_db == "/somewhere/state.vscdb"
    assert reloaded.agent("claude").home == r"\\wsl.localhost\Ubuntu\home\u\.claude"


def test_enabled_agents_tables_come_first(tmp_path):
    """Cosmetic but deliberate: the file should still read in tray order."""
    cfg = Config()
    cfg.enabled_agents = ["codex"]
    cfg.agent("cursor").state_db = "/x"
    cfg.enabled_agents = ["codex"]

    tables = [line for line in config_mod.dumps(cfg).splitlines() if line.startswith("[agents.")]
    assert tables == ["[agents.codex]", "[agents.cursor]"]


def test_dumps_does_not_invent_tables_for_unconfigured_agents(tmp_path):
    cfg = Config()  # enabled_agents defaults to ["claude"], cfg.agents is empty
    tables = [line for line in config_mod.dumps(cfg).splitlines() if line.startswith("[agents.")]
    assert tables == ["[agents.claude]"]
