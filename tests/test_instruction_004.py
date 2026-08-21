"""INSTRUCTION_004 のRSS復旧・死活監視・系列分離ガード。"""

from datetime import datetime, timezone
from types import SimpleNamespace

import common
import score


def test_nhk_feed_url_and_new_feed_set_are_fixed():
    config = common.load_config()
    feeds = {feed["name"]: feed["url"] for feed in config["news"]["feeds"]}

    assert feeds["nhk_keizai"] == "https://news.web.nhk/n-data/conf/na/rss/cat5.xml"
    assert config["news"]["feed_set"] == "nhk_news_web+yahoo"
    assert config["news"]["feed_health"]["consecutive_zero_limit"] == 3


def test_feed_zero_streak_is_counted_per_feed_and_recovery_resets():
    observations = [
        {"raw_entry_counts": {"nhk_keizai": 0, "yahoo_keizai": 8}},
        {"raw_entry_counts": {"nhk_keizai": 0, "yahoo_keizai": 7}},
        {"raw_entry_counts": {"nhk_keizai": 0, "yahoo_keizai": 0}},
    ]
    assert common.feed_zero_streaks(observations, ["nhk_keizai", "yahoo_keizai"]) == {
        "nhk_keizai": 3,
        "yahoo_keizai": 1,
    }

    observations.append({"raw_entry_counts": {"nhk_keizai": 5, "yahoo_keizai": 0}})
    assert common.feed_zero_streaks(observations, ["nhk_keizai", "yahoo_keizai"]) == {
        "nhk_keizai": 0,
        "yahoo_keizai": 2,
    }


def test_feed_health_log_is_append_only(tmp_path):
    path = tmp_path / "feed_health.jsonl"
    urls = {"nhk_keizai": "https://nhk.example/rss", "yahoo_keizai": "https://yahoo.example/rss"}
    common.append_feed_health_observation(
        {"nhk_keizai": 0, "yahoo_keizai": 8},
        urls,
        "1.1.0",
        path=path,
        checked_at=datetime(2026, 8, 21, 0, 0, tzinfo=timezone.utc),
    )
    first = path.read_text(encoding="utf-8")
    common.append_feed_health_observation(
        {"nhk_keizai": 4, "yahoo_keizai": 7},
        urls,
        "1.1.0",
        path=path,
        checked_at=datetime(2026, 8, 21, 9, 0, tzinfo=timezone.utc),
    )

    assert path.read_text(encoding="utf-8").startswith(first)
    assert len(common.read_feed_health_observations(path)) == 2


def test_legacy_records_are_partitioned_without_backfill():
    before = {"scored_at": "2026-08-10T23:59:00+09:00"}
    yahoo_only = {"scored_at": "2026-08-11T00:00:00+09:00"}
    restored = {
        "scored_at": "2026-08-21T18:00:00+09:00",
        "feed_set": "nhk_news_web+yahoo",
    }

    assert common.resolve_feed_set(before) == "nhk+yahoo"
    assert common.resolve_feed_set(yahoo_only) == "yahoo"
    assert common.resolve_feed_set(restored) == "nhk_news_web+yahoo"


def test_anthropic_v1_uses_extra_body_for_frozen_temperature():
    class FakeMessages:
        def __init__(self):
            self.kwargs = None

        def create(self, **kwargs):
            self.kwargs = kwargs
            block = SimpleNamespace(type="text", text='{"score": 1, "relevance": 2, "rationale": "根拠"}')
            return SimpleNamespace(content=[block])

    messages = FakeMessages()
    client = SimpleNamespace(messages=messages)

    result = score.call_model(client, "claude-haiku-4-5-20251001", "prompt", temperature=0)

    assert result["score"] == 1
    assert "temperature" not in messages.kwargs
    assert messages.kwargs["extra_body"] == {"temperature": 0}


def test_workflow_commits_health_observation_before_propagating_score_failure():
    workflow = (common.REPO_ROOT / ".github" / "workflows" / "daily.yml").read_text(encoding="utf-8")

    assert "continue-on-error: true" in workflow
    assert "feed_health.jsonl" in workflow
    assert "steps.score.outcome == 'failure'" in workflow
