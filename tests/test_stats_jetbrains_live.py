"""JetBrains AI card: the live balance in `other.xml` (PhpStorm 2026.3 stopped refreshing
`AIAssistantQuotaManager2.xml`) and Junie CLI's spend counted in the same figure."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tintaview.core.config import AgentConfig, Config
from tintaview.stats import junie_usage
from tintaview.stats.providers import jetbrains as jb
from tintaview.stats.providers.jetbrains import JetBrainsUsageProvider
from tintaview.stats.service import StatsService

NOW = datetime(2026, 10, 26, 17, 50, tzinfo=UTC).timestamp()
DAY = 86400.0


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    monkeypatch.setattr(jb, "_now", lambda: NOW)


def _touch(path: Path, mtime: float) -> Path:
    os.utime(path, (mtime, mtime))
    return path


def _quota_file(root: Path, ide: str, *, current: str = "12669.9155", mtime: float,
                next_refill: str = "2026-10-15T10:00:11.410Z") -> Path:
    """The frozen `AIAssistantQuotaManager2.xml`, in the escaped shape the IDE writes."""
    path = root / ide / "options" / "AIAssistantQuotaManager2.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    refill = json.dumps({"type": "Known", "next": next_refill,
                         "tariff": {"amount": "1000000", "duration": "PT720H"}})
    info = json.dumps({"type": "Available",
                       "tariffQuota": {"current": current, "maximum": "1000000",
                                       "available": str(1000000 - float(current))},
                       "topUpQuota": {"current": "0", "maximum": "4498808.015",
                                      "available": "4498808.015"}})
    esc = lambda text: text.replace('"', "&quot;")  # noqa: E731
    path.write_text(
        '<application><component name="AIAssistantQuotaManager2">'
        f'<option name="nextRefill" value="{esc(refill)}" />'
        f'<option name="quotaInfo" value="{esc(info)}" />'
        "</component></application>", encoding="utf-8")
    return _touch(path, mtime)


def _other_xml(root: Path, ide: str, *, tariff: str | None = "935729.9515",
               top_up: str | None = "4498808.015", mtime: float, plain_quotes: bool = False) -> Path:
    """`options/other.xml` as PhpStorm 2026.3 writes it: a JSON blob, `&quot;`-escaped."""
    path = root / ide / "options" / "other.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    q = '"' if plain_quotes else "&quot;"
    entries = ['"unrelated.key": "1"']
    if tariff is not None:
        entries.append(f'{q}AIHubNotificationManager.lastTariffQuotaAvailable{q}: {q}{tariff}{q}')
    if top_up is not None:
        entries.append(f'{q}AIHubNotificationManager.lastTopUpQuotaAvailable{q}: {q}{top_up}{q}')
    path.write_text('<application><component name="PropertyService"><![CDATA[{\n  '
                    + ",\n  ".join(entries) + "\n}]]></component></application>", encoding="utf-8")
    return _touch(path, mtime)


@pytest.fixture
def ide_root(tmp_path, monkeypatch):
    root = tmp_path / "JetBrains"
    root.mkdir()
    monkeypatch.setattr(jb, "_default_jetbrains_root", lambda: root)
    return root


# --------------------------------------------------------------------------- live balance


def test_the_live_balance_beats_the_frozen_quota_file_and_matches_the_ide(ide_root):
    """The measured case: PhpStorm 2026.3 showed 9.36 of 10.00 left and a renewal on
    14 Nov; its `AIAssistantQuotaManager2.xml` still said 0.13 used and a refill on 15 Oct."""
    _quota_file(ide_root, "PhpStorm2026.3", mtime=NOW - 27 * DAY)
    _other_xml(ide_root, "PhpStorm2026.3", mtime=NOW - 60)

    result = JetBrainsUsageProvider().fetch(AgentConfig())

    assert result.ok and result.notice is None
    monthly, top_up = result.rows
    assert monthly.right.startswith("0.64 / 10.00 used · 14 Nov")  # 10.00 - 9.36
    assert monthly.pct == pytest.approx(6.427, abs=0.01)
    assert top_up.right == "44.99 credits available"


def test_without_any_quota_file_the_live_balance_alone_still_makes_a_card(ide_root):
    """A fresh IDE install has no `AIAssistantQuotaManager2.xml`: say what is left, and
    do not invent a maximum, a bar or a renewal date the data never gave."""
    _other_xml(ide_root, "PhpStorm2026.3", mtime=NOW - 60)

    result = JetBrainsUsageProvider().fetch(AgentConfig())

    assert result.ok
    monthly, top_up = result.rows
    assert (monthly.right, monthly.kind, monthly.show_pct) == ("9.36 left", "info", False)
    assert top_up.right == "44.99 credits available"
    assert result.notice is None  # no period known, so nothing to call stale


def test_a_quota_file_newer_than_other_xml_wins(ide_root):
    """Older IDEs keep refreshing the quota file; whichever was written last is right."""
    _other_xml(ide_root, "PhpStorm2026.3", mtime=NOW - 5 * DAY)
    _quota_file(ide_root, "PyCharm2026.2", current="500000", mtime=NOW - 60,
                next_refill="2026-11-14T10:00:11.410Z")

    monthly = JetBrainsUsageProvider().fetch(AgentConfig()).rows[0]

    assert monthly.right.startswith("5.00 / 10.00 used")


def test_other_xml_without_the_keys_is_ignored(ide_root):
    _quota_file(ide_root, "PhpStorm2026.3", current="161185.755", mtime=NOW - 60,
                next_refill="2026-11-14T10:00:11.410Z")
    _other_xml(ide_root, "PhpStorm2026.3", tariff=None, top_up=None, mtime=NOW)

    assert JetBrainsUsageProvider().fetch(AgentConfig()).rows[0].right.startswith("1.61 / 10.00")


def test_plain_quotes_are_read_too(ide_root):
    _other_xml(ide_root, "X1", mtime=NOW, plain_quotes=True)
    assert JetBrainsUsageProvider().fetch(AgentConfig()).rows[0].right == "9.36 left"


def test_a_pinned_quota_path_means_that_file_only(ide_root, tmp_path):
    _other_xml(ide_root, "PhpStorm2026.3", mtime=NOW)
    pinned = _quota_file(tmp_path, "pinned", current="300000", mtime=NOW - 60,
                         next_refill="2026-11-14T10:00:11.410Z")

    monthly = JetBrainsUsageProvider().fetch(AgentConfig(quota_path=str(pinned))).rows[0]

    assert monthly.right.startswith("3.00 / 10.00 used")


def test_figures_from_before_the_current_period_carry_a_notice(ide_root):
    """The frozen file alone: last period's numbers must not pass as this period's."""
    _quota_file(ide_root, "PhpStorm2026.3", mtime=NOW - 27 * DAY)  # Sep 29

    result = JetBrainsUsageProvider().fetch(AgentConfig())

    assert result.ok
    assert result.notice and "renewal" in result.notice
    assert "14 Nov" in result.rows[0].right  # the date shown is the next one, not Oct 15


def test_detect_sees_a_machine_with_only_other_xml(ide_root):
    assert jb.detect() is False
    _other_xml(ide_root, "PhpStorm2026.3", mtime=NOW)
    assert jb.detect() is True


@pytest.mark.parametrize(("text", "seconds"), [
    ("PT720H", 720 * 3600.0), ("P30D", 30 * DAY), ("P1DT12H", 36 * 3600.0),
    ("PT0S", None), ("", None), ("30 days", None), (None, None),
])
def test_duration_parsing(text, seconds):
    assert jb._duration_seconds(text) == seconds


def test_a_past_refill_rolls_forward_by_whole_periods():
    refill = {"type": "Known", "next": "2026-10-15T10:00:11.410Z",
              "tariff": {"duration": "PT720H"}}
    start, end = jb._period(refill, NOW)
    assert datetime.fromtimestamp(end, UTC).date().isoformat() == "2026-11-14"
    assert end - start == 30 * DAY
    # several periods behind: still the first boundary after "now"
    start, end = jb._period(refill, NOW + 70 * DAY)
    assert datetime.fromtimestamp(end, UTC).date().isoformat() == "2027-01-13"
    assert jb._period({"type": "Unknown"}, NOW) is None


# --------------------------------------------------------------------------- Junie CLI


def _session(home: Path, name: str, *, cost: float, updated: float, provider: str | None = "jetbrains-ai",
             sub_cost: float = 0.0) -> Path:
    folder = home / "sessions" / name
    folder.mkdir(parents=True)
    summary = {"sessionId": name, "updatedAt": int(updated * 1000),
               "mainAgent": {"modelUsage": [{"model": "m", "cost": cost}]},
               "subagents": [{"modelUsage": [{"model": "s", "cost": sub_cost}]}] if sub_cost else []}
    (folder / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    if provider is not None:
        (folder / "settings.json").write_text(
            json.dumps({"model": {"providerCode": provider}}), encoding="utf-8")
    return folder


def _provider(*homes: Path) -> JetBrainsUsageProvider:
    return JetBrainsUsageProvider(junie_roots=lambda: list(homes))


def test_junie_spend_after_the_ides_last_write_is_counted_in_the_same_figure(ide_root, tmp_path):
    """One figure, not a second line: Windows and WSL spend since the IDE last wrote its
    balance (9.36 left = 0.64 used) is added, marked with a ~, and nothing else is."""
    _quota_file(ide_root, "PhpStorm2026.3", mtime=NOW - 27 * DAY)
    _other_xml(ide_root, "PhpStorm2026.3", mtime=NOW - 60)
    wsl, win = tmp_path / "wsl-junie", tmp_path / "win-junie"
    _session(wsl, "s1", cost=0.30, sub_cost=0.05, updated=NOW - 30)
    _session(win, "s2", cost=0.10, updated=NOW - 10)
    _session(wsl, "byok", cost=9.0, updated=NOW - 30, provider="proxy:ingrazzio")
    _session(wsl, "nosettings", cost=9.0, updated=NOW - 30, provider=None)
    # already in the IDE's balance: it was synced before the IDE wrote the figure
    _session(wsl, "before", cost=9.0, updated=NOW - DAY)

    result = _provider(wsl, win).fetch(AgentConfig())

    assert result.estimate == []  # no separate line
    monthly = result.rows[0]
    assert monthly.right.startswith("~1.09 / 10.00 used · 14 Nov")  # 0.64 + 0.35 + 0.10
    assert monthly.pct == pytest.approx(10.93, abs=0.02)


def test_with_no_new_junie_spend_the_figure_is_the_ides_own_and_unmarked(ide_root, tmp_path):
    _other_xml(ide_root, "PhpStorm2026.3", mtime=NOW - 60)
    _quota_file(ide_root, "PhpStorm2026.3", mtime=NOW - 27 * DAY)
    home = tmp_path / "junie"
    _session(home, "old", cost=0.5, updated=NOW - DAY)

    assert _provider(home).fetch(AgentConfig()).rows[0].right.startswith("0.64 / 10.00 used")


def test_junie_only_lists_the_top_of_each_session_folder(ide_root, tmp_path):
    home = tmp_path / "junie"
    folder = _session(home, "s1", cost=0.2, updated=NOW - 30)
    nested = folder / "checkpoints" / "x"
    nested.mkdir(parents=True)
    (nested / "summary.json").write_text(json.dumps(
        {"updatedAt": int(NOW * 1000), "mainAgent": {"modelUsage": [{"cost": 50}]}}), encoding="utf-8")
    _quota_file(ide_root, "PhpStorm2026.3", mtime=NOW - 27 * DAY)
    _other_xml(ide_root, "PhpStorm2026.3", mtime=NOW - 60)

    assert _provider(home).fetch(AgentConfig()).rows[0].right.startswith("~0.84 / 10.00 used")


def test_the_estimate_setting_switches_the_junie_part_off(ide_root, tmp_path):
    _quota_file(ide_root, "PhpStorm2026.3", mtime=NOW - 27 * DAY)
    _other_xml(ide_root, "PhpStorm2026.3", mtime=NOW - 60)
    home = tmp_path / "junie"
    _session(home, "s1", cost=0.2, updated=NOW - 30)

    assert _provider(home).fetch(AgentConfig(), with_estimate=False).rows[0].right.startswith(
        "0.64 / 10.00 used")
    assert _provider(home).fetch(AgentConfig()).rows[0].right.startswith("~0.84 / 10.00 used")


def test_a_bare_provider_reads_no_junie_at_all(ide_root):
    """No roots handed over: it must not wander into the real machine's ~/.junie."""
    _other_xml(ide_root, "PhpStorm2026.3", mtime=NOW)
    assert JetBrainsUsageProvider().fetch(AgentConfig()).rows[0].right == "9.36 left"


