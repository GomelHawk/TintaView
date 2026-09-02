"""Tests for the usage/statistics stack: three providers, the cache, and the service
that ties them together.

Nothing here touches the network or the user's real ~/.claude, ~/.codex or ~/.cursor —
every provider is pointed at a throwaway tmp_path via AgentConfig.home /
AgentConfig.state_db, and every HTTP call goes through a monkeypatched
urllib.request.urlopen.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tintaview.core.config import AgentConfig, Config
from tintaview.stats import _scan as scan_mod
from tintaview.stats.cache import UsageCache
from tintaview.stats.model import UsageProvider, UsageResult, UsageRow
from tintaview.stats.providers import codex as codex_mod
from tintaview.stats.providers import copilot as copilot_mod
from tintaview.stats.providers import jetbrains as jetbrains_mod
from tintaview.stats.providers.claude import ClaudeUsageProvider
from tintaview.stats.providers.codex import CodexUsageProvider
from tintaview.stats.providers.copilot import CopilotUsageProvider
from tintaview.stats.providers.cursor import CursorUsageProvider
from tintaview.stats.providers.jetbrains import JetBrainsUsageProvider
from tintaview.stats.service import StatsService

FIXTURES = Path(__file__).parent / "fixtures"


def _load_template(name: str, **subs: str) -> str:
    text = (FIXTURES / name).read_text(encoding="utf-8")
    for key, value in subs.items():
        text = text.replace(f"__{key}__", value)
    return text


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class _FakeResponse:
    """Minimal stand-in for the object `urlopen` returns as a context manager."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._payload


def _http_error(code: int, reason: str = "error") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url="https://example.invalid", code=code, msg=reason, hdrs=None, fp=None)  # type: ignore[arg-type]


def _write_credentials(home: Path, token: str = "test-token", subscription: str | None = "Max",
                        expires_in: float = 3600.0) -> None:
    home.mkdir(parents=True, exist_ok=True)
    payload = {
        "claudeAiOauth": {
            "accessToken": token,
            "expiresAt": int((time.time() + expires_in) * 1000),
            "subscriptionType": subscription,
        }
    }
    (home / ".credentials.json").write_text(json.dumps(payload), encoding="utf-8")


# --------------------------------------------------------------------------- Claude


class TestClaudeOfficial:
    def test_parses_official_payload(self, tmp_path, monkeypatch):
        home = tmp_path / "claude_home"
        _write_credentials(home)
        payload = json.loads((FIXTURES / "claude_usage_official.json").read_text())

        def fake_urlopen(req, timeout=None):
            assert req.get_header("Authorization") == "Bearer test-token"
            assert req.get_header("Anthropic-beta") == "oauth-2025-04-20"
            return _FakeResponse(json.dumps(payload).encode())

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        result = ClaudeUsageProvider().fetch(AgentConfig(home=str(home)))

        assert result.ok
        assert result.source == "official"
        assert result.error is None
        assert "Max" in result.header

        by_label = {row.label: row for row in result.rows}
        assert set(by_label) == {"5-hour limit", "Weekly · all models", "Weekly · Opus", "Usage credits"}

        five_hour = by_label["5-hour limit"]
        assert five_hour.pct == 42.5
        assert five_hour.show_pct is True
        assert five_hour.kind == "limit"
        assert five_hour.severity == "normal"

        weekly = by_label["Weekly · all models"]
        assert weekly.pct == 88.0
        assert weekly.severity == "warning"  # taken from the matching `limits[].severity`

        opus = by_label["Weekly · Opus"]
        assert opus.pct == 10.0

        # The money-in-minor-units case: amount_minor 1258 / exponent 2 -> $12.58.
        credits = by_label["Usage credits"]
        assert credits.kind == "credits"
        assert credits.show_pct is False
        assert credits.pct == 25.16
        assert credits.right == "$12.58 of $50.00"

    def test_extra_usage_credits_variant(self, tmp_path, monkeypatch):
        """The overage pool can also arrive via `extra_usage` (decimal_places / minor
        integer credits) instead of `spend` — a second, differently-shaped money case."""
        home = tmp_path / "claude_home"
        _write_credentials(home)
        payload = {
            "five_hour": {"utilization": 5.0, "resets_at": "2099-01-01T00:00:00Z"},
            "spend": {"enabled": False},
            "extra_usage": {
                "is_enabled": True,
                "decimal_places": 2,
                "currency": "USD",
                "used_credits": 733,
                "monthly_limit": 2000,
                "utilization": 36.65,
            },
        }

        def fake_urlopen(req, timeout=None):
            return _FakeResponse(json.dumps(payload).encode())

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        result = ClaudeUsageProvider().fetch(AgentConfig(home=str(home)))

        assert result.ok
        credits = next(r for r in result.rows if r.label == "Usage credits")
        assert credits.right == "$7.33 of $20.00"
        assert credits.pct == 36.65

    def test_scoped_model_row_appears_only_when_plan_exposes_it(self, tmp_path, monkeypatch):
        """Per-model weekly limits arrive as `limits[]` entries with kind
        "weekly_scoped" and a `scope.model.display_name` — there is no stable
        top-level key per model (observed live: the API hands out an obfuscated key,
        e.g. "nimbus_quill", for Fable). A model with no usage yet has `resets_at:
        null`, which should render as "Not used yet" rather than a reset time."""
        home = tmp_path / "claude_home"
        _write_credentials(home)
        payload = json.loads((FIXTURES / "claude_usage_official.json").read_text())
        payload["limits"].append(
            {"kind": "weekly_scoped", "percent": 0.0, "resets_at": None, "severity": "normal",
             "scope": {"model": {"display_name": "Fable"}}}
        )

        def fake_urlopen(req, timeout=None):
            return _FakeResponse(json.dumps(payload).encode())

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        result = ClaudeUsageProvider().fetch(AgentConfig(home=str(home)))

        opus = next(r for r in result.rows if r.label == "Weekly · Opus")
        assert opus.pct == 10.0
        assert opus.right != "Not used yet"

        fable = next(r for r in result.rows if r.label == "Weekly · Fable")
        assert fable.pct == 0.0
        assert fable.right == "Not used yet"


