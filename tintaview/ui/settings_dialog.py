"""A native settings window: the GUI counterpart to the console `setup` wizard
(`tintaview.ui.wizard`), covering the knobs a user wants to reach often — enabled
agents (drag-reorderable — that list order is also the tray tooltip/flyout order),
interface language, lighting engine, status colours, chime, usage/update polling —
without spawning a console.

Deliberately **not** a replacement for the wizard: hook installation, autostart and
per-engine device wiring (OpenRGB host/port, G HUB dll_path, WSL split) only ever
happen with the user watching a diff/confirmation in a terminal (see wizard.py's own
docstring), and doing that inside a modal Qt dialog would be a worse version of the
same flow. This dialog's "Open Full Setup Wizard (Terminal)…" button hands off to that
console wizard for exactly those cases instead of reimplementing them — and because
ticking an agent here cannot install its hooks, it *checks* whether they're installed
and offers the same hand-off rather than leaving a config that looks right and never
lights up.

Edits a working copy of `Config` and only writes it back (`core.config.save`) when the
user accepts — Cancel must leave the on-disk config and the running tray untouched.
The caller (`TrayApp._open_settings`) then pushes the accepted copy into the live
config; this dialog never touches the tray, and never launches the wizard itself — it
only raises `launch_wizard`, so the sequencing stays with the caller.

IMPORTANT: this dialog and the console wizard overlap on purpose — see AGENTS.md's
"Two config UIs — touch both". Any `Config` field either one exposes, adding one,
renaming one, or changing its choices, needs the same change made in `ui/wizard.py`
too, or the two surfaces silently drift apart. The engine names/labels/gating
(`engines.factory.ENGINE_MODES`/`ENGINE_DISPLAY`/`engine_supported`) and the
agent/provider names (`agents.base.STATS_ONLY_AGENTS`/`display_name`) are shared
tables precisely so those parts *cannot* drift; everything else is on you.
"""

from __future__ import annotations

import copy
import logging
import threading

from PySide6 import QtCore, QtWidgets

from ..agents import base as agents_base
from ..core import config as config_mod
from ..core.config import ColorsConfig, Config, DeviceColorsConfig
from ..engines.factory import ENGINE_DISPLAY, ENGINE_MODES, available_engines, engine_supported
from ..i18n import LANGUAGES, t
from ..i18n import normalize as normalize_language
from ..install import detect

log = logging.getLogger(__name__)


#: Width the dialog is laid out for. Wider than it needs to be in English on purpose:
#: German and Polish run 30-40% longer than the same English string, and `QCheckBox` and
#: `QLabel` in a form's field column clip rather than wrap, so a width chosen against
#: English produced "Ton, wenn ein Agent eine Bestätig" with the rest cut off.
_MIN_WIDTH = 560

#: Width the wrapped hints below are asked to size themselves for. A `QLabel` with
#: `wordWrap` reports a one-line `sizeHint`, so a layout gives it one line's height and
#: the second line lands on top of whatever is underneath — the fix is an explicit
#: minimum height computed from the width it will actually get (`heightForWidth`).
_HINT_WIDTH = _MIN_WIDTH - 40


def _hint(text: str) -> QtWidgets.QLabel:
    """A small, greyed explanatory line, spanning the whole form width."""
    label = QtWidgets.QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet("color: palette(mid); font-size: 11px;")
    label.setMinimumHeight(label.heightForWidth(_HINT_WIDTH))
    return label


def _engine_label(mode: str) -> str:
    """Translated name for an engine mode.

    `ENGINE_DISPLAY` stays the shared table of *which* engines exist and in what order
    (see AGENTS.md's "Two config UIs"); this only translates the label, and falls back to
    that table's English so an engine added to `engines.factory` without a catalogue key
    still shows a real name here instead of a bare key.
    """
    fallback = ENGINE_DISPLAY.get(mode, mode)
    key = f"engine.mode.{mode}"
    label = t(key)
    return fallback if label == key else label

#: The three real statuses, in the order they escalate. `none` is deliberately absent:
#: it is not a fourth visible state (see `ColorsConfig`'s docstring) and offering a
#: colour picker for it would invite someone to set a colour nothing ever shows.
_STATUSES: tuple[str, ...] = ("idle", "working", "confirm")

#: Two spaces before the availability detail, so it reads as an aside on the engine's
#: name rather than part of it. The detail itself is translated (`settings.engine.*`).
_DETAIL_GAP = "  "


