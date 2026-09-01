"""Tests for the tray's "Settings…" popup (`ui/settings_dialog.py`).

Same headless-Qt setup as `test_ui.py`: `QT_QPA_PLATFORM=offscreen` before PySide6 is
ever imported. Dialogs are built and driven directly — `_on_accept()`/
`_reset_colors()`/`_open_advanced_setup()` are called straight, never through a real
`exec()` event loop, since nothing here needs actual mouse/keyboard delivery.

`TINTAVIEW_HOME` is pinned to a tmp dir for every test (see
`isolate-config-in-adhoc-tests` in project memory — this file used to be exactly the
kind of ad-hoc script that clobbered a real `~/.tintaview/config.toml`), and
`available_engines`/`detect.detect`/`hooks.status` are stubbed so no test ever probes
real lighting hardware, depends on the host's actual platform, or reads the developer's
own `~/.claude/settings.json`. Modal message boxes are stubbed too: an unstubbed
`QMessageBox.question` in the accept path would block the run forever.
"""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6 import QtCore, QtWidgets  # noqa: E402

from tintaview.core import config as config_mod  # noqa: E402
from tintaview.core.config import ColorsConfig, Config, DeviceColorsConfig  # noqa: E402
from tintaview.install import detect  # noqa: E402
from tintaview.install import hooks as hooks_mod  # noqa: E402
from tintaview.install.detect import Environment  # noqa: E402
from tintaview.ui import settings_dialog  # noqa: E402
from tintaview.ui.settings_dialog import SettingsDialog  # noqa: E402

# --------------------------------------------------------------------------- isolation


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """No test here may touch the real ~/.tintaview, probe real hardware, read the
    host's agent config, or open a modal box that never gets answered."""
    monkeypatch.setenv("TINTAVIEW_HOME", str(tmp_path / "tvhome"))
    monkeypatch.setattr(
        settings_dialog, "available_engines",
        lambda cfg: [("chroma", False), ("ghub", False), ("openrgb", False), ("none", True)],
    )
    monkeypatch.setattr(
        detect, "detect", lambda: Environment(platform="linux", mode="native")
    )
    # Hooks look installed unless a test says otherwise, so the accept path is quiet.
    monkeypatch.setattr(hooks_mod, "status", lambda *a, **k: hooks_mod.STATUS_INSTALLED)
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", lambda *a, **k: None)
    monkeypatch.setattr(
        QtWidgets.QMessageBox, "question", lambda *a, **k: QtWidgets.QMessageBox.No
    )
    # The engine probe runs on a background thread in the real dialog; tests drive
    # `_apply_engine_probe` directly instead, so no test depends on thread timing.
    # `test_engine_probe_runs_off_the_gui_thread` re-enables it for itself.
    monkeypatch.setattr(SettingsDialog, "_start_engine_probe", lambda self: None)


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])
    return app


def make_cfg(tmp_path) -> Config:
    cfg = Config()
    cfg.path = tmp_path / "config.toml"
    return cfg


def fake_env(platform: str = "linux") -> Environment:
    return Environment(platform=platform, mode="native")


def probe_all_down() -> dict:
    return {"chroma": False, "ghub": False, "openrgb": False, "none": True}


def agent_keys(dialog: SettingsDialog) -> list[str]:
    return [
        dialog._agent_list.item(i).data(QtCore.Qt.UserRole)
        for i in range(dialog._agent_list.count())
    ]


# --------------------------------------------------------------------------- agent list


def test_agent_list_includes_stats_only_providers(qapp, tmp_path):
    cfg = make_cfg(tmp_path)
    dialog = SettingsDialog(cfg)
    # Hook-backed agents plus the stats-only providers (agents_base.STATS_ONLY_AGENTS) —
    # missing the latter here was the exact bug found and fixed in this feature.
    assert set(agent_keys(dialog)) == {"claude", "codex", "cursor", "jetbrains", "copilot"}