class TestClaudeFallback:
    def test_reconstructs_from_transcripts_when_endpoint_unreachable(self, tmp_path, monkeypatch):
        home = tmp_path / "claude_home"
        home.mkdir(parents=True)
        # No .credentials.json at all: _read_access_token raises OSError, which the
        # provider treats the same as any other "endpoint failed" case.

        now = datetime.now(UTC)
        content = _load_template(
            "claude_transcript.jsonl.template",
            TS_RECENT=_iso(now - timedelta(hours=1)),  # inside both 5h and 7d windows
            TS_MID_WEEK=_iso(now - timedelta(days=6)),  # inside 7d only
            TS_OLD=_iso(now - timedelta(days=10)),  # outside both
        )
        project_dir = home / "projects" / "proj1"
        project_dir.mkdir(parents=True)
        (project_dir / "session.jsonl").write_text(content, encoding="utf-8")

        result = ClaudeUsageProvider().fetch(AgentConfig(home=str(home)))

        assert result.ok
        assert result.source == "estimate"
        by_label = {row.label: row for row in result.rows}
        assert set(by_label) == {"5-hour", "This week"}
        for row in result.rows:
            assert row.show_pct is False
            assert row.pct == 0.0

        # 5-hour bucket sees only the one recent line; "This week" sees that one plus
        # the 6-day-old one, never the 10-day-old one.
        five_hour_tokens = float(by_label["5-hour"].right.split("M")[0])
        week_tokens = float(by_label["This week"].right.split("M")[0])
        assert five_hour_tokens == pytest.approx(0.72, abs=0.01)  # 720,000 tokens
        assert week_tokens == pytest.approx(1.44, abs=0.01)  # 2x 720,000 tokens

        five_hour_cost = float(by_label["5-hour"].right.split("$")[1])
        week_cost = float(by_label["This week"].right.split("$")[1])
        assert week_cost == pytest.approx(2 * five_hour_cost, rel=0.01)

    def test_no_transcripts_reports_clear_error(self, tmp_path):
        home = tmp_path / "claude_home"
        home.mkdir(parents=True)
        result = ClaudeUsageProvider().fetch(AgentConfig(home=str(home)))
        assert not result.ok
        assert result.source == "estimate"
        assert result.error

    @staticmethod
    def _write_transcript(home: Path, model: str, input_tokens: int) -> None:
        """One usage line, an hour old, so it lands in both the 5h and 7d windows."""
        project_dir = home / "projects" / "proj1"
        project_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps({
            "timestamp": _iso(datetime.now(UTC) - timedelta(hours=1)),
            "message": {"model": model, "usage": {"input_tokens": input_tokens,
                                                   "output_tokens": 0}},
        })
        (project_dir / "session.jsonl").write_text(line + "\n", encoding="utf-8")

    @staticmethod
    def _cost_of(result, label: str = "5-hour") -> float:
        row = next(r for r in result.rows if r.label == label)
        return float(row.right.split("$")[1])

    @pytest.mark.parametrize(
        ("model", "expected_cost"),
        [
            # 1M input tokens, so the cost in dollars *is* the input rate per MTok.
            ("claude-opus-5", 5.0),
            ("claude-sonnet-5", 2.0),
            ("claude-haiku-4-5-20251001", 1.0),  # a dated snapshot still resolves
            ("claude-fable-5", 10.0),
        ],
    )
    def test_current_models_are_priced(self, tmp_path, model, expected_cost):
        """Regression: `claude-opus-5` was absent from PRICING, and an unknown model
        defaulted to (0.0, 0.0) — so the model most sessions actually run contributed
        exactly nothing to the estimate. A plausible-but-silently-low total is the
        failure mode nobody notices, which is why this is pinned per model.
        """
        home = tmp_path / "claude_home"
        home.mkdir(parents=True)
        self._write_transcript(home, model, 1_000_000)

        result = ClaudeUsageProvider().fetch(AgentConfig(home=str(home)))

        assert result.ok
        assert self._cost_of(result) == pytest.approx(expected_cost, rel=0.01)
        assert "unknown model" not in (result.header or "")

    def test_an_unknown_model_is_estimated_and_says_so(self, tmp_path):
        """A model released after this build must not price at zero — it gets the
        flagship rate, and the header admits the rate was a guess."""
        home = tmp_path / "claude_home"
        home.mkdir(parents=True)
        self._write_transcript(home, "claude-something-99", 1_000_000)

        result = ClaudeUsageProvider().fetch(AgentConfig(home=str(home)))

        assert result.ok
        assert self._cost_of(result) > 0.0
        assert self._cost_of(result) == pytest.approx(5.0, rel=0.01)  # DEFAULT_PRICING
        assert "unknown model" in result.header

    def test_a_known_model_keeps_the_plain_estimate_header(self, tmp_path):
        home = tmp_path / "claude_home"
        home.mkdir(parents=True)
        self._write_transcript(home, "claude-opus-5", 1_000_000)

        result = ClaudeUsageProvider().fetch(AgentConfig(home=str(home)))

        assert result.header == "Claude usage — estimate (official % unavailable)"


class TestClaudeAuthAndRateLimit:
    def test_401_reports_expired_login_and_no_rows(self, tmp_path, monkeypatch):
        home = tmp_path / "claude_home"
        _write_credentials(home)

        def fake_urlopen(req, timeout=None):
            raise _http_error(401)

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        result = ClaudeUsageProvider().fetch(AgentConfig(home=str(home)))

        assert not result.ok
        assert result.rows == []
        assert result.error is not None
        assert "expired" in result.error
        assert "claude" in result.error.lower()

    def test_429_does_not_fall_back_to_estimate(self, tmp_path, monkeypatch):
        home = tmp_path / "claude_home"
        _write_credentials(home)
        # Even though local transcripts exist, a 429 must not trigger the JSONL
        # fallback — it should just report the rate limit with empty rows.
        project_dir = home / "projects" / "proj1"
        project_dir.mkdir(parents=True)
        now = datetime.now(UTC)
        (project_dir / "session.jsonl").write_text(
            _load_template(
                "claude_transcript.jsonl.template",
                TS_RECENT=_iso(now),
                TS_MID_WEEK=_iso(now),
                TS_OLD=_iso(now),
            ),
            encoding="utf-8",
        )

        def fake_urlopen(req, timeout=None):
            raise _http_error(429)

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        result = ClaudeUsageProvider().fetch(AgentConfig(home=str(home)))

        assert not result.ok
        assert result.rows == []
        assert result.source == "official"  # never downgraded to "estimate"
        assert "429" in result.error or "rate" in result.error.lower()

    def test_service_keeps_cached_rows_across_a_rate_limit(self, tmp_path, monkeypatch):
        """End-to-end: StatsService must not let a 429 blank out a previous good
        result — this is the behavior the provider-level test above exists to enable."""
        home = tmp_path / "claude_home"
        _write_credentials(home)
        cfg = Config()
        cfg.enabled_agents = ["claude"]
        cfg.agents["claude"] = AgentConfig(home=str(home))
        cache = UsageCache(path=tmp_path / "cache.json")
        service = StatsService(cfg, cache=cache, providers={"claude": ClaudeUsageProvider()})

        good_payload = json.loads((FIXTURES / "claude_usage_official.json").read_text())
        monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(
            json.dumps(good_payload).encode()
        ))
        first = service.fetch_all()["claude"]
        assert first.ok and first.source == "official"

        monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: (_ for _ in ()).throw(
            _http_error(429)
        ))
        second = service.fetch_all()["claude"]
        assert second.ok  # NOT blanked out
        assert second.source == "cache"
        assert second.rows == first.rows


# --------------------------------------------------------------------------- Codex