def _safe_hex(value: str, fallback: str) -> str:
    """`value` if it parses as a hex colour, else `fallback`.

    config.toml gets hand-edited, and a swatch is cosmetic: a typo'd colour must not be
    what stops the whole settings window from opening (which is what an unguarded
    `hex_to_rgb` did — the dialog failed to construct and the user's only route to
    fixing it was the file they'd just broken).
    """
    try:
        config_mod.hex_to_rgb(value)
    except (ValueError, AttributeError, TypeError):
        return fallback
    return value


class _ColorButton(QtWidgets.QPushButton):
    """A button that shows its current colour as a swatch and edits it via QColorDialog.

    Tracks whether the user actually changed it (`edited`), which the device-colour
    column needs: an empty `colors.device.*` means "inherit the icon colour", so the
    swatch has to *show* the inherited colour without silently pinning it to a literal
    hex the moment the dialog is accepted.
    """

    def __init__(self, hex_color: str, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedWidth(90)
        self._hex = _safe_hex(hex_color, "#000000")
        self.edited = False
        self._apply_swatch()
        self.clicked.connect(self._pick)

    def _apply_swatch(self) -> None:
        self.setText(self._hex.upper())
        self.setStyleSheet(
            f"background-color: {self._hex}; color: {'#000' if self._is_light() else '#fff'};"
        )

    def _is_light(self) -> bool:
        r, g, b = config_mod.hex_to_rgb(self._hex)  # already validated by _safe_hex
        return (0.299 * r + 0.587 * g + 0.114 * b) > 150

    def _pick(self) -> None:
        from PySide6 import QtGui

        color = QtWidgets.QColorDialog.getColor(
            QtGui.QColor(self._hex), self, t("settings.colors.picker")
        )
        if color.isValid():
            self.set_hex_color(color.name())

    def hex_color(self) -> str:
        return self._hex

    def set_hex_color(self, hex_color: str) -> None:
        self._hex = _safe_hex(hex_color, self._hex)
        self.edited = True
        self._apply_swatch()


class SettingsDialog(QtWidgets.QDialog):
    """Modal settings window. Construct with the tray's live `Config`; on accept the
    caller should re-read `dialog.result_cfg` and apply it (colours, timers, ...) —
    this dialog only fills in and saves the config, it doesn't touch the tray itself.

    `dialog.launch_wizard` is True when the user asked for the console wizard (the
    button, or the "hooks aren't installed" prompt); the caller runs it *after* the
    dialog has closed, so the wizard never re-reads a config the caller is still
    halfway through applying.
    """

    #: (Environment, {engine name: probe ok}) from the background probe below.
    engine_probe_done = QtCore.Signal(object, object)

    def __init__(self, cfg: Config, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("settings.title"))
        self._cfg = cfg
        #: Edited in place, then either saved (accept) or discarded (reject) — never
        #: the caller's live `cfg` directly, so a cancelled dialog changes nothing.
        self.result_cfg: Config = copy.deepcopy(cfg)
        #: Set when the user wants the console wizard; acted on by the caller.
        self.launch_wizard = False

        tabs = QtWidgets.QTabWidget(self)
        tabs.addTab(self._build_general_tab(), t("settings.tab.general"))
        tabs.addTab(self._build_lighting_tab(), t("settings.tab.lighting"))

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        advanced = QtWidgets.QPushButton(t("settings.wizard"))
        advanced.setToolTip(t("settings.wizard.tooltip"))
        advanced.clicked.connect(self._open_advanced_setup)
        buttons.addButton(advanced, QtWidgets.QDialogButtonBox.ResetRole)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(buttons)
        self.setMinimumWidth(_MIN_WIDTH)

        self.engine_probe_done.connect(self._apply_engine_probe)
        self._start_engine_probe()

    # --- General tab --------------------------------------------------------

    def _build_general_tab(self) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(widget)

        # The full provider list: hook-capable agents (Claude Code, Codex CLI, Cursor)
        # plus the stats-only integrations (JetBrains, Copilot) that have no
        # AgentAdapter but are still valid `enabled_agents` entries — see
        # `agents_base.STATS_ONLY_AGENTS`. Missing the latter here would make this
        # list quietly shorter than the wizard's own agent step.
        all_keys = [a.key for a in agents_base.all_agents()]
        all_keys += [key for key, _ in agents_base.STATS_ONLY_AGENTS]
        # Enabled agents first, in their configured order (that order also drives the
        # tray tooltip and flyout — see TrayApp._tooltip_for); any agent never enabled
        # is appended after, in registry order, so it's still reachable to turn on.
        ordered_keys = [k for k in self._cfg.enabled_agents if k in all_keys]
        ordered_keys += [k for k in all_keys if k not in ordered_keys]

        self._agent_list = QtWidgets.QListWidget()
        self._agent_list.setDragDropMode(QtWidgets.QAbstractItemView.InternalMove)
        self._agent_list.setDefaultDropAction(QtCore.Qt.MoveAction)
        self._agent_list.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        for key in ordered_keys:
            item = QtWidgets.QListWidgetItem(agents_base.display_name(key))
            # ItemIsDropEnabled is on by default and lets an InternalMove drop land *on*
            # a row instead of between two, which is how a dragged row goes missing
            # instead of moving. Reordering only ever needs drops between rows.
            flags = (item.flags() | QtCore.Qt.ItemIsUserCheckable) & ~QtCore.Qt.ItemIsDropEnabled
            item.setFlags(flags)
            item.setCheckState(QtCore.Qt.Checked if self._cfg.is_enabled(key) else QtCore.Qt.Unchecked)
            item.setData(QtCore.Qt.UserRole, key)
            self._agent_list.addItem(item)
        row_height = self._agent_list.sizeHintForRow(0) if self._agent_list.count() else 22
        self._agent_list.setFixedHeight(min(160, row_height * max(1, len(ordered_keys)) + 6))
        form.addRow(t("settings.agents"), self._agent_list)
        form.addRow(_hint(t("settings.agents.hint")))

        self._language_combo = self._build_language_combo()
        form.addRow(t("settings.language"), self._language_combo)
        form.addRow(_hint(t("settings.language.hint")))

        self._chime_check = QtWidgets.QCheckBox(t("settings.chime"))
        self._chime_check.setChecked(self._cfg.ui.chime_on_confirm)
        # Spanning the whole form, not sitting in the field column beside an empty label:
        # a QCheckBox clips its own text rather than wrapping or eliding it, and the field
        # column is only as wide as the longest *label* leaves it — which cut the German
        # wording short by a word.
        form.addRow(self._chime_check)

        self._poll_spin = QtWidgets.QSpinBox()
        # Lower bound normally 30s (the usage APIs rate-limit and their windows are
        # hours long), but never above whatever the file already says: clamping a
        # hand-edited `poll_seconds = 10` up to 30 on open would then write that back on
        # accept, silently changing a setting the user never touched.
        stored_poll = int(self._cfg.stats.poll_seconds or 0)
        self._poll_spin.setRange(max(1, min(30, stored_poll)), max(3600, stored_poll))
        self._poll_spin.setSuffix(f" {t('settings.poll.suffix')}")
        self._poll_spin.setValue(stored_poll)
        form.addRow(t("settings.poll"), self._poll_spin)

        self._update_check = QtWidgets.QCheckBox(t("settings.update_check"))
        self._update_check.setChecked(self._cfg.update.check)
        form.addRow(self._update_check)  # spanning, same reason as the chime row

        return widget

    def _build_language_combo(self) -> QtWidgets.QComboBox:
        """Interface-language picker, listing each language under its own name.

        Endonyms, not English names ("Polski", not "Polish"): someone who opens this
        because the interface is in a language they don't read is looking for the word
        they *do* recognise. Only the interface changes — the hint under the combo says
        so, because a usage panel that keeps reporting an agent's own API wording in
        English would otherwise look like a half-applied setting.
        """
        combo = QtWidgets.QComboBox()
        for code, name in LANGUAGES:
            combo.addItem(name, userData=code)
        # `normalize` so a hand-edited `language = "ru_RU"` (or a code this build doesn't
        # know) selects the language it resolves to rather than silently landing on the
        # first row and writing that back on accept.
        index = combo.findData(normalize_language(self._cfg.ui.language))
        combo.setCurrentIndex(index if index >= 0 else 0)
        return combo

    # --- Lighting tab --------------------------------------------------------

    def _build_lighting_tab(self) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(widget)

        form = QtWidgets.QFormLayout()
        self._engine_combo = QtWidgets.QComboBox()
        for mode in ENGINE_MODES:
            label = _engine_label(mode)
            # Availability detail is filled in by `_apply_engine_probe` once the probe
            # comes back; the real engines say so meanwhile rather than reading as
            # "available".
            if mode not in ("auto", "none"):
                label += _DETAIL_GAP + t("settings.engine.checking")
            self._engine_combo.addItem(label, userData=mode)
        index = self._engine_combo.findData(self._cfg.engine.mode)
        self._engine_combo.setCurrentIndex(index if index >= 0 else 0)
        form.addRow(t("settings.engine"), self._engine_combo)
        outer.addLayout(form)

        outer.addWidget(self._build_colors_group())
        outer.addStretch(1)
        return widget

    def _build_colors_group(self) -> QtWidgets.QWidget:
        """One swatch per status, driving **both** the tray icon and the devices.

        `Config` keeps two palettes (see `DeviceColorsConfig`'s docstring): the icon's
        hues are sampled from the brand mark and judged at 16px on screen, the device's
        are fully saturated primaries that survive an RGB LED behind a diffuser. That
        split is worth keeping as a *default*, but it must not leak into this dialog as
        two pickers: a colour someone deliberately chooses here is what they expect to
        see on the icon and on the keyboard. So the swatch shows the icon colour and, on
        accept, a colour the user actually changed is written to both — while a status
        they never touched keeps its LED-tuned device default untouched too.
        """
        group = QtWidgets.QGroupBox(t("settings.colors.group"))
        grid = QtWidgets.QGridLayout(group)

        self._color_buttons: dict[str, _ColorButton] = {}
        #: An explicit device colour queued for a status, or None to derive it from the
        #: swatch. Only "Reset colours to defaults" queues one — it has to restore the
        #: *hardware* defaults, which aren't the icon's (see `_reset_colors`).
        self._device_overrides: dict[str, str | None] = dict.fromkeys(_STATUSES)
        for row, status in enumerate(_STATUSES):
            grid.addWidget(QtWidgets.QLabel(f"{t(f'settings.colors.{status}')}:"), row, 0)
            icon_hex = getattr(self._cfg.colors, status)
            button = _ColorButton(_safe_hex(icon_hex, getattr(ColorsConfig(), status)))
            self._color_buttons[status] = button
            grid.addWidget(button, row, 1)
        grid.setColumnStretch(2, 1)

        # One line. The reason the two palettes differ is real (below, as a tooltip) but
        # it is not something to make someone read every time they open Settings.
        note = _hint(t("settings.colors.note"))
        note.setToolTip(t("settings.colors.note_tooltip"))
        grid.addWidget(note, len(_STATUSES), 0, 1, 3)

        reset_colors = QtWidgets.QPushButton(t("settings.colors.reset"))
        reset_colors.clicked.connect(self._reset_colors)
        grid.addWidget(reset_colors, len(_STATUSES) + 1, 0, 1, 3)
        return group

    def _reset_colors(self) -> None:
        """Back to the shipped palette — both palettes, since accepting will write both.

        The device column goes back to `DeviceColorsConfig`'s saturated primaries rather
        than to whatever the icon defaults are, so "reset" really does restore what a
        fresh install looks like on the hardware too.
        """
        icon_defaults = ColorsConfig()
        device_defaults = DeviceColorsConfig()
        for status, button in self._color_buttons.items():
            button.set_hex_color(getattr(icon_defaults, status))
            self._device_overrides[status] = getattr(device_defaults, status)

    # --- engine availability probe -------------------------------------------

    def _start_engine_probe(self) -> None:
        """Probe the lighting engines off the GUI thread.

        `available_engines` opens a socket to `engine.openrgb.host:port` and loads two
        vendor SDKs. That is normally instant against localhost, but the host is
        configurable — pointed at a remote or firewalled machine it blocks for the full
        connect timeout, and doing that inline froze the window for as long as it took
        just to *open* Settings. Reads the private `result_cfg` copy, so nothing here
        touches state the GUI thread is editing.
        """
        cfg = self.result_cfg

        def run() -> None:
            try:
                env = detect.detect()
                probes = dict(available_engines(cfg))
            except Exception:
                log.exception("engine probe for the settings dialog failed")
                return
            self.engine_probe_done.emit(env, probes)

        threading.Thread(target=run, daemon=True, name="tv-settings-probe").start()

    @QtCore.Slot(object, object)
    def _apply_engine_probe(self, env, probes: dict) -> None:
        """Replace each engine's "(checking…)" with what the probe found.

        Same three-way distinction the console wizard draws (`ui.wizard._engine_label`):
        "not supported on this platform" is not the same answer as "supported but not
        running", and collapsing them into "not detected" tells someone on Linux to go
        start Razer Synapse.
        """
        for i in range(self._engine_combo.count()):
            mode = self._engine_combo.itemData(i)
            label = _engine_label(mode)
            if mode not in ("auto", "none"):
                if not engine_supported(mode, env):
                    # The platform name is a config/detection value ("windows", "linux"),
                    # not prose — passed through as it is everywhere else it appears.
                    platform = getattr(env, "platform", "") or t("settings.engine.this_platform")
                    detail = t("settings.engine.unsupported", platform=platform)
                elif probes.get(mode):
                    detail = t("settings.engine.running")
                else:
                    detail = t("settings.engine.not_running")
                label += _DETAIL_GAP + detail
            self._engine_combo.setItemText(i, label)

    # --- actions --------------------------------------------------------

    def _open_advanced_setup(self) -> None:
        """Hand off to the console wizard for anything this dialog doesn't cover.

        Any edits already made here are discarded (`reject`) rather than silently
        merged behind the wizard's back — the wizard re-reads the on-disk config
        itself, so running both against divergent in-memory state would be
        confusing at best. The caller launches it once this dialog has closed.
        """
        self.launch_wizard = True
        self.reject()

    def _on_accept(self) -> None:
        checked = [
            self._agent_list.item(i).data(QtCore.Qt.UserRole)
            for i in range(self._agent_list.count())
            if self._agent_list.item(i).checkState() == QtCore.Qt.Checked
        ]
        # Same floor the wizard's agent step enforces: with nothing enabled TintaView
        # tracks nothing, shows "No agents enabled." and looks broken rather than off.
        if not checked:
            QtWidgets.QMessageBox.warning(self, "TintaView", t("settings.error.no_agents"))
            return

        cfg = self.result_cfg
        newly_enabled = [k for k in checked if k not in self._cfg.enabled_agents]
        cfg.enabled_agents = checked
        self._seed_new_agent_defaults(newly_enabled)

        cfg.ui.chime_on_confirm = self._chime_check.isChecked()
        cfg.ui.language = self._language_combo.currentData()
        cfg.stats.poll_seconds = self._poll_spin.value()
        cfg.update.check = self._update_check.isChecked()
        cfg.engine.mode = self._engine_combo.currentData()
        for status, button in self._color_buttons.items():
            setattr(cfg.colors, status, button.hex_color())
            # A colour the user actually picked drives the hardware too — that's what
            # "custom colour" means to anyone setting one. A status left untouched keeps
            # whatever `[colors.device]` already said, so the LED-tuned defaults (and any
            # hand-edited override) survive a trip through this dialog.
            queued = self._device_overrides.get(status)
            if queued is not None:
                setattr(cfg.colors.device, status, queued)
            elif button.edited:
                setattr(cfg.colors.device, status, button.hex_color())

        try:
            config_mod.save(cfg)
        except Exception:
            log.exception("could not save settings")
            QtWidgets.QMessageBox.warning(self, "TintaView", t("settings.error.save_failed"))
            return
        self._offer_hook_install(newly_enabled)
        self.accept()

    def _seed_new_agent_defaults(self, newly_enabled: list[str]) -> None:
        """Give an agent enabled here the same starting point the wizard would give it.

        `confirm_detection` is the one that matters: Cursor declares `stall` (it has no
        "waiting for you" event to hook), and leaving it at `AgentConfig`'s `event`
        default means the agent is enabled, its hooks fire, and the light simply never
        turns red. Only for keys with no stored settings at all — an agent that was
        configured before, disabled, and re-enabled keeps whatever it had.
        """
        for key in newly_enabled:
            adapter = agents_base.get(key)
            if adapter is None or key in self.result_cfg.agents:
                continue
            self.result_cfg.agent(key).confirm_detection = adapter.default_confirm_detection

    def _missing_hooks(self, keys: list[str]) -> list[str]:
        """Display names of `keys` whose hooks aren't installed and pointing at us.

        Stats-only providers are skipped — they have no hooks by definition.
        """
        from ..install import hooks as hooks_mod

        hook_bin = config_mod.hook_bin_path()
        missing = []
        for key in keys:
            adapter = agents_base.get(key)
            if adapter is None:
                continue
            try:
                state = hooks_mod.status(adapter, hook_bin)
            except Exception:
                log.exception("could not check hook status for %s", key)
                continue
            if state != hooks_mod.STATUS_INSTALLED:
                missing.append(adapter.display_name)
        return missing

    def _offer_hook_install(self, newly_enabled: list[str]) -> None:
        """Ticking an agent here cannot install its hooks — say so, and offer the wizard.

        Without this, enabling an agent in this dialog produced a config that looks
        entirely correct and can never work: no hook entries in the agent's own config
        file means no events, which reads as "TintaView is broken", not "setup is
        unfinished". Only ever a prompt — the diff-and-confirm hook flow stays in the
        console wizard (see this module's docstring), and declining leaves the rest of
        the settings saved either way.
        """
        missing = self._missing_hooks(newly_enabled)
        if not missing:
            return
        answer = QtWidgets.QMessageBox.question(
            self, "TintaView",
            t("settings.hooks.prompt", agents=", ".join(missing)),
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        )
        if answer == QtWidgets.QMessageBox.Yes:
            self.launch_wizard = True
