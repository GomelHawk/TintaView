"""The Claude provider's transcript fallback: dedupe, pricing and the scan rule.

Measured against a real ``~/.claude/projects`` (one week): 3592 usage lines collapsed to
1737 distinct messages, and only 27 of 236 files (23 of 158 MB) had been modified inside
the window. Both facts drive what is pinned here.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tintaview.core.config import AgentConfig
from tintaview.stats.providers import claude as claude_mod
from tintaview.stats.providers.claude import ClaudeUsageProvider


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _line(
    model: str,
    *,
    msg_id: str | None = None,
    request_id: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read: int = 0,
    cache_write: int = 0,
    age: timedelta = timedelta(hours=1),
) -> str:
    message: dict = {
        "model": model,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_write,
        },
    }
    if msg_id is not None:
        message["id"] = msg_id
    rec: dict = {"timestamp": _iso(datetime.now(UTC) - age), "message": message}
    if request_id is not None:
        rec["requestId"] = request_id
    return json.dumps(rec)


def _write(home: Path, lines: list[str], name: str = "session.jsonl") -> Path:
    project_dir = home / "projects" / "proj1"
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _row(result, label: str):
    return next(r for r in result.rows if r.label == label)


def _tokens_m(result, label: str = "5-hour") -> float:
    return float(_row(result, label).right.split("M")[0])


def _cost(result, label: str = "5-hour") -> float:
    return float(_row(result, label).right.split("$")[1])


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "claude_home"
    h.mkdir(parents=True)
    return h


class TestDedupe:
    def test_one_streamed_message_is_counted_once(self, home):
        """Claude Code writes one line per content block, each carrying the message's
        usage. Input/cache repeat identically; output grows and the last line holds
        the final count. Three lines for one message must count as one message with
        the largest output figure."""
        shared = {"msg_id": "msg_1", "request_id": "req_1", "input_tokens": 2,
                  "cache_read": 18_995, "cache_write": 8_605}
        _write(home, [
            _line("claude-opus-5", output_tokens=1, **shared),
            _line("claude-opus-5", output_tokens=1, **shared),
            _line("claude-opus-5", output_tokens=266, **shared),
        ])

        result = ClaudeUsageProvider().fetch(AgentConfig(home=str(home)))

        assert result.ok
        expected_tokens = (2 + 266 + 18_995 + 8_605) / 1e6
        assert _tokens_m(result) == pytest.approx(expected_tokens, abs=0.005)
        expected_cost = (2 * 5.0 + 266 * 25.0 + 18_995 * 0.5 + 8_605 * 6.25) / 1e6
        assert _cost(result) == pytest.approx(expected_cost, abs=0.005)

    def test_distinct_messages_still_add_up(self, home):
        _write(home, [
            _line("claude-opus-5", msg_id="m1", request_id="r1", input_tokens=500_000),
            _line("claude-opus-5", msg_id="m2", request_id="r2", input_tokens=500_000),
        ])
        result = ClaudeUsageProvider().fetch(AgentConfig(home=str(home)))
        assert _tokens_m(result) == pytest.approx(1.0, abs=0.005)

    def test_lines_without_ids_are_never_collapsed(self, home):
        """An older transcript shape has neither id; two such lines are two messages."""
        _write(home, [
            _line("claude-opus-5", input_tokens=500_000),
            _line("claude-opus-5", input_tokens=500_000),
        ])
        result = ClaudeUsageProvider().fetch(AgentConfig(home=str(home)))
        assert _tokens_m(result) == pytest.approx(1.0, abs=0.005)


class TestPricing:
    @pytest.mark.parametrize(
        ("model", "expected_cost"),
        [
            ("claude-fable-5-1", 10.0),
            ("claude-mythos-5-1", 10.0),
            ("claude-fable-5", 10.0),
            ("claude-opus-5", 5.0),
        ],
    )
    def test_current_top_tier_models_are_priced(self, home, model, expected_cost):
        """Fable 5.1 was missing from PRICING while being the model in the user's own
        transcripts — charged at the default $5 instead of $10 and flipping the header
        to the 'unknown model' wording for every Fable 5.1 user."""
        _write(home, [_line(model, input_tokens=1_000_000)])
        result = ClaudeUsageProvider().fetch(AgentConfig(home=str(home)))
        assert _cost(result) == pytest.approx(expected_cost, rel=0.01)
        assert "unknown model" not in (result.header or "")

    def test_fable_5_1_cache_reads_use_the_flat_rate(self, home):
        """$0.25/MTok, not 0.1x of the $10 input rate."""
        _write(home, [_line("claude-fable-5-1", cache_read=1_000_000)])
        result = ClaudeUsageProvider().fetch(AgentConfig(home=str(home)))
        assert _cost(result) == pytest.approx(0.25, abs=0.005)

    def test_other_models_keep_the_tenth_of_input_cache_rate(self, home):
        _write(home, [_line("claude-opus-5", cache_read=1_000_000)])
        result = ClaudeUsageProvider().fetch(AgentConfig(home=str(home)))
        assert _cost(result) == pytest.approx(0.5, abs=0.005)

    def test_synthetic_placeholder_messages_are_ignored(self, home):
        """`<synthetic>` is Claude Code's placeholder for a cancelled/interrupted turn.
        It must neither count nor flip the header to the unpriced wording."""
        _write(home, [
            _line("claude-opus-5", input_tokens=1_000_000),
            _line(claude_mod.SYNTHETIC_MODEL, input_tokens=1_000_000),
        ])
        result = ClaudeUsageProvider().fetch(AgentConfig(home=str(home)))
        assert _tokens_m(result) == pytest.approx(1.0, abs=0.005)
        assert "unknown model" not in (result.header or "")


class TestScanRule:
    def test_files_untouched_for_a_week_are_not_opened(self, home):
        """The mtime filter, not the per-line timestamp, is what skips the file: this
        file's only line is recent, and it is still ignored because the file itself
        was last modified ten days ago (impossible in reality, which is the point —
        the test proves the file was never read)."""
        _write(home, [_line("claude-opus-5", input_tokens=1_000_000)], name="fresh.jsonl")
        stale = _write(home, [_line("claude-opus-5", input_tokens=1_000_000)], name="stale.jsonl")
        ten_days_ago = (datetime.now(UTC) - timedelta(days=10)).timestamp()
        os.utime(stale, (ten_days_ago, ten_days_ago))

        result = ClaudeUsageProvider().fetch(AgentConfig(home=str(home)))

        assert _tokens_m(result) == pytest.approx(1.0, abs=0.005)

    def test_unchanged_files_are_served_from_the_memo(self, home, monkeypatch):
        path = _write(home, [_line("claude-opus-5", input_tokens=1_000_000)])
        calls = 0
        real = claude_mod._parse_transcript

        def counting(p: Path):
            nonlocal calls
            calls += 1
            return real(p)

        monkeypatch.setattr(claude_mod, "_parse_transcript", counting)
        provider = ClaudeUsageProvider()

        provider.fetch(AgentConfig(home=str(home)))
        provider.fetch(AgentConfig(home=str(home)))
        assert calls == 1

        # An appended line grows the file, so the next poll re-parses it.
        with open(path, "a", encoding="utf-8") as f:
            f.write(_line("claude-opus-5", input_tokens=1_000_000) + "\n")
        result = provider.fetch(AgentConfig(home=str(home)))
        assert calls == 2
        assert _tokens_m(result) == pytest.approx(2.0, abs=0.005)

    def test_window_bucketing_uses_the_line_timestamp(self, home):
        _write(home, [
            _line("claude-opus-5", input_tokens=1_000_000, age=timedelta(hours=1)),
            _line("claude-opus-5", input_tokens=1_000_000, age=timedelta(days=6)),
            _line("claude-opus-5", input_tokens=1_000_000, age=timedelta(days=10)),
        ])
        result = ClaudeUsageProvider().fetch(AgentConfig(home=str(home)))
        assert _tokens_m(result, "5-hour") == pytest.approx(1.0, abs=0.005)
        assert _tokens_m(result, "This week") == pytest.approx(2.0, abs=0.005)