def _write_codex_session(
    home: Path,
    name: str,
    content: str,
    mtime: float | None = None,
    when: datetime | None = None,
) -> Path:
    """Write one rollout file into Codex's real `sessions/YYYY/MM/DD` layout.

    `when` defaults to today, because that is where a session Codex is actually writing
    to lands — and the provider prunes date directories whose whole range is outside the
    7-day window before it ever lists them.
    """
    day = (when or datetime.now(UTC)).astimezone()
    session_dir = home / "sessions" / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / name
    path.write_text(content, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _codex_token_record(ts: datetime, total: int, **extra) -> str:
    """One `token_count` line with a cumulative `total_token_usage` of `total`."""
    info = {"total_token_usage": {"input_tokens": total, "output_tokens": 0, "total_tokens": total}}
    payload = {"type": "token_count", "info": info, "rate_limits": {"primary": None, "secondary": None}}
    payload.update(extra)
    return json.dumps({"timestamp": _iso(ts), "type": "event_msg", "payload": payload})


class TestCodex:
    def test_populated_rate_limits_yield_percentage_rows(self, tmp_path):
        home = tmp_path / "codex_home"
        now = datetime.now(UTC)

        # An older file (still within the 7-day window) with a *lower* percentage —
        # this must lose to whichever file has the most recent token_count record.
        older_content = json.dumps(
            {
                "timestamp": _iso(now - timedelta(hours=2)),
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"total_tokens": 1000}},
                    "rate_limits": {"primary": {"used_percent": 20.0}, "secondary": None},
                },
            }
        )
        _write_codex_session(home, "rollout-older.jsonl", older_content)

        newer_content = _load_template(
            "codex_rollout_with_limits.jsonl.template",
            TS_OLDER=_iso(now - timedelta(hours=3)),
            TS_LATEST=_iso(now - timedelta(minutes=10)),
        )
        _write_codex_session(home, "rollout-newer.jsonl", newer_content)

        result = CodexUsageProvider().fetch(AgentConfig(home=str(home)))

        assert result.ok
        assert result.source == "official"
        by_label = {row.label: row for row in result.rows}
        # Labels come from each window's own `window_minutes` (300 / 10080 here), not
        # from its position in the payload — see `_window_label`.
        assert set(by_label) == {"5-hour limit", "Weekly limit"}
        # Must reflect the *newer* file's percentages (61.5 / 12.0), not the older
        # file's 20.0 — "most recent token_count record across all session files".
        assert by_label["5-hour limit"].pct == 61.5
        assert by_label["5-hour limit"].show_pct is True
        assert by_label["Weekly limit"].pct == 12.0

    def test_null_rate_limits_yield_token_total_rows(self, tmp_path):
        home = tmp_path / "codex_home"
        now = datetime.now(UTC)
        content = _load_template(
            "codex_rollout_no_limits.jsonl.template", TS_LATEST=_iso(now - timedelta(minutes=5))
        )
        _write_codex_session(home, "rollout-1.jsonl", content)

        result = CodexUsageProvider().fetch(AgentConfig(home=str(home)))

        assert result.ok
        assert result.source == "activity"
        by_label = {row.label: row for row in result.rows}
        assert set(by_label) == {"Last 5 hours", "Last 7 days"}
        for row in result.rows:
            assert row.show_pct is False
            assert row.kind == "info"
        assert "870" in by_label["Last 5 hours"].right or "0.87M" in by_label["Last 5 hours"].right

    def test_old_files_are_not_scanned(self, tmp_path):
        home = tmp_path / "codex_home"
        now = datetime.now(UTC)
        content = _load_template(
            "codex_rollout_with_limits.jsonl.template",
            TS_OLDER=_iso(now - timedelta(days=20)),
            TS_LATEST=_iso(now - timedelta(days=20)),
        )
        old_mtime = time.time() - 20 * 86400
        _write_codex_session(home, "rollout-ancient.jsonl", content, mtime=old_mtime)

        result = CodexUsageProvider().fetch(AgentConfig(home=str(home)))

        assert not result.ok
        assert "No recent Codex session" in result.error

    def test_missing_home_reports_clear_error_never_raises(self, tmp_path):
        result = CodexUsageProvider().fetch(AgentConfig(home=str(tmp_path / "does_not_exist")))
        assert not result.ok
        assert result.error

    def test_the_five_hour_row_counts_only_what_the_session_spent_in_those_hours(self, tmp_path):
        """`total_token_usage` is cumulative for the whole session.

        A session opened three days ago and touched ten minutes ago belongs in the
        5-hour window — but only for what it spent there. Adding its running total
        charged the 5h row with every token the session had ever used, which is how
        "Last 5 hours" ended up larger than "Last 7 days".
        """
        home = tmp_path / "codex_home"
        now = datetime.now(UTC)
        lines = [
            _codex_token_record(now - timedelta(hours=30), 1_000_000),   # long before the window
            _codex_token_record(now - timedelta(hours=6), 1_800_000),    # the baseline
            _codex_token_record(now - timedelta(minutes=10), 1_850_000),  # 50k spent inside 5h
        ]
        _write_codex_session(home, "rollout-long.jsonl", "\n".join(lines) + "\n")

        result = CodexUsageProvider().fetch(AgentConfig(home=str(home)))

        assert result.ok
        by_label = {row.label: row for row in result.rows}
        assert by_label["Last 5 hours"].right == "50k tokens"   # 1,850,000 - 1,800,000
        assert by_label["Last 7 days"].right == "1.85M tokens"  # the session's whole total

    def test_a_session_that_started_inside_the_window_counts_in_full(self, tmp_path):
        """No snapshot from before the cutoff means the running total IS the window."""
        home = tmp_path / "codex_home"
        now = datetime.now(UTC)
        lines = [
            _codex_token_record(now - timedelta(hours=1), 20_000),
            _codex_token_record(now - timedelta(minutes=2), 90_000),
        ]
        _write_codex_session(home, "rollout-fresh.jsonl", "\n".join(lines) + "\n")

        by_label = {r.label: r for r in CodexUsageProvider().fetch(AgentConfig(home=str(home))).rows}
        assert by_label["Last 5 hours"].right == "90k tokens"

    def test_a_session_last_touched_before_the_window_is_not_in_the_five_hour_row(self, tmp_path):
        home = tmp_path / "codex_home"
        now = datetime.now(UTC)
        lines = [
            _codex_token_record(now - timedelta(hours=30), 100_000),
            _codex_token_record(now - timedelta(hours=8), 400_000),
        ]
        _write_codex_session(home, "rollout-stale.jsonl", "\n".join(lines) + "\n")

        by_label = {r.label: r for r in CodexUsageProvider().fetch(AgentConfig(home=str(home))).rows}
        assert by_label["Last 5 hours"].right == "0k tokens"
        assert by_label["Last 7 days"].right == "400k tokens"

    def test_a_counter_that_goes_backwards_never_subtracts(self, tmp_path):
        """A resumed session can re-report from scratch; a negative delta would eat
        another session's usage out of the same bucket."""
        home = tmp_path / "codex_home"
        now = datetime.now(UTC)
        lines = [
            _codex_token_record(now - timedelta(hours=6), 900_000),
            _codex_token_record(now - timedelta(minutes=5), 10_000),
        ]
        _write_codex_session(home, "rollout-reset.jsonl", "\n".join(lines) + "\n")

        by_label = {r.label: r for r in CodexUsageProvider().fetch(AgentConfig(home=str(home))).rows}
        assert by_label["Last 5 hours"].right == "0k tokens"

    def test_an_unchanged_file_is_not_re_read_on_the_next_poll(self, tmp_path, monkeypatch):
        """The memo is the whole point of `_scan.FileMemo` here: over a WSL-split UNC
        path a re-read is a round trip per session file, every five minutes."""
        home = tmp_path / "codex_home"
        now = datetime.now(UTC)
        _write_codex_session(home, "rollout-1.jsonl",
                             _codex_token_record(now - timedelta(minutes=5), 5_000) + "\n")

        parsed: list[Path] = []
        real_scan = codex_mod._scan_tail
        monkeypatch.setattr(codex_mod, "_scan_tail",
                            lambda path: parsed.append(path) or real_scan(path))
        # A fresh memo, so this test does not depend on what an earlier one cached.
        monkeypatch.setattr(codex_mod, "_TAIL_MEMO", scan_mod.FileMemo())

        provider = CodexUsageProvider()
        assert provider.fetch(AgentConfig(home=str(home))).ok
        assert len(parsed) == 1
        assert provider.fetch(AgentConfig(home=str(home))).ok
        assert len(parsed) == 1, "an unchanged rollout file was parsed twice"

    def test_a_file_that_grew_is_re_read(self, tmp_path, monkeypatch):
        home = tmp_path / "codex_home"
        now = datetime.now(UTC)
        path = _write_codex_session(
            home, "rollout-1.jsonl", _codex_token_record(now - timedelta(minutes=9), 5_000) + "\n"
        )
        monkeypatch.setattr(codex_mod, "_TAIL_MEMO", scan_mod.FileMemo())
        provider = CodexUsageProvider()
        first = provider.fetch(AgentConfig(home=str(home)))
        assert first.ok

        with path.open("a", encoding="utf-8") as fh:
            fh.write(_codex_token_record(now - timedelta(minutes=1), 700_000) + "\n")
        os.utime(path, (time.time(), time.time()))

        by_label = {r.label: r for r in provider.fetch(AgentConfig(home=str(home))).rows}
        assert by_label["Last 7 days"].right == "700k tokens"

    def test_the_memo_forgets_files_that_left_the_window(self, tmp_path, monkeypatch):
        home = tmp_path / "codex_home"
        now = datetime.now(UTC)
        _write_codex_session(home, "rollout-1.jsonl",
                             _codex_token_record(now - timedelta(minutes=5), 1_000) + "\n")
        memo = scan_mod.FileMemo()
        monkeypatch.setattr(codex_mod, "_TAIL_MEMO", memo)

        assert CodexUsageProvider().fetch(AgentConfig(home=str(home))).ok
        assert len(memo) == 1

        stale = _write_codex_session(home, "rollout-2.jsonl",
                                     _codex_token_record(now - timedelta(minutes=5), 1_000) + "\n")
        assert CodexUsageProvider().fetch(AgentConfig(home=str(home))).ok
        assert len(memo) == 2

        old = time.time() - 30 * 86400
        os.utime(stale, (old, old))
        assert CodexUsageProvider().fetch(AgentConfig(home=str(home))).ok
        assert len(memo) == 1, "the memo kept a file that dropped out of the 7-day window"


