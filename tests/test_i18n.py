"""Tests for the interface translations (`tintaview.i18n`).

Translation bugs are quiet by nature: a missing key or a mistyped placeholder renders a
key, an English string or nothing at all — in a language the person reviewing the change
most likely doesn't read. So the catalogues are checked mechanically:

- every language has the *same keys* as `en.json`, which is the source of truth;
- every key has the *same placeholders* as its English original, so no caller can hand a
  translation a `{count}` it doesn't use (or be asked for a `{minutes}` it never has);
- every plural entry carries every form its language's rule can select;
- every literal `t("…")` in the package resolves to a real key, and the dynamically
  built key families (statuses, engine modes, weekdays, months, quota ids) are covered
  by name here rather than only at runtime.

The last test in the file is the one that would have caught the packaging mistake: the
catalogues are data files, so a missing `package-data` entry ships a wheel where every
language silently falls back to English.
"""

from __future__ import annotations

import json
import re
import string
from pathlib import Path

import pytest

from tintaview import i18n
from tintaview.core import config as config_mod
from tintaview.core.events import STATUS_CONFIRM, STATUS_IDLE, STATUS_WORKING
from tintaview.engines.factory import ENGINE_MODES

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCALES_DIR = REPO_ROOT / "tintaview" / "i18n" / "locales"
PACKAGE_DIR = REPO_ROOT / "tintaview"

OTHER_LANGUAGES = [code for code in i18n.LANGUAGE_CODES if code != i18n.DEFAULT_LANGUAGE]


@pytest.fixture(autouse=True)
def _restore_language():
    """Every test here leaves the process back on English.

    `set_language` is global state, and the rest of the suite (tray tooltips, stats row
    labels) asserts English text — a leaked language would fail those tests somewhere
    else entirely, which is the worst kind of failure to debug.
    """
    yield
    i18n.set_language(i18n.DEFAULT_LANGUAGE)


def _catalog_file(code: str) -> dict:
    return json.loads((LOCALES_DIR / f"{code}.json").read_text(encoding="utf-8"))


def _placeholders(template: str) -> set[str]:
    return {
        name for _lit, name, _spec, _conv in string.Formatter().parse(template) if name
    }


def _forms(entry) -> dict[str, str]:
    """A catalogue entry as {form: template} — a plain string is the only form there is."""
    return entry if isinstance(entry, dict) else {"other": entry}


@pytest.fixture(scope="module")
def english() -> dict:
    return _catalog_file("en")


# --------------------------------------------------------------------------- catalogues


def test_every_supported_language_has_a_catalogue():
    for code, name in i18n.LANGUAGES:
        assert (LOCALES_DIR / f"{code}.json").is_file(), f"no catalogue for {name}"


def test_no_stray_catalogues():
    """A file no `LANGUAGES` entry points at is dead weight nothing can select."""
    shipped = {p.stem for p in LOCALES_DIR.glob("*.json")}
    assert shipped == set(i18n.LANGUAGE_CODES)


@pytest.mark.parametrize("code", OTHER_LANGUAGES)
def test_keys_match_english_exactly(code: str, english: dict):
    catalog = _catalog_file(code)
    missing = sorted(set(english) - set(catalog))
    extra = sorted(set(catalog) - set(english))
    assert not missing, f"{code}.json is missing: {missing}"
    assert not extra, f"{code}.json has keys en.json doesn't: {extra}"


#: Placeholders a caller passes that the *English* wording happens not to use, so they
#: can't be discovered from `en.json` alone. Only one so far, and deliberately: the clock
#: line is offered as both a 12-hour and a 24-hour field (`stats.format.reset_at_time`)
#: because English reads "3:59 PM" and German, Polish and Russian read "15:59".
_EXTRA_PLACEHOLDERS = {"usage.reset.at_time": {"hour24"}}


@pytest.mark.parametrize("code", OTHER_LANGUAGES)
def test_placeholders_match_english(code: str, english: dict):
    catalog = _catalog_file(code)
    for key, source in english.items():
        expected = set(_EXTRA_PLACEHOLDERS.get(key, ()))
        for template in _forms(source).values():
            expected |= _placeholders(template)
        for form, template in _forms(catalog[key]).items():
            got = _placeholders(template)
            # A translation may leave one out (Russian's clock line drops {ampm}), but it
            # may never ask for a placeholder the caller doesn't pass — that renders as
            # the English string, or as nothing useful at all.
            assert got <= expected, f"{code}.json {key} [{form}] wants unknown {sorted(got - expected)}"