def test_spend_comes_off_what_is_left_when_no_maximum_is_known(ide_root, tmp_path):
    _other_xml(ide_root, "PhpStorm2026.3", mtime=NOW - 60)
    home = tmp_path / "junie"
    _session(home, "s1", cost=0.45, updated=NOW - 30)

    assert _provider(home).fetch(AgentConfig()).rows[0].right == "~8.91 left"


def test_stale_figures_get_no_junie_spend_added(ide_root, tmp_path):
    """Last period's numbers plus this period's spend would be neither: leave them alone."""
    _quota_file(ide_root, "PhpStorm2026.3", mtime=NOW - 27 * DAY)
    home = tmp_path / "junie"
    _session(home, "s1", cost=0.45, updated=NOW - 30)

    result = _provider(home).fetch(AgentConfig())

    assert result.notice
    assert result.rows[0].right.startswith("0.13 / 10.00 used")


def test_an_unreadable_session_is_skipped_not_fatal(ide_root, tmp_path):
    _quota_file(ide_root, "PhpStorm2026.3", mtime=NOW - 27 * DAY)
    _other_xml(ide_root, "PhpStorm2026.3", mtime=NOW - 60)
    home = tmp_path / "junie"
    _session(home, "good", cost=0.2, updated=NOW - 30)
    bad = home / "sessions" / "bad"
    bad.mkdir()
    (bad / "summary.json").write_text("{not json", encoding="utf-8")

    assert _provider(home).fetch(AgentConfig()).rows[0].right.startswith("~0.84 / 10.00 used")


def test_no_quota_at_all_is_just_the_error(ide_root, tmp_path):
    home = tmp_path / "junie"
    _session(home, "s1", cost=0.2, updated=NOW - 30)

    result = _provider(home).fetch(AgentConfig())

    assert not result.ok and result.error and result.estimate == []


# --------------------------------------------------------------------------- wiring


def test_the_service_hands_the_jetbrains_card_every_junie_home(tmp_path, monkeypatch):
    from tintaview.agents import base as agents_base

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "winprofile"))
    cfg = Config()
    cfg.agent("junie").home = str(tmp_path / "distro" / ".junie")

    roots = junie_usage.session_roots(cfg)

    assert roots == [tmp_path / "distro" / ".junie", agents_base.get("junie").default_home()]
    assert roots[1] == tmp_path / "winprofile" / ".junie"
    # one entry when the configured home is the default, not two
    assert junie_usage.session_roots(Config()) == [agents_base.get("junie").default_home()]
    provider = StatsService(cfg)._providers["jetbrains"]
    assert isinstance(provider, JetBrainsUsageProvider) and provider._junie_roots() == roots