# --------------------------------------------------------------------------- Codex date dirs


class TestCodexDateDirPruning:
    """`sessions/` gains a directory per day and never loses one.

    Globbing `**/rollout-*.jsonl` re-walked every day the user had ever run Codex on
    every 5-minute poll, over a UNC path where each directory is a round trip. The
    pruner has to be aggressive enough to matter and conservative enough never to drop a
    file whose mtime is inside the window.
    """

    def _skip(self, cutoff_days_ago: float = 7.0):
        root = Path("/codex/sessions")
        return codex_mod._make_skip_dir(root, time.time() - cutoff_days_ago * 86400), str(root)

    def test_a_year_entirely_before_the_window_is_pruned(self):
        skip, root = self._skip()
        assert skip(root, "2019") is True

    def test_the_current_year_month_and_day_are_never_pruned(self):
        skip, root = self._skip()
        today = datetime.now().astimezone()
        assert skip(root, f"{today.year:04d}") is False
        assert skip(f"{root}/{today.year:04d}", f"{today.month:02d}") is False
        assert skip(f"{root}/{today.year:04d}/{today.month:02d}", f"{today.day:02d}") is False

    def test_yesterday_survives_a_month_boundary(self):
        """The month directory of a day inside the window must not be pruned just
        because most of that month is outside it."""
        skip, root = self._skip()
        for days in range(0, 7):
            day = datetime.now().astimezone() - timedelta(days=days)
            year, month = f"{day.year:04d}", f"{day.month:02d}"
            assert skip(root, year) is False, day
            assert skip(f"{root}/{year}", month) is False, day
            assert skip(f"{root}/{year}/{month}", f"{day.day:02d}") is False, day

    def test_a_day_directory_keeps_a_grace_period(self):
        """Codex names the directory for when the session *started*; one left open
        overnight keeps appending, so its file's mtime outlives its directory's date."""
        skip, root = self._skip()
        edge = datetime.now().astimezone() - timedelta(days=8)
        parent = f"{root}/{edge.year:04d}/{edge.month:02d}"
        assert skip(parent, f"{edge.day:02d}") is False
        ancient = datetime.now().astimezone() - timedelta(days=40)
        assert skip(f"{root}/{ancient.year:04d}/{ancient.month:02d}", f"{ancient.day:02d}") is True

    @pytest.mark.parametrize("name", ["backup", "2019x", "19", "not-a-date", "202"])
    def test_a_directory_that_is_not_a_date_is_never_pruned(self, name):
        """A name we do not recognise might be anything; losing real sessions is far
        worse than walking a few extra directories."""
        skip, root = self._skip()
        assert skip(root, name) is False

    @pytest.mark.parametrize("parts,ok", [
        (["2020", "13"], False),   # month 13
        (["2020", "02", "31"], False),  # no such day
        (["2020", "12"], True),    # December rolls the year over, not the month
    ])
    def test_impossible_dates_are_not_pruned(self, parts, ok):
        assert (codex_mod._date_dir_end(parts) is not None) is ok

    def test_pruned_directories_are_never_listed(self, tmp_path):
        """End to end: an old date tree must not even be walked."""
        home = tmp_path / "codex_home"
        now = datetime.now(UTC)
        _write_codex_session(home, "rollout-today.jsonl",
                             _codex_token_record(now - timedelta(minutes=1), 1_000) + "\n")
        # A file with a *fresh* mtime sitting in a two-year-old date directory: it is
        # pruned by its path, which is exactly the trade the grace period bounds.
        ancient = home / "sessions" / "2019" / "01" / "02"
        ancient.mkdir(parents=True)
        (ancient / "rollout-ancient.jsonl").write_text(
            _codex_token_record(now, 9_000_000) + "\n", encoding="utf-8"
        )

        files = codex_mod._recent_session_files(home)
        assert [p.name for p in files] == ["rollout-today.jsonl"]


# --------------------------------------------------------------------------- Cursor


