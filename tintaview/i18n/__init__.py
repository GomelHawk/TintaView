"""Interface translations — one JSON catalogue per language, stdlib only.

Why not Qt's `QTranslator`/`.qm`: the strings translated here are produced in two
places that must keep working with **no PySide6 at all** — the usage providers
(`stats/providers/*.py`, which build row labels and error reasons on a worker thread and
run in a `--headless` install too) and `core.config`. A Qt-only mechanism would cover the
widgets and leave those behind, so this layer is plain `dict` lookups over JSON shipped
as package data: no compiled catalogues, no build step, no new dependency.

Scope, deliberately: **the tray UI and the usage panel**. The console setup wizard,
`doctor` and the rest of the CLI stay English — they are read once at install time from a
terminal, and mixing a half-translated diff-and-confirm flow into someone's shell is
worse than plain English. Data that comes back from an agent's own API (release notes, a
provider's HTTP error text, a model or plan name) is likewise never translated: it is
quoted as it arrived.

Usage::

    from tintaview.i18n import t
    t("tray.menu.quit")                        # "Quit"
    t("usage.reset.in_minutes", minutes=12)    # "Resets in 12 min"
    t("tray.tooltip.session_count", count=2)   # plural-aware, see below

Contract for callers:

- `t()` **never raises**. A missing key, a malformed catalogue or a translation with a
  bad placeholder falls back to English and then to the key itself — a cosmetic label
  must not be able to kill a `paintEvent` or a stats poll.
- Every placeholder is **named** (`{count}`, not `{}`), so a translator may reorder them.
- `count=` selects a plural form when the catalogue entry is a table of forms rather
  than a string. The Slavic languages here need three, which is the whole reason for it.

`en.json` is the source of truth: it holds every key, and `tests/test_i18n.py` asserts
the other catalogues match it key for key and placeholder for placeholder.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_LANGUAGE = "en"

#: Supported languages as `(code, endonym)` — the name of each language *in* that
#: language, which is what a language picker has to show: someone looking for Polish is
#: looking for "Polski", not for "Polish" spelled out in a language they can't read.
#: This tuple is the order both config UIs offer, and the set `normalize()` accepts.
LANGUAGES: tuple[tuple[str, str], ...] = (
    ("en", "English"),
    ("es", "Español"),
    ("it", "Italiano"),
    ("de", "Deutsch"),
    ("pl", "Polski"),
    ("ru", "Русский"),
    ("be", "Беларуская"),
    ("uk", "Українська"),
)

LANGUAGE_CODES: tuple[str, ...] = tuple(code for code, _ in LANGUAGES)
_LANGUAGE_NAMES: dict[str, str] = dict(LANGUAGES)

#: Loaded catalogues, keyed by language code. A language is read once, on first use.
#: Guarded by `_lock` — the tray's GUI thread and the stats worker threads both call
#: `t()`, so two of them can race on the first lookup of the same language.
_catalogs: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()

#: The active language. A plain module global: assignment is atomic, so a `t()` running
#: on a stats thread while the GUI thread switches language reads either the old or the
#: new value, never a torn one.
_current = DEFAULT_LANGUAGE


# --------------------------------------------------------------------------- plurals


def _slavic_category(n: int) -> str:
    """Russian / Ukrainian / Belarusian / Polish plural selection (CLDR forms).

    one:  1, 21, 31 …            (…1, but not …11)
    few:  2-4, 22-24 …           (…2-…4, but not …12-…14)
    many: 0, 5-20, 25-30 …
    """
    if n % 10 == 1 and n % 100 != 11:
        return "one"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "few"
    return "many"


def _germanic_category(n: int) -> str:
    return "one" if n == 1 else "other"


#: Per-language plural selector. Polish differs from Russian only for non-integers,
#: which no TintaView count is, so the three-form Slavic rule covers both.
_PLURAL_RULES = {
    "ru": _slavic_category,
    "uk": _slavic_category,
    "be": _slavic_category,
    "pl": _slavic_category,
}

#: Tried in order when a catalogue is missing the form the rule asked for, so a
#: two-form translation of a three-form language degrades instead of vanishing.
_FORM_FALLBACKS = ("other", "many", "few", "one")


def _plural_form(lang: str, count: Any) -> str:
    try:
        n = abs(int(count))
    except (TypeError, ValueError):
        return "other"
    return _PLURAL_RULES.get(lang, _germanic_category)(n)


# --------------------------------------------------------------------------- catalogues


def _load(lang: str) -> dict[str, Any]:
    """Read one catalogue from package data. A missing or corrupt file is an empty
    catalogue, not an error: English still answers every key."""
    try:
        from importlib.resources import files

        raw = files(__package__).joinpath("locales", f"{lang}.json").read_text(encoding="utf-8")
    except (OSError, ValueError, ModuleNotFoundError, TypeError):
        log.warning("i18n: no catalogue for %r", lang)
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        log.warning("i18n: catalogue for %r is not valid JSON", lang)
        return {}
    return data if isinstance(data, dict) else {}


def catalog(lang: str) -> dict[str, Any]:
    """The catalogue for `lang`, loading it on first use. Exposed for the tests."""
    with _lock:
        cached = _catalogs.get(lang)
        if cached is None:
            cached = _catalogs[lang] = _load(lang)
        return cached


# --------------------------------------------------------------------------- language


def normalize(value: str | None) -> str:
    """Turn anything config-shaped into a supported code, falling back to English.

    Accepts a bare code ("ru"), a locale ("ru_RU", "uk-UA", "pl_PL.UTF-8") and any
    casing, because `config.toml` is hand-edited and a user who writes `language =
    "de_DE"` means German, not "fall silently back to English".
    """
    if not value:
        return DEFAULT_LANGUAGE
    code = str(value).strip().lower().replace("_", "-").split(".")[0].split("-")[0]
    return code if code in _LANGUAGE_NAMES else DEFAULT_LANGUAGE


def set_language(value: str | None) -> str:
    """Make `value` the active language and return the code actually applied."""
    global _current
    lang = normalize(value)
    _current = lang
    catalog(lang)  # fail (and log) now rather than mid-paint
    return lang


def current_language() -> str:
    return _current


def language_name(code: str) -> str:
    """The endonym for `code` — its own name in its own language."""
    return _LANGUAGE_NAMES.get(normalize(code), code)


# --------------------------------------------------------------------------- lookup


def _template(lang: str, key: str, kwargs: dict[str, Any]) -> str | None:
    entry = catalog(lang).get(key)
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        form = _plural_form(lang, kwargs.get("count"))
        for candidate in (form, *_FORM_FALLBACKS):
            value = entry.get(candidate)
            if isinstance(value, str):
                return value
    return None


def t(key: str, /, **kwargs: Any) -> str:
    """The translated string for `key`, with `kwargs` interpolated by name.

    Falls back — English catalogue, then the key itself — rather than raising, and does
    so per *attempt*: a translation whose placeholders don't match the code (a typo'd
    `{minutes}`) is skipped in favour of the English one instead of surfacing a
    `KeyError` from whatever thread happened to render it.
    """
    langs = (_current,) if _current == DEFAULT_LANGUAGE else (_current, DEFAULT_LANGUAGE)
    for lang in langs:
        template = _template(lang, key, kwargs)
        if template is None:
            continue
        if not kwargs:
            return template
        try:
            return template.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            log.warning("i18n: %r in %r has placeholders the caller didn't supply", key, lang)
    return key