@pytest.mark.parametrize("code", i18n.LANGUAGE_CODES)
def test_plural_entries_cover_every_form_their_rule_selects(code: str, english: dict):
    """Whatever `_plural_form` can return for a language, that language must have.

    Belt and braces on top of `_FORM_FALLBACKS`: falling back to another form is there so
    a half-finished catalogue still renders, not so a shipped one can be wrong about
    Russian's "5 сессий".
    """
    catalog = _catalog_file(code)
    needed = {i18n._plural_form(code, n) for n in range(0, 101)}
    plural_keys = [key for key, value in english.items() if isinstance(value, dict)]
    assert plural_keys, "en.json has no plural entries — this test would be vacuous"
    for key in plural_keys:
        forms = catalog[key]
        assert isinstance(forms, dict), f"{code}.json {key} must be a table of plural forms"
        assert needed <= set(forms), f"{code}.json {key} is missing {sorted(needed - set(forms))}"


def test_slavic_plural_rule_picks_the_right_forms():
    for n, form in ((1, "one"), (21, "one"), (11, "many"), (2, "few"), (24, "few"),
                     (12, "many"), (5, "many"), (0, "many")):
        assert i18n._plural_form("ru", n) == form, n
    assert i18n._plural_form("de", 1) == "one"
    assert i18n._plural_form("de", 2) == "other"


def test_russian_active_sessions_uses_all_three_forms():
    i18n.set_language("ru")
    assert i18n.t("tray.tooltip.active_sessions", count=1) == "1 активная сессия"
    assert i18n.t("tray.tooltip.active_sessions", count=3) == "3 активные сессии"
    assert i18n.t("tray.tooltip.active_sessions", count=7) == "7 активных сессий"


# --------------------------------------------------------------------------- key coverage


#: `t()` calls whose key is built at runtime. Each family is asserted explicitly below,
#: since the source scan can only see literal keys.
_DYNAMIC_PREFIXES = (
    "settings.colors.",
    "engine.mode.",
    "usage.weekday.",
    "usage.month.",
    "usage.copilot.quota.",
    "usage.claude.window_",
    "usage.copilot.plan.",
)


def test_every_literal_t_call_in_the_package_has_a_key(english: dict):
    pattern = re.compile(r"""\bt\(\s*["']([a-z][a-z0-9_.]+)["']""")
    seen = 0
    for path in PACKAGE_DIR.rglob("*.py"):
        for key in pattern.findall(path.read_text(encoding="utf-8")):
            seen += 1
            assert key in english, f"{path.relative_to(REPO_ROOT)} uses missing key {key!r}"
    assert seen > 50, "the scan found suspiciously few t() calls — has the pattern rotted?"


def test_no_unused_keys(english: dict):
    """A key nothing renders is a string seven translators wrote for nothing."""
    sources = "\n".join(
        path.read_text(encoding="utf-8") for path in PACKAGE_DIR.rglob("*.py")
    )
    unused = [
        key for key in english
        # Both quote styles: a key inside an f-string is written with single quotes.
        if f'"{key}"' not in sources and f"'{key}'" not in sources
        and not key.startswith(_DYNAMIC_PREFIXES)
    ]
    assert not unused, f"en.json keys nothing uses: {unused}"


def test_status_and_engine_key_families_are_complete(english: dict):
    for status in (STATUS_IDLE, STATUS_WORKING, STATUS_CONFIRM):
        assert f"settings.colors.{status}" in english
    for mode in ENGINE_MODES:
        assert f"engine.mode.{mode}" in english


def test_date_key_families_are_complete(english: dict):
    from tintaview.stats import format as fmt

    for suffix in fmt._WEEKDAY_KEYS:
        assert f"usage.weekday.{suffix}" in english
    for suffix in fmt._MONTH_KEYS:
        assert f"usage.month.{suffix}" in english