def _make_state_db(path: Path, token: str, as_json_string: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    try:
        con.execute("CREATE TABLE ItemTable (key TEXT UNIQUE, value TEXT)")
        stored = json.dumps(token) if as_json_string else token
        con.execute("INSERT INTO ItemTable (key, value) VALUES (?, ?)", ("cursorAuth/accessToken", stored))
        con.commit()
    finally:
        con.close()


class TestCursor:
    TOKEN = "super-secret-cursor-token-value"  # noqa: S105 - test fixture, not a real credential

    def test_success_parses_usage_and_never_logs_token(self, tmp_path, monkeypatch, caplog):
        db_path = tmp_path / "state.vscdb"
        _make_state_db(db_path, self.TOKEN)

        # Shape captured from a live GetCurrentPeriodUsage response, not invented: the
        # provider used to guess field names and collapsed both quotas into one row.
        payload = {
            "billingCycleStart": "1786533077000",
            "billingCycleEnd": "1789125077000",
            "planUsage": {
                "totalSpend": 2447,
                "includedSpend": 2000,
                "bonusSpend": 447,
                "limit": 2000,
                "autoPercentUsed": 5.437777777777778,
                "apiPercentUsed": 0,
                "totalPercentUsed": 4.943434343434343,
            },
        }
        seen_auth = {}

        def fake_urlopen(req, timeout=None):
            seen_auth["header"] = req.get_header("Authorization")
            return _FakeResponse(json.dumps(payload).encode())

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        caplog.set_level(logging.DEBUG)
        result = CursorUsageProvider().fetch(AgentConfig(state_db=str(db_path)))

        assert result.ok
        assert seen_auth["header"] == f"Bearer {self.TOKEN}"

        # Two quotas, exactly as Cursor's own usage screen shows them.
        assert [r.label for r in result.rows] == ["Cursor Models", "Other Models"]
        cursor_models, other_models = result.rows
        assert cursor_models.pct == pytest.approx(5.4377, abs=1e-3)
        assert other_models.pct == 0.0
        assert all(r.show_pct for r in result.rows)
        # The two must not be conflated: totalPercentUsed (4.94) is a third figure and
        # must never stand in for either bar.
        assert cursor_models.pct != other_models.pct

        assert "Resets" in cursor_models.right
        assert other_models.right == "$20.00 included"

        assert self.TOKEN not in caplog.text

    def test_not_signed_in_when_db_missing(self, tmp_path):
        result = CursorUsageProvider().fetch(AgentConfig(state_db=str(tmp_path / "nope.vscdb")))
        assert not result.ok
        assert "not signed in" in result.error.lower()

    def test_locked_or_corrupt_db_degrades_cleanly(self, tmp_path):
        bad_db = tmp_path / "corrupt.vscdb"
        bad_db.write_bytes(b"this is not a sqlite database at all")
        result = CursorUsageProvider().fetch(AgentConfig(state_db=str(bad_db)))
        assert not result.ok
        assert result.error  # some clear message, not an exception

    def test_unrecognised_response_shape_is_an_error_not_an_exception(self, tmp_path, monkeypatch):
        db_path = tmp_path / "state.vscdb"
        _make_state_db(db_path, self.TOKEN)
        monkeypatch.setattr(
            urllib.request, "urlopen",
            lambda req, timeout=None: _FakeResponse(json.dumps({"totally": "unexpected"}).encode()),
        )
        result = CursorUsageProvider().fetch(AgentConfig(state_db=str(db_path)))
        assert not result.ok
        assert "unofficial endpoint changed" in result.error

    def test_401_triggers_one_token_reread_and_retry(self, tmp_path, monkeypatch):
        db_path = tmp_path / "state.vscdb"
        _make_state_db(db_path, self.TOKEN)
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _http_error(401)
            return _FakeResponse(json.dumps({"planUsage": {"autoPercentUsed": 5.0}}).encode())

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        result = CursorUsageProvider().fetch(AgentConfig(state_db=str(db_path)))

        assert result.ok
        assert calls["n"] == 2

    def test_bare_string_token_storage_also_works(self, tmp_path, monkeypatch):
        """Cursor has stored the token both JSON-quoted and as a bare string across
        versions — the reader must handle either."""
        db_path = tmp_path / "state.vscdb"
        _make_state_db(db_path, self.TOKEN, as_json_string=False)
        seen = {}
        monkeypatch.setattr(
            urllib.request, "urlopen",
            lambda req, timeout=None: (seen.setdefault("auth", req.get_header("Authorization")),
                                        _FakeResponse(json.dumps({"planUsage": {"autoPercentUsed": 1.0}}).encode()))[1],
        )
        result = CursorUsageProvider().fetch(AgentConfig(state_db=str(db_path)))
        assert result.ok
        assert seen["auth"] == f"Bearer {self.TOKEN}"


# --------------------------------------------------------------------------- JetBrains


# Field names and the escaped-XML shape are copied verbatim from a real
# AIAssistantQuotaManager2.xml (WebStorm 2026.2) rather than invented — the encoding
# (`&#10;`/`&quot;` wrapping a JSON string) is exactly what trips up a naive parser.
_JETBRAINS_DEFAULTS = {
    "NEXT": "2026-08-16T10:00:11.989Z",
    "CURRENT": "161185.755",
    "MAXIMUM": "5498808.015",
    "UNTIL": "2027-03-19T21:00:00Z",
    "TARIFF_CURRENT": "161185.755",
    "TARIFF_MAX": "1000000",
    "TARIFF_AVAIL": "838814.245",
    "TOPUP_CURRENT": "0",
    "TOPUP_MAX": "4498808.015",
    "TOPUP_AVAIL": "4498808.015",
}


def _write_quota_file(path: Path, **overrides: str) -> Path:
    subs = {**_JETBRAINS_DEFAULTS, **overrides}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_load_template("jetbrains_quota.xml.template", **subs), encoding="utf-8")
    return path


class TestJetBrains:
    def test_success_parses_both_quotas(self, tmp_path):
        path = _write_quota_file(tmp_path / "AIAssistantQuotaManager2.xml")
        result = JetBrainsUsageProvider().fetch(AgentConfig(quota_path=str(path)))

        assert result.ok
        assert [r.label for r in result.rows] == ["Monthly Credits", "Top-up Credits"]
        included, top_up = result.rows
        # 161185.755 / 1000000 * 100
        assert included.pct == pytest.approx(16.1186, abs=1e-3)
        assert included.severity == "normal"
        assert included.show_pct is False
        # Local-time day/month, not the raw UTC date — matches how the provider
        # formats it (astimezone()), so this must not hardcode a runner's timezone.
        reset_dt = datetime.fromisoformat(_JETBRAINS_DEFAULTS["NEXT"].replace("Z", "+00:00")).astimezone()
        # 161185.755 / 100_000 -- CREDIT_SCALE, reverse-engineered from the IDE's own
        # widget (see the provider's module docstring) -- shown as *used* credit
        # points, matching every other provider's percentage-of-usage convention
        # (Claude's "Usage credits" row, Copilot's quota rows) rather than "left".
        assert included.right == f"1.61 / 10.00 used · {reset_dt.day} {reset_dt.strftime('%b')}"
        assert top_up.pct == 0.0
        assert top_up.show_pct is False
        # 4498808.015 / 100_000 -- CREDIT_SCALE, reverse-engineered from the IDE's own
        # widget (see the provider's module docstring).
        assert top_up.right == "44.99 credits available"

    def test_matches_a_real_ide_widget_reading(self, tmp_path):
        """Values captured from a live PyCharm 2026.2 quota file at the exact moment
        its AI Assistant widget showed "8.27 / 10.00 monthly credits left" and
        "44.99 top-up credits" — the strongest evidence for the CREDIT_SCALE
        conversion, so pinned here as a regression guard."""
        path = _write_quota_file(
            tmp_path / "AIAssistantQuotaManager2.xml",
            CURRENT="172494.4", TARIFF_CURRENT="172494.4", TARIFF_AVAIL="827505.6",
        )
        result = JetBrainsUsageProvider().fetch(AgentConfig(quota_path=str(path)))

        assert result.ok
        included, top_up = result.rows
        # The screenshot and the quota-file read weren't taken at the exact same
        # instant (usage accrues continuously), so this allows a little drift while
        # still pinning the CREDIT_SCALE conversion to two significant digits.
        remaining_credits = 10.0 - included.pct / 100 * 10.0
        assert remaining_credits == pytest.approx(8.27, abs=0.01)
        assert included.right.startswith("1.72 / 10.00 used")
        assert top_up.right == "44.99 credits available"

    def test_quota_path_accepts_an_ide_directory_not_just_the_file(self, tmp_path):
        ide_dir = tmp_path / "PyCharm2026.2"
        _write_quota_file(ide_dir / "options" / "AIAssistantQuotaManager2.xml")
        result = JetBrainsUsageProvider().fetch(AgentConfig(quota_path=str(ide_dir)))
        assert result.ok

    def test_top_up_row_omitted_when_no_top_up_balance_exists(self, tmp_path):
        path = _write_quota_file(tmp_path / "quota.xml", TOPUP_MAX="0", TOPUP_CURRENT="0")
        result = JetBrainsUsageProvider().fetch(AgentConfig(quota_path=str(path)))
        assert result.ok
        assert [r.label for r in result.rows] == ["Monthly Credits"]

    def test_high_usage_is_flagged_critical(self, tmp_path):
        path = _write_quota_file(tmp_path / "quota.xml", TARIFF_CURRENT="950000")
        result = JetBrainsUsageProvider().fetch(AgentConfig(quota_path=str(path)))
        assert result.rows[0].severity == "critical"

    def test_missing_file_degrades_cleanly(self, tmp_path):
        result = JetBrainsUsageProvider().fetch(AgentConfig(quota_path=str(tmp_path / "nope.xml")))
        assert not result.ok
        assert result.error

    def test_corrupt_xml_degrades_cleanly(self, tmp_path):
        path = tmp_path / "quota.xml"
        path.write_text("this is not xml at all <<<", encoding="utf-8")
        result = JetBrainsUsageProvider().fetch(AgentConfig(quota_path=str(path)))
        assert not result.ok
        assert result.error

    def test_missing_quota_info_degrades_cleanly(self, tmp_path):
        path = tmp_path / "quota.xml"
        path.write_text(
            '<application><component name="AIAssistantQuotaManager2"></component></application>',
            encoding="utf-8",
        )
        result = JetBrainsUsageProvider().fetch(AgentConfig(quota_path=str(path)))
        assert not result.ok
        assert result.error

    def test_auto_detect_picks_the_most_recently_synced_ide(self, tmp_path, monkeypatch):
        """Every installed IDE keeps its own copy of the account-wide quota, synced
        only when that IDE last talked to the JetBrains AI service — the newest file
        on disk is the freshest signal available, so it must win over an older one
        even if the older one alphabetically sorts first."""
        monkeypatch.setattr(jetbrains_mod, "_default_jetbrains_root", lambda: tmp_path)

        old_path = _write_quota_file(
            tmp_path / "AWebStorm2025.1" / "options" / "AIAssistantQuotaManager2.xml",
            TARIFF_CURRENT="1000",
        )
        os.utime(old_path, (time.time() - 3600, time.time() - 3600))

        new_path = _write_quota_file(
            tmp_path / "ZPyCharm2026.2" / "options" / "AIAssistantQuotaManager2.xml",
            TARIFF_CURRENT="500000",
        )
        os.utime(new_path, (time.time(), time.time()))

        assert jetbrains_mod.detect() is True
        result = JetBrainsUsageProvider().fetch(AgentConfig())
        assert result.ok
        assert result.rows[0].pct == pytest.approx(50.0, abs=1e-3)

    def test_detect_false_when_root_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(jetbrains_mod, "_default_jetbrains_root", lambda: tmp_path / "nope")
        assert jetbrains_mod.detect() is False