def test_agent_list_orders_enabled_agents_first(qapp, tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.enabled_agents = ["cursor", "claude"]
    dialog = SettingsDialog(cfg)
    keys = agent_keys(dialog)
    assert keys[:2] == ["cursor", "claude"]
    assert set(keys[2:]) == {"codex", "jetbrains", "copilot"}


def test_agent_list_check_state_matches_enabled_agents(qapp, tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.enabled_agents = ["claude"]
    dialog = SettingsDialog(cfg)
    checked = {
        dialog._agent_list.item(i).data(QtCore.Qt.UserRole)
        for i in range(dialog._agent_list.count())
        if dialog._agent_list.item(i).checkState() == QtCore.Qt.Checked
    }
    assert checked == {"claude"}


def test_agent_rows_do_not_accept_drops_on_themselves(qapp, tmp_path):
    """Reordering only ever needs drops *between* rows. Leaving `ItemIsDropEnabled` on
    lets an InternalMove drop land on a row instead, which loses the dragged item."""
    cfg = make_cfg(tmp_path)
    dialog = SettingsDialog(cfg)
    for i in range(dialog._agent_list.count()):
        flags = dialog._agent_list.item(i).flags()
        assert not (flags & QtCore.Qt.ItemIsDropEnabled)
        assert flags & QtCore.Qt.ItemIsUserCheckable  # still tickable


def test_accept_reorders_and_filters_enabled_agents(qapp, tmp_path):
    """Dragging is internal QListWidget behaviour (InternalMove) — what matters for
    correctness is that `_on_accept` reads the list's current order/check state, not
    the original `enabled_agents`. Reordering is simulated the same way a drop does:
    take an item out and reinsert it elsewhere.
    """
    cfg = make_cfg(tmp_path)
    cfg.enabled_agents = ["claude", "cursor", "codex"]
    dialog = SettingsDialog(cfg)

    # Uncheck codex, then move claude (index 0) to the end.
    for i in range(dialog._agent_list.count()):
        item = dialog._agent_list.item(i)
        if item.data(QtCore.Qt.UserRole) == "codex":
            item.setCheckState(QtCore.Qt.Unchecked)
    claude_item = dialog._agent_list.takeItem(0)
    dialog._agent_list.addItem(claude_item)

    dialog._on_accept()

    assert dialog.result_cfg.enabled_agents == ["cursor", "claude"]
    assert dialog.result() == QtWidgets.QDialog.Accepted


def test_accept_refuses_an_empty_agent_selection(qapp, tmp_path):
    """The wizard's agent step refuses an empty pick ("TintaView needs at least one
    agent to do anything"); this dialog must not be the way around that."""
    cfg = make_cfg(tmp_path)
    dialog = SettingsDialog(cfg)
    for i in range(dialog._agent_list.count()):
        dialog._agent_list.item(i).setCheckState(QtCore.Qt.Unchecked)

    dialog._on_accept()

    assert dialog.result() != QtWidgets.QDialog.Accepted
    assert not cfg.path.exists()
    assert dialog.result_cfg.enabled_agents == ["claude"]  # untouched


# --------------------------------------------------------------------------- new agents


def test_newly_enabled_agent_gets_its_adapters_confirm_detection(qapp, tmp_path):
    """Cursor declares `stall` (it has no "waiting for you" event to hook). Left at
    `AgentConfig`'s `event` default it would be enabled, its hooks would fire, and the
    light would simply never turn red — the wizard seeds this, so this must too.
    """
    cfg = make_cfg(tmp_path)
    cfg.enabled_agents = ["claude"]
    dialog = SettingsDialog(cfg)
    for i in range(dialog._agent_list.count()):
        item = dialog._agent_list.item(i)
        if item.data(QtCore.Qt.UserRole) == "cursor":
            item.setCheckState(QtCore.Qt.Checked)

    dialog._on_accept()

    assert dialog.result_cfg.agent("cursor").confirm_detection == "stall"
    saved = config_mod.load(cfg.path)
    assert saved.agent("cursor").confirm_detection == "stall"


def test_re_enabling_a_configured_agent_keeps_its_stored_settings(qapp, tmp_path):
    """An agent configured before, disabled, then re-enabled must keep what it had —
    seeding is only for a key with no stored settings at all."""
    cfg = make_cfg(tmp_path)
    cfg.enabled_agents = ["claude"]
    cfg.agent("cursor").confirm_detection = "none"  # deliberately overridden earlier
    cfg.agent("cursor").state_db = "/somewhere/state.vscdb"
    dialog = SettingsDialog(cfg)
    for i in range(dialog._agent_list.count()):
        item = dialog._agent_list.item(i)
        if item.data(QtCore.Qt.UserRole) == "cursor":
            item.setCheckState(QtCore.Qt.Checked)

    dialog._on_accept()

    assert dialog.result_cfg.agent("cursor").confirm_detection == "none"
    assert dialog.result_cfg.agent("cursor").state_db == "/somewhere/state.vscdb"


def test_newly_enabled_agent_without_hooks_offers_the_wizard(qapp, tmp_path, monkeypatch):
    """Ticking an agent here can't install hooks, so a config that looks right and can
    never work must not be the silent outcome."""
    cfg = make_cfg(tmp_path)
    cfg.enabled_agents = ["claude"]
    dialog = SettingsDialog(cfg)

    monkeypatch.setattr(hooks_mod, "status", lambda *a, **k: hooks_mod.STATUS_MISSING)
    asked = []

    def fake_question(parent, title, text, *a, **k):
        asked.append(text)
        return QtWidgets.QMessageBox.Yes

    monkeypatch.setattr(QtWidgets.QMessageBox, "question", fake_question)

    for i in range(dialog._agent_list.count()):
        item = dialog._agent_list.item(i)
        if item.data(QtCore.Qt.UserRole) == "codex":
            item.setCheckState(QtCore.Qt.Checked)
    dialog._on_accept()

    assert len(asked) == 1
    assert "Codex CLI" in asked[0]
    assert dialog.launch_wizard is True
    # The rest of the settings are saved either way — the prompt is not a rollback.
    assert dialog.result() == QtWidgets.QDialog.Accepted
    assert config_mod.load(cfg.path).enabled_agents == ["claude", "codex"]


def test_declining_the_hook_prompt_still_saves(qapp, tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    cfg.enabled_agents = ["claude"]
    dialog = SettingsDialog(cfg)
    monkeypatch.setattr(hooks_mod, "status", lambda *a, **k: hooks_mod.STATUS_MISSING)

    for i in range(dialog._agent_list.count()):
        item = dialog._agent_list.item(i)
        if item.data(QtCore.Qt.UserRole) == "codex":
            item.setCheckState(QtCore.Qt.Checked)
    dialog._on_accept()  # the fixture's question() answers No

    assert dialog.launch_wizard is False
    assert dialog.result() == QtWidgets.QDialog.Accepted


def test_stats_only_providers_never_trigger_a_hook_prompt(qapp, tmp_path, monkeypatch):
    """JetBrains/Copilot have no hook layer at all — asking about their hooks would be
    asking about something that doesn't exist."""
    cfg = make_cfg(tmp_path)
    cfg.enabled_agents = ["claude"]
    dialog = SettingsDialog(cfg)
    monkeypatch.setattr(hooks_mod, "status", lambda *a, **k: hooks_mod.STATUS_MISSING)

    for i in range(dialog._agent_list.count()):
        item = dialog._agent_list.item(i)
        if item.data(QtCore.Qt.UserRole) == "jetbrains":
            item.setCheckState(QtCore.Qt.Checked)

    assert dialog._missing_hooks(["jetbrains"]) == []
    dialog._on_accept()
    assert dialog.launch_wizard is False


# --------------------------------------------------------------------------- lighting engine


def test_engine_combo_offers_every_mode_in_the_shared_order(qapp, tmp_path):
    """Same list, same order as the console wizard — both read
    `engines.factory.ENGINE_MODES` so neither can grow or rename an engine alone."""
    from tintaview.engines.factory import ENGINE_MODES

    cfg = make_cfg(tmp_path)
    dialog = SettingsDialog(cfg)
    modes = [dialog._engine_combo.itemData(i) for i in range(dialog._engine_combo.count())]
    assert modes == list(ENGINE_MODES)


def test_engine_combo_says_checking_until_the_probe_returns(qapp, tmp_path):
    """Probing is off the GUI thread, so the real engines must not read as "available"
    in the meantime."""
    cfg = make_cfg(tmp_path)
    dialog = SettingsDialog(cfg)
    labels = {dialog._engine_combo.itemData(i): dialog._engine_combo.itemText(i)
              for i in range(dialog._engine_combo.count())}
    assert "checking" in labels["openrgb"]
    assert "checking" not in labels["auto"]  # nothing to probe
    assert "checking" not in labels["none"]


def test_engine_combo_marks_unsupported_platform(qapp, tmp_path):
    cfg = make_cfg(tmp_path)
    dialog = SettingsDialog(cfg)

    dialog._apply_engine_probe(fake_env("linux"), probe_all_down())

    labels = {dialog._engine_combo.itemData(i): dialog._engine_combo.itemText(i)
              for i in range(dialog._engine_combo.count())}
    # Chroma/G HUB are Windows-only — the same gating the wizard applies.
    assert "not supported on linux" in labels["chroma"]
    assert "not supported on linux" in labels["ghub"]
    # OpenRGB is supported everywhere; this probe says it's not running.
    assert "not running" in labels["openrgb"]
    assert "not supported" not in labels["openrgb"]


def test_engine_combo_marks_a_running_engine(qapp, tmp_path):
    cfg = make_cfg(tmp_path)
    dialog = SettingsDialog(cfg)

    probes = dict(probe_all_down(), openrgb=True)
    dialog._apply_engine_probe(fake_env("linux"), probes)

    label = dialog._engine_combo.itemText(dialog._engine_combo.findData("openrgb"))
    assert "(running)" in label


def test_engine_probe_runs_off_the_gui_thread(qapp, tmp_path, monkeypatch):
    """The real probe path: `available_engines` opens sockets and loads vendor SDKs, so
    it must not run inline — a remote/firewalled OpenRGB host would freeze the window
    for the whole connect timeout just to open Settings.
    """
    monkeypatch.undo()  # restore the real _start_engine_probe (see the isolation fixture)
    monkeypatch.setenv("TINTAVIEW_HOME", str(tmp_path / "tvhome"))
    monkeypatch.setattr(
        settings_dialog, "available_engines", lambda cfg: [("openrgb", True)]
    )
    monkeypatch.setattr(detect, "detect", lambda: fake_env("linux"))

    cfg = make_cfg(tmp_path)
    dialog = SettingsDialog(cfg)
    index = dialog._engine_combo.findData("openrgb")
    # Nothing has been probed yet at the point the window would first paint.
    assert "checking" in dialog._engine_combo.itemText(index)

    # The result arrives as a queued signal, so it only lands once the event loop runs —
    # which is exactly the property under test. Polled rather than slept on a fixed
    # delay, so a slow machine doesn't turn this into a flake.
    for _ in range(500):
        qapp.processEvents()
        if "checking" not in dialog._engine_combo.itemText(index):
            break
        time.sleep(0.01)

    assert "(running)" in dialog._engine_combo.itemText(index)


def test_engine_combo_selects_current_mode(qapp, tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.engine.mode = "openrgb"
    dialog = SettingsDialog(cfg)
    assert dialog._engine_combo.currentData() == "openrgb"


def test_engine_combo_falls_back_for_an_unknown_mode(qapp, tmp_path):
    """A config written by a newer TintaView must not leave the combo on nothing."""
    cfg = make_cfg(tmp_path)
    cfg.engine.mode = "some-future-engine"
    dialog = SettingsDialog(cfg)
    assert dialog._engine_combo.currentData() == "auto"


def test_accept_saves_chosen_engine_mode(qapp, tmp_path):
    cfg = make_cfg(tmp_path)
    dialog = SettingsDialog(cfg)
    index = dialog._engine_combo.findData("none")
    dialog._engine_combo.setCurrentIndex(index)

    dialog._on_accept()

    assert dialog.result_cfg.engine.mode == "none"
    saved = config_mod.load(cfg.path)
    assert saved.engine.mode == "none"


# --------------------------------------------------------------------------- language


def test_language_combo_lists_every_language_under_its_own_name(qapp, tmp_path):
    """Endonyms, not English names: someone opening this because the interface is in a
    language they can't read is looking for the word they *do* recognise."""
    from tintaview.i18n import LANGUAGES

    dialog = SettingsDialog(make_cfg(tmp_path))
    combo = dialog._language_combo
    listed = [(combo.itemData(i), combo.itemText(i)) for i in range(combo.count())]
    assert listed == list(LANGUAGES)
    assert ("pl", "Polski") in listed


def test_language_combo_starts_on_the_configured_language(qapp, tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.ui.language = "de"
    dialog = SettingsDialog(cfg)
    assert dialog._language_combo.currentData() == "de"


def test_language_combo_normalizes_a_hand_edited_locale(qapp, tmp_path):
    """`language = "ru_RU"` in a hand-edited config means Russian. Landing on the first
    row instead would also write English back on accept — silently changing a setting
    the user never touched."""
    cfg = make_cfg(tmp_path)
    cfg.ui.language = "ru_RU"
    dialog = SettingsDialog(cfg)
    assert dialog._language_combo.currentData() == "ru"


def test_accept_saves_the_chosen_language(qapp, tmp_path):
    cfg = make_cfg(tmp_path)
    dialog = SettingsDialog(cfg)
    dialog._language_combo.setCurrentIndex(dialog._language_combo.findData("uk"))

    dialog._on_accept()

    assert dialog.result_cfg.ui.language == "uk"
    assert config_mod.load(cfg.path).ui.language == "uk"
    # The dialog only records the choice; applying it live is the tray's job
    # (`TrayApp._apply_settings`), so nothing here switched the running catalogue.
    from tintaview.i18n import current_language

    assert current_language() == "en"


# --------------------------------------------------------------------------- colours


def test_reset_colors_restores_defaults(qapp, tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.colors.idle = "#123456"
    cfg.colors.working = "#654321"
    cfg.colors.confirm = "#ABCDEF"
    dialog = SettingsDialog(cfg)

    dialog._reset_colors()

    defaults = ColorsConfig()
    for status in ("idle", "working", "confirm"):
        assert dialog._color_buttons[status].hex_color() == getattr(defaults, status)


def test_reset_colors_restores_the_device_palette_too(qapp, tmp_path):
    """"Reset" has to mean what a fresh install looks like on the hardware as well —
    and the hardware defaults are not the icon's (see DeviceColorsConfig)."""
    cfg = make_cfg(tmp_path)
    cfg.colors.device.idle = "#111111"
    dialog = SettingsDialog(cfg)

    dialog._reset_colors()
    dialog._on_accept()

    device_defaults = DeviceColorsConfig()
    saved = config_mod.load(cfg.path)
    for status in ("idle", "working", "confirm"):
        assert saved.colors.device.__dict__[status] == getattr(device_defaults, status)


def test_a_custom_colour_drives_the_icon_and_the_devices(qapp, tmp_path):
    """The whole point of picking a colour: the tray icon *and* the LEDs use it. Writing
    only `colors.*` left the hardware on its own saturated default, so a user could set
    the tray purple and watch their keyboard stay amber.
    """
    cfg = make_cfg(tmp_path)
    dialog = SettingsDialog(cfg)
    dialog._color_buttons["idle"].set_hex_color("#00FF00")
    dialog._color_buttons["confirm"].set_hex_color("#FF00FF")

    dialog._on_accept()

    saved = config_mod.load(cfg.path)
    assert saved.colors.idle == "#00FF00"
    assert saved.colors.confirm == "#FF00FF"
    assert saved.colors.device.idle == "#00FF00"
    assert saved.colors.device.confirm == "#FF00FF"
    # And that's what the controller would actually send to the hardware.
    assert saved.colors.device_rgb("confirm") == (255, 0, 255)


def test_untouched_statuses_keep_their_device_defaults(qapp, tmp_path):
    """Editing one status must not flatten the others' LED-tuned defaults onto the icon
    palette — only a colour the user actually picked propagates."""
    cfg = make_cfg(tmp_path)
    dialog = SettingsDialog(cfg)
    dialog._color_buttons["idle"].set_hex_color("#00FF00")

    dialog._on_accept()

    saved = config_mod.load(cfg.path)
    assert saved.colors.device.working == DeviceColorsConfig().working
    assert saved.colors.device.confirm == DeviceColorsConfig().confirm


def test_untouched_inherited_device_colour_stays_inherited(qapp, tmp_path):
    """An empty `colors.device.*` means "inherit the icon colour". Opening and accepting
    the dialog without touching that status must not pin it to a literal hex."""
    cfg = make_cfg(tmp_path)
    cfg.colors.device.working = ""
    dialog = SettingsDialog(cfg)

    dialog._on_accept()

    assert config_mod.load(cfg.path).colors.device.working == ""


def test_a_malformed_colour_in_config_still_opens_the_dialog(qapp, tmp_path):
    """A hand-edited typo is cosmetic; it must not be what stops the settings window
    from opening at all — the only route the user has to fixing it."""
    cfg = make_cfg(tmp_path)
    cfg.colors.working = "not-a-colour"
    dialog = SettingsDialog(cfg)
    assert dialog._color_buttons["working"].hex_color() == ColorsConfig().working


# --------------------------------------------------------------------------- general tab


def test_other_general_fields_round_trip_on_accept(qapp, tmp_path):
    cfg = make_cfg(tmp_path)
    dialog = SettingsDialog(cfg)

    dialog._chime_check.setChecked(True)
    dialog._poll_spin.setValue(120)
    dialog._update_check.setChecked(False)

    dialog._on_accept()

    saved = config_mod.load(cfg.path)
    assert saved.ui.chime_on_confirm is True
    assert saved.stats.poll_seconds == 120
    assert saved.update.check is False


def test_update_channel_round_trips_on_accept(qapp, tmp_path):
    cfg = make_cfg(tmp_path)
    dialog = SettingsDialog(cfg)
    assert dialog._update_beta.isChecked() is False  # "stable" is the default

    dialog._update_beta.setChecked(True)
    dialog._on_accept()
    assert config_mod.load(cfg.path).update.channel == "beta"


def test_update_channel_reads_back_as_ticked(qapp, tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.update.channel = "beta"
    dialog = SettingsDialog(cfg)
    assert dialog._update_beta.isChecked() is True

    dialog._update_beta.setChecked(False)
    dialog._on_accept()
    assert config_mod.load(cfg.path).update.channel == "stable"


def test_an_unrecognised_channel_shows_as_stable_and_is_written_back_as_stable(qapp, tmp_path):
    """Same forgiving-parse policy as `engine.mode` and `ui.language`: a hand-edited
    typo must not leave the dialog showing a state it can't represent."""
    cfg = make_cfg(tmp_path)
    cfg.update.channel = "nightly"
    dialog = SettingsDialog(cfg)

    assert dialog._update_beta.isChecked() is False
    dialog._on_accept()
    assert config_mod.load(cfg.path).update.channel == "stable"


def test_the_channel_box_is_disabled_when_update_checks_are_off(qapp, tmp_path):
    """A channel with checking switched off is a live-looking control that does
    nothing."""
    cfg = make_cfg(tmp_path)
    dialog = SettingsDialog(cfg)

    dialog._update_check.setChecked(False)
    assert dialog._update_beta.isEnabled() is False
    dialog._update_check.setChecked(True)
    assert dialog._update_beta.isEnabled() is True


def test_a_low_stored_poll_interval_is_not_silently_raised(qapp, tmp_path):
    """The spin box floor is 30s, but clamping a hand-edited `poll_seconds = 5` up to 30
    on open would write that back on accept — changing a setting nobody touched."""
    cfg = make_cfg(tmp_path)
    cfg.stats.poll_seconds = 5
    dialog = SettingsDialog(cfg)

    assert dialog._poll_spin.value() == 5
    dialog._on_accept()
    assert config_mod.load(cfg.path).stats.poll_seconds == 5


# --------------------------------------------------------------------------- accept/cancel


def test_cancel_never_touches_the_config_file(qapp, tmp_path):
    cfg = make_cfg(tmp_path)
    dialog = SettingsDialog(cfg)
    dialog._color_buttons["idle"].set_hex_color("#00FF00")  # edited, never accepted

    dialog.reject()

    assert not cfg.path.exists()
    assert dialog.result() == QtWidgets.QDialog.Rejected


def test_the_dialog_never_edits_the_live_config(qapp, tmp_path):
    """`TrayApp` hands in its live `Config`; only `_apply_settings` may change it, so
    an accepted dialog must leave the caller's object alone until then."""
    cfg = make_cfg(tmp_path)
    dialog = SettingsDialog(cfg)
    dialog._color_buttons["idle"].set_hex_color("#00FF00")
    dialog._poll_spin.setValue(90)

    dialog._on_accept()

    assert cfg.colors.idle == ColorsConfig().idle
    assert cfg.stats.poll_seconds == 300
    assert dialog.result_cfg.colors.idle == "#00FF00"


def test_accept_failure_to_save_does_not_close_the_dialog(qapp, tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    dialog = SettingsDialog(cfg)

    monkeypatch.setattr(config_mod, "save", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))

    dialog._on_accept()

    assert dialog.result() != QtWidgets.QDialog.Accepted


# --------------------------------------------------------------------------- advanced setup


def test_advanced_setup_rejects_and_asks_the_caller_for_the_wizard(qapp, tmp_path):
    """The dialog doesn't launch the wizard itself — the wizard re-reads the config from
    disk, so it has to start after this dialog has closed (see `TrayApp._open_settings`).
    """
    cfg = make_cfg(tmp_path)
    dialog = SettingsDialog(cfg)

    dialog._open_advanced_setup()

    assert dialog.launch_wizard is True
    assert dialog.result() == QtWidgets.QDialog.Rejected
    assert not cfg.path.exists()  # edits discarded, not merged behind the wizard's back