def test_copilot_quota_labels_are_translated(english: dict):
    from tintaview.stats.providers import copilot

    for quota_id in copilot._QUOTA_ORDER:
        assert f"usage.copilot.quota.{quota_id}" in english
    for key in copilot._PLAN_LABELS.values():
        assert key in english


# --------------------------------------------------------------------------- runtime


def test_unknown_key_returns_the_key_rather_than_raising():
    assert i18n.t("nope.not.a.key") == "nope.not.a.key"


def test_missing_translation_falls_back_to_english(monkeypatch):
    monkeypatch.setitem(i18n._catalogs, "ru", {})  # a catalogue that answers nothing
    i18n.set_language("ru")
    assert i18n.t("tray.menu.quit") == "Quit"


def test_translation_with_bad_placeholders_falls_back_to_english(monkeypatch):
    """The failure mode this guards: a translator renames `{version}` and every tray
    "up to date" dialog raises `KeyError` from inside a Qt slot."""
    monkeypatch.setitem(
        i18n._catalogs, "ru", {"tray.update.up_to_date": "Версия {versionn}."}
    )
    i18n.set_language("ru")
    assert i18n.t("tray.update.up_to_date", version="1.2.3") == "You're up to date (version 1.2.3)."


def test_language_can_be_switched_back_and_forth():
    assert i18n.current_language() == "en"
    assert i18n.set_language("uk") == "uk"
    assert i18n.current_language() == "uk"
    assert i18n.t("tray.menu.quit") == "Вийти"
    i18n.set_language("en")
    assert i18n.t("tray.menu.quit") == "Quit"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("ru", "ru"), ("RU", "ru"), ("ru_RU", "ru"), ("uk-UA", "ru" if False else "uk"),
        ("pl_PL.UTF-8", "pl"), ("  de  ", "de"),
        ("", "en"), (None, "en"), ("klingon", "en"), ("zh_CN", "en"),
    ],
)
def test_normalize_accepts_locales_and_falls_back_to_english(value, expected):
    assert i18n.normalize(value) == expected


def test_language_name_is_the_endonym():
    assert i18n.language_name("pl") == "Polski"
    assert i18n.language_name("ru_RU") == "Русский"


def test_every_language_renders_the_whole_catalogue_without_raising(english: dict):
    """Smoke test over every string in every language.

    Formats each entry with a placeholder value per name it declares, which is the cheap
    way to catch a stray `{` or a `{0}` positional field that `t()` would log and skip.
    """
    for code in i18n.LANGUAGE_CODES:
        i18n.set_language(code)
        for key, source in english.items():
            names = set()
            for template in _forms(source).values():
                names |= _placeholders(template)
            kwargs = {name: 1 if name == "count" else "X" for name in names}
            rendered = i18n.t(key, **kwargs)
            assert rendered and rendered != key, f"{code}: {key} did not render"


# --------------------------------------------------------------------------- integration


def test_tray_tooltip_and_usage_rows_follow_the_language():
    from tintaview.stats.providers.codex import _window_label

    i18n.set_language("ru")
    assert i18n.t("tray.tooltip.active_sessions", count=1) == "1 активная сессия"
    assert _window_label({"window_minutes": 10080}, "fallback") == "Лимит на неделю"


def test_config_round_trips_the_language():
    cfg = config_mod.Config()
    assert cfg.ui.language == "en"  # English unless the user picks otherwise
    cfg.ui.language = "be"
    assert "language = 'be'" in config_mod.dumps(cfg)


def test_settings_dialog_and_wizard_both_expose_the_language():
    """AGENTS.md's "Two config UIs — touch both": `ui.language` is a `Config` field a
    user sets, so it has to be reachable from the popup *and* the console wizard."""
    dialog_src = (PACKAGE_DIR / "ui" / "settings_dialog.py").read_text(encoding="utf-8")
    wizard_src = (PACKAGE_DIR / "ui" / "wizard.py").read_text(encoding="utf-8")
    assert "ui.language" in dialog_src
    assert "ui.language" in wizard_src


def test_catalogues_are_declared_as_package_data():
    """Without this line in pyproject.toml the wheel ships no catalogues at all, and
    every language falls back to English on an installed copy while working perfectly
    from a source checkout."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"tintaview.i18n" = ["locales/*.json"]' in pyproject