# --------------------------------------------------------------------------- Copilot


def _make_copilot_db(path: Path, events: list[tuple[str, int, int, str]]) -> None:
    """`events` is `[(model, input_tokens, output_tokens, created_at), ...]` — the
    columns this provider actually reads, out of the full real table (verified
    against a live `~/.copilot/session-store.db`, which carries many more)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    try:
        con.execute(
            "CREATE TABLE assistant_usage_events ("
            "model TEXT, input_tokens INTEGER, output_tokens INTEGER, created_at TEXT)"
        )
        con.executemany(
            "INSERT INTO assistant_usage_events (model, input_tokens, output_tokens, created_at) "
            "VALUES (?, ?, ?, ?)",
            events,
        )
        con.commit()
    finally:
        con.close()


def _deny_copilot_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Forces the quota path to fail fast, as it always does off Windows, so the
    token-totals fallback tests exercise that fallback deterministically instead of
    depending on whatever's really sitting in a CI runner's credential store."""

    def _raise() -> str:
        raise copilot_mod._TokenError("no token in this test")

    monkeypatch.setattr(copilot_mod, "_read_copilot_token", _raise)


class TestCopilot:
    def test_sums_tokens_per_model_within_the_window(self, tmp_path, monkeypatch):
        _deny_copilot_token(monkeypatch)
        home = tmp_path / "copilot_home"
        now = datetime.now(UTC)
        _make_copilot_db(home / "session-store.db", [
            ("gpt-5-mini", 30261, 1002, _iso(now - timedelta(hours=1))),
            ("gpt-5-mini", 27931, 435, _iso(now - timedelta(days=2))),
            ("claude-sonnet-4.5", 5000, 500, _iso(now - timedelta(days=6))),
            # Outside the 7-day window: must not be counted.
            ("gpt-5-mini", 999_999, 999_999, _iso(now - timedelta(days=8))),
        ])

        result = CopilotUsageProvider().fetch(AgentConfig(home=str(home)))

        assert result.ok
        assert result.source == "activity"
        by_label = {row.label: row for row in result.rows}
        assert set(by_label) == {"gpt-5-mini", "claude-sonnet-4.5"}
        for row in result.rows:
            assert row.show_pct is False
            assert row.kind == "info"
            assert row.pct == 0.0
        # (30261+1002) + (27931+435) = 59629 -> "59.6k tokens"
        assert by_label["gpt-5-mini"].right == "59.6k tokens"
        assert by_label["claude-sonnet-4.5"].right == "5.5k tokens"
        # Ranked by usage, largest first.
        assert [r.label for r in result.rows] == ["gpt-5-mini", "claude-sonnet-4.5"]

    def test_caps_at_five_models(self, tmp_path, monkeypatch):
        _deny_copilot_token(monkeypatch)
        home = tmp_path / "copilot_home"
        now = datetime.now(UTC)
        events = [(f"model-{i}", 1000 * (i + 1), 0, _iso(now)) for i in range(7)]
        _make_copilot_db(home / "session-store.db", events)

        result = CopilotUsageProvider().fetch(AgentConfig(home=str(home)))
        assert result.ok
        assert len(result.rows) == 5
        # The five highest-usage models, not just the first five inserted.
        assert result.rows[0].label == "model-6"

    def test_missing_database_degrades_cleanly(self, tmp_path, monkeypatch):
        _deny_copilot_token(monkeypatch)
        result = CopilotUsageProvider().fetch(AgentConfig(home=str(tmp_path / "nope")))
        assert not result.ok
        assert result.error

    def test_corrupt_database_degrades_cleanly(self, tmp_path, monkeypatch):
        _deny_copilot_token(monkeypatch)
        home = tmp_path / "copilot_home"
        home.mkdir(parents=True)
        (home / "session-store.db").write_bytes(b"not a sqlite database")
        result = CopilotUsageProvider().fetch(AgentConfig(home=str(home)))
        assert not result.ok
        assert result.error

    def test_no_recent_activity_degrades_cleanly(self, tmp_path, monkeypatch):
        _deny_copilot_token(monkeypatch)
        home = tmp_path / "copilot_home"
        now = datetime.now(UTC)
        _make_copilot_db(home / "session-store.db", [
            ("gpt-5-mini", 100, 100, _iso(now - timedelta(days=30))),
        ])
        result = CopilotUsageProvider().fetch(AgentConfig(home=str(home)))
        assert not result.ok
        assert result.error

    def test_detect(self, tmp_path, monkeypatch):
        monkeypatch.setattr(copilot_mod, "_default_home", lambda: tmp_path / "nope")
        assert copilot_mod.detect() is False

        home = tmp_path / "copilot_home"
        _make_copilot_db(home / "session-store.db", [])
        monkeypatch.setattr(copilot_mod, "_default_home", lambda: home)
        assert copilot_mod.detect() is True

    # --------------------------------------------------------------- real quota endpoint

    def test_quota_parses_real_endpoint_shape_and_never_logs_token(self, monkeypatch, caplog):
        """Payload shape captured verbatim from a live `copilot_internal/user`
        response (see the provider's module docstring) — a free-plan account with
        `premium_interactions.has_quota: false`, which must be omitted rather than
        shown as a 100%-used row."""
        token = "gho_super-secret-live-oauth-token"  # noqa: S105 - test fixture, not a real credential
        reset_at = datetime.now(UTC) + timedelta(days=17, hours=2)
        payload = {
            "login": "GomelHawk",
            "access_type_sku": "free_limited_copilot",
            "copilot_plan": "individual",
            "quota_reset_date_utc": _iso(reset_at),
            "quota_snapshots": {
                "chat": {
                    "has_quota": True, "unlimited": False, "percent_remaining": 97.4,
                    "quota_remaining": 194.8, "entitlement": 200, "remaining": 194, "credits_used": 5,
                },
                "completions": {
                    "has_quota": True, "unlimited": False, "percent_remaining": 100.0,
                    "entitlement": 2000, "remaining": 2000,
                },
                "premium_interactions": {
                    "has_quota": False, "unlimited": False, "percent_remaining": 0.0, "entitlement": 0,
                },
            },
        }
        monkeypatch.setattr(copilot_mod, "_read_copilot_token", lambda: token)
        seen_auth = {}

        def fake_urlopen(req, timeout=None):
            seen_auth["header"] = req.get_header("Authorization")
            return _FakeResponse(json.dumps(payload).encode())

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        caplog.set_level(logging.DEBUG)
        result = CopilotUsageProvider().fetch(AgentConfig())

        assert result.ok
        assert result.source == "official"
        assert result.header == "GitHub Copilot — Copilot Free"
        assert seen_auth["header"] == f"token {token}"

        # premium_interactions (has_quota: false) must be omitted, not shown as 100% used.
        assert [r.label for r in result.rows] == ["Chat messages", "Code completions"]
        chat, completions = result.rows
        assert chat.pct == pytest.approx(2.6, abs=1e-6)  # 100 - 97.4
        assert chat.show_pct is True
        assert chat.right == "Resets in 18d"
        assert chat.severity == "normal"
        assert completions.pct == 0.0
        assert completions.right == "Resets in 18d"

        assert token not in caplog.text

    def test_quota_unlimited_snapshot_shown_without_a_percentage(self, monkeypatch):
        payload = {
            "quota_reset_date_utc": _iso(datetime.now(UTC) + timedelta(days=5)),
            "quota_snapshots": {
                "chat": {"has_quota": True, "unlimited": True, "percent_remaining": 100.0},
            },
        }
        monkeypatch.setattr(copilot_mod, "_read_copilot_token", lambda: "gho_x")  # noqa: S106
        monkeypatch.setattr(
            urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(json.dumps(payload).encode())
        )
        result = CopilotUsageProvider().fetch(AgentConfig())
        assert result.ok
        assert result.rows[0].right == "Unlimited"
        assert result.rows[0].show_pct is False
        assert result.rows[0].kind == "info"

    def test_quota_high_usage_is_flagged_critical(self, monkeypatch):
        payload = {
            "quota_reset_date_utc": _iso(datetime.now(UTC) + timedelta(days=1)),
            "quota_snapshots": {"chat": {"has_quota": True, "unlimited": False, "percent_remaining": 5.0}},
        }
        monkeypatch.setattr(copilot_mod, "_read_copilot_token", lambda: "gho_x")  # noqa: S106
        monkeypatch.setattr(
            urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(json.dumps(payload).encode())
        )
        result = CopilotUsageProvider().fetch(AgentConfig())
        assert result.rows[0].severity == "critical"

    def test_quota_falls_back_to_token_totals_on_malformed_response(self, tmp_path, monkeypatch):
        """A response with no recognisable `quota_snapshots` (endpoint shape changed,
        or an org account without the field seen on a personal one) must degrade to
        the local totals, not surface a blank/broken quota card."""
        home = tmp_path / "copilot_home"
        _make_copilot_db(home / "session-store.db", [("gpt-5-mini", 100, 50, _iso(datetime.now(UTC)))])
        monkeypatch.setattr(copilot_mod, "_read_copilot_token", lambda: "gho_x")  # noqa: S106
        monkeypatch.setattr(
            urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(b'{"unexpected": true}')
        )
        result = CopilotUsageProvider().fetch(AgentConfig(home=str(home)))
        assert result.ok
        assert result.source == "activity"

    def test_quota_falls_back_to_token_totals_when_endpoint_errors(self, tmp_path, monkeypatch):
        home = tmp_path / "copilot_home"
        _make_copilot_db(home / "session-store.db", [("gpt-5-mini", 100, 50, _iso(datetime.now(UTC)))])
        monkeypatch.setattr(copilot_mod, "_read_copilot_token", lambda: "gho_x")  # noqa: S106
        monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: (_ for _ in ()).throw(_http_error(401)))
        result = CopilotUsageProvider().fetch(AgentConfig(home=str(home)))
        assert result.ok
        assert result.source == "activity"

    def test_quota_skipped_off_windows_falls_back_to_token_totals(self, tmp_path):
        """No monkeypatching of `_read_copilot_token` itself here — this is the real
        function, which must refuse to touch Windows-only APIs on any other
        platform."""
        home = tmp_path / "copilot_home"
        _make_copilot_db(home / "session-store.db", [("gpt-5-mini", 100, 50, _iso(datetime.now(UTC)))])
        result = CopilotUsageProvider().fetch(AgentConfig(home=str(home)))
        assert result.ok
        if sys.platform != "win32":
            assert result.source == "activity"


