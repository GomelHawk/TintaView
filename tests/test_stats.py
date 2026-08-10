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
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tintaview.core.config import AgentConfig, Config
from tintaview.stats.cache import UsageCache
from tintaview.stats.model import UsageProvider, UsageResult, UsageRow
from tintaview.stats.providers.claude import ClaudeUsageProvider
from tintaview.stats.providers.codex import CodexUsageProvider
from tintaview.stats.providers.cursor import CursorUsageProvider
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


def _write_codex_session(home: Path, name: str, content: str, mtime: float | None = None) -> Path:
    session_dir = home / "sessions" / "2026" / "07" / "24"
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / name
    path.write_text(content, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


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
        assert set(by_label) == {"5-hour limit", "Weekly"}
        # Must reflect the *newer* file's percentages (61.5 / 12.0), not the older
        # file's 20.0 — "most recent token_count record across all session files".
        assert by_label["5-hour limit"].pct == 61.5
        assert by_label["5-hour limit"].show_pct is True
        assert by_label["Weekly"].pct == 12.0

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

        payload = {"usage": {"currentPeriod": {"usedCents": 1234, "limitCents": 5000, "percentUsed": 24.68}}}
        seen_auth = {}

        def fake_urlopen(req, timeout=None):
            seen_auth["header"] = req.get_header("Authorization")
            return _FakeResponse(json.dumps(payload).encode())

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        caplog.set_level(logging.DEBUG)
        result = CursorUsageProvider().fetch(AgentConfig(state_db=str(db_path)))

        assert result.ok
        assert seen_auth["header"] == f"Bearer {self.TOKEN}"
        row = result.rows[0]
        assert row.pct == 24.68
        assert row.right == "$12.34 of $50.00"
        assert row.show_pct is True

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
            return _FakeResponse(json.dumps({"usage": {"percentUsed": 5.0}}).encode())

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
                                        _FakeResponse(json.dumps({"usage": {"percentUsed": 1.0}}).encode()))[1],
        )
        result = CursorUsageProvider().fetch(AgentConfig(state_db=str(db_path)))
        assert result.ok
        assert seen["auth"] == f"Bearer {self.TOKEN}"


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