# --------------------------------------------------------------------------- cache


class TestUsageCache:
    def test_round_trip(self, tmp_path):
        path = tmp_path / "usage_cache.json"
        result = UsageResult(
            agent="claude",
            rows=[UsageRow(label="5-hour limit", pct=42.0, right="Resets in 1 hr", kind="limit")],
            header="Your usage limits",
            source="official",
        )
        UsageCache(path=path).update(result)

        reloaded = UsageCache(path=path)
        fetched = reloaded.get("claude")
        assert fetched is not None
        assert fetched.to_dict() == result.to_dict()
        assert reloaded.get("codex") is None

    def test_corrupt_file_is_tolerated(self, tmp_path):
        path = tmp_path / "usage_cache.json"
        path.write_text("{not valid json at all", encoding="utf-8")
        cache = UsageCache(path=path)  # must not raise
        assert cache.get("claude") is None
        # And it must still be writable afterwards.
        cache.update(UsageResult(agent="claude", rows=[UsageRow(label="x", pct=1.0)]))
        assert UsageCache(path=path).get("claude") is not None

    def test_partially_corrupt_file_keeps_good_entries(self, tmp_path):
        path = tmp_path / "usage_cache.json"
        path.write_text(json.dumps({"claude": {"agent": "claude", "rows": []}, "codex": "not-a-dict"}),
                         encoding="utf-8")
        cache = UsageCache(path=path)
        assert cache.get("claude") is not None
        assert cache.get("codex") is None


    def test_update_many_persists_every_result_in_one_write(self, tmp_path, monkeypatch):
        """One file rewrite per poll, not one per provider.

        `StatsService` used to call `update()` from each provider's thread, so a
        five-provider poll produced five temp files and five `os.replace` calls on the
        same path — five chances for a reader to catch a half-finished set.
        """
        path = tmp_path / "usage_cache.json"
        cache = UsageCache(path=path)
        saves: list[int] = []
        real_save = cache._save
        monkeypatch.setattr(cache, "_save", lambda: saves.append(1) or real_save())

        cache.update_many([
            UsageResult(agent="claude", rows=[UsageRow(label="a", pct=1.0)]),
            UsageResult(agent="codex", rows=[UsageRow(label="b", pct=2.0)]),
        ])

        assert saves == [1]
        reloaded = UsageCache(path=path)
        assert reloaded.get("claude") is not None
        assert reloaded.get("codex") is not None

    def test_update_many_with_nothing_to_store_writes_nothing(self, tmp_path):
        path = tmp_path / "usage_cache.json"
        UsageCache(path=path).update_many([])
        assert not path.exists()


# --------------------------------------------------------------------------- service


class _FakeProvider(UsageProvider):
    def __init__(self, key: str, fn) -> None:
        self.key = key
        self._fn = fn

    def fetch(self, agent_config, timeout: float = 15.0) -> UsageResult:
        return self._fn(agent_config, timeout)


class TestStatsService:
    def _cfg(self, agents: list[str]) -> Config:
        cfg = Config()
        cfg.enabled_agents = list(agents)
        for a in agents:
            cfg.agents[a] = AgentConfig()
        return cfg

    def test_fetches_each_agent_in_parallel(self, tmp_path):
        def slow_ok(agent_config, timeout):
            time.sleep(0.3)
            return UsageResult(agent="claude", rows=[UsageRow(label="x", pct=1.0)])

        cfg = self._cfg(["claude", "codex"])
        service = StatsService(
            cfg,
            cache=UsageCache(path=tmp_path / "cache.json"),
            providers={
                "claude": _FakeProvider("claude", slow_ok),
                "codex": _FakeProvider("codex", slow_ok),
            },
        )
        started = time.perf_counter()
        results = service.fetch_all()
        elapsed = time.perf_counter() - started

        assert set(results) == {"claude", "codex"}
        assert all(r.ok for r in results.values())
        # Two 0.3s providers run in parallel should finish well under their sum.
        assert elapsed < 0.55

    def test_a_slow_or_broken_provider_does_not_block_others(self, tmp_path):
        def raises(agent_config, timeout):
            raise RuntimeError("boom")

        def ok(agent_config, timeout):
            return UsageResult(agent="codex", rows=[UsageRow(label="x", pct=1.0)])

        cfg = self._cfg(["claude", "codex"])
        service = StatsService(
            cfg,
            cache=UsageCache(path=tmp_path / "cache.json"),
            providers={"claude": _FakeProvider("claude", raises), "codex": _FakeProvider("codex", ok)},
        )
        results = service.fetch_all()
        assert results["codex"].ok
        assert not results["claude"].ok
        assert results["claude"].error

    def test_cache_policy_keeps_last_good_result_on_failure(self, tmp_path):
        state = {"mode": "ok"}

        def flaky(agent_config, timeout):
            if state["mode"] == "ok":
                return UsageResult(agent="claude", rows=[UsageRow(label="x", pct=7.0)], source="official")
            return UsageResult(agent="claude", error="temporary failure")

        cfg = self._cfg(["claude"])
        cache = UsageCache(path=tmp_path / "cache.json")
        service = StatsService(cfg, cache=cache, providers={"claude": _FakeProvider("claude", flaky)})

        first = service.fetch_all()["claude"]
        assert first.ok and first.source == "official"

        state["mode"] = "fail"
        second = service.fetch_all()["claude"]
        assert second.ok
        assert second.source == "cache"
        assert second.rows == first.rows
        assert service.latest("claude") == second

    def test_no_enabled_agents_returns_empty(self, tmp_path):
        cfg = self._cfg([])
        service = StatsService(cfg, cache=UsageCache(path=tmp_path / "cache.json"), providers={})
        assert service.fetch_all() == {}

    def test_result_order_matches_configured_order_not_completion_order(self, tmp_path):
        # codex is made to finish first (no sleep) while claude finishes last (a
        # sleep) — if `fetch_all` returned results in completion order, "codex" would
        # land before "claude" here despite "claude" being configured first.
        def fast(agent_config, timeout):
            return UsageResult(agent="codex", rows=[UsageRow(label="x", pct=1.0)])

        def slow(agent_config, timeout):
            time.sleep(0.2)
            return UsageResult(agent="claude", rows=[UsageRow(label="x", pct=1.0)])

        def medium(agent_config, timeout):
            time.sleep(0.1)
            return UsageResult(agent="cursor", rows=[UsageRow(label="x", pct=1.0)])

        cfg = self._cfg(["claude", "codex", "cursor"])
        service = StatsService(
            cfg,
            cache=UsageCache(path=tmp_path / "cache.json"),
            providers={
                "claude": _FakeProvider("claude", slow),
                "codex": _FakeProvider("codex", fast),
                "cursor": _FakeProvider("cursor", medium),
            },
        )
        results = service.fetch_all()
        assert list(results) == ["claude", "codex", "cursor"]

    def test_unconfigured_provider_key_is_skipped(self, tmp_path):
        cfg = self._cfg(["claude", "unknown-agent"])
        service = StatsService(
            cfg,
            cache=UsageCache(path=tmp_path / "cache.json"),
            providers={"claude": _FakeProvider("claude", lambda c, t: UsageResult(agent="claude", rows=[
                UsageRow(label="x", pct=1.0)
            ]))},
        )
        results = service.fetch_all()
        assert set(results) == {"claude"}

    def test_a_poll_rewrites_the_cache_file_once(self, tmp_path, monkeypatch):
        """Not once per provider — see `TestUsageCache.test_update_many_...`."""
        def ok(agent_config, timeout):
            return UsageResult(agent="x", rows=[UsageRow(label="x", pct=1.0)], source="official")

        cache = UsageCache(path=tmp_path / "cache.json")
        saves: list[int] = []
        real_save = cache._save
        monkeypatch.setattr(cache, "_save", lambda: saves.append(1) or real_save())

        cfg = self._cfg(["claude", "codex", "cursor"])
        service = StatsService(cfg, cache=cache, providers={
            k: _FakeProvider(k, ok) for k in ("claude", "codex", "cursor")
        })
        service.fetch_all()

        assert saves == [1], f"{len(saves)} cache writes for one poll"

    def test_a_failed_provider_does_not_rewrite_the_cache_with_its_own_cached_copy(
        self, tmp_path, monkeypatch
    ):
        """A `source="cache"` result is what was already on disk — writing it back is
        pure I/O for no change, and this poll runs every five minutes forever."""
        state = {"mode": "ok"}

        def flaky(agent_config, timeout):
            if state["mode"] == "ok":
                return UsageResult(agent="claude", rows=[UsageRow(label="x", pct=7.0)], source="official")
            return UsageResult(agent="claude", error="temporary failure")

        cache = UsageCache(path=tmp_path / "cache.json")
        cfg = self._cfg(["claude"])
        service = StatsService(cfg, cache=cache, providers={"claude": _FakeProvider("claude", flaky)})
        service.fetch_all()

        saves: list[int] = []
        real_save = cache._save
        monkeypatch.setattr(cache, "_save", lambda: saves.append(1) or real_save())
        state["mode"] = "fail"
        assert service.fetch_all()["claude"].source == "cache"
        assert saves == []

    def test_the_poll_never_creates_an_agent_table(self, tmp_path):
        """`Config.agent()` inserts as a side effect of being asked, from whichever
        stats thread got there first — while `dumps()` iterates that same dict on the
        GUI thread during a settings save. The poll must use `agent_config()`."""
        cfg = Config()
        cfg.enabled_agents = ["claude", "codex"]
        assert cfg.agents == {}

        service = StatsService(
            cfg,
            cache=UsageCache(path=tmp_path / "cache.json"),
            providers={
                k: _FakeProvider(k, lambda c, t: UsageResult(agent="x", rows=[UsageRow(label="x", pct=1.0)]))
                for k in ("claude", "codex")
            },
        )
        service.fetch_all()

        assert cfg.agents == {}, "the stats poll mutated the config"


def test_codex_window_label_follows_window_minutes():
    """`primary` is a position in the payload, not a fixed duration.

    A free plan reports a single `primary` window of 43200 minutes (30 days). Labelling
    that "5-hour limit" — as this did until a real payload showed otherwise — makes a
    nearly-exhausted monthly budget look like one that clears over lunch.
    """
    from tintaview.stats.providers.codex import _fmt_reset, _pct_row, _window_label

    monthly = {"used_percent": 92.0, "window_minutes": 43200, "resets_at": 1789125090}
    row = _pct_row(_window_label(monthly, "5-hour limit"), monthly)
    assert row.label == "Monthly limit"
    assert row.pct == 92.0
    assert row.severity == "critical"

    # resets_at arrives as a Unix epoch int, which `datetime.fromisoformat` rejects;
    # it used to fall through and print the raw number where a usage figure belongs.
    assert "1789125090" not in row.right
    assert row.right.startswith("Resets ")

    assert _window_label({"window_minutes": 300}, "x") == "5-hour limit"
    assert _window_label({"window_minutes": 10080}, "x") == "Weekly limit"
    assert _window_label({"window_minutes": 1440}, "x") == "Daily limit"
    # Unusable/absent values keep the caller's fallback rather than inventing a label.
    assert _window_label({}, "5-hour limit") == "5-hour limit"
    assert _window_label({"window_minutes": 0}, "5-hour limit") == "5-hour limit"
    assert _window_label({"window_minutes": True}, "5-hour limit") == "5-hour limit"

    # A reset weeks away must carry a date, not a bare weekday.
    assert _fmt_reset(resets_in_seconds=30 * 86400).startswith("Resets ")
    assert any(ch.isdigit() for ch in _fmt_reset(resets_in_seconds=30 * 86400))
