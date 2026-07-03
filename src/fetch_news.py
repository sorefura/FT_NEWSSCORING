"""RSSフィードから直近ニュースを取得する(SPEC.md §3, §4.1)。

取得してよいのは見出し・URL・媒体名・公開時刻・本文抜粋(採点のためメモリ上でのみ使用)。
本文をディスクへ保存するコードはここにも他のどこにも書かないこと(SPEC §3.2)。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import feedparser

from common import SCORES_PATH, load_config, normalize_url, read_all_records, url_to_id


@dataclass(frozen=True)
class NewsItem:
    id: str
    source: str
    url: str
    headline: str
    published_at: datetime
    body_excerpt: str  # 採点プロンプト構築にのみ使用。scores.jsonlには保存しない。

    def to_public_dict(self) -> dict[str, Any]:
        """保存・ログ出力してよい情報のみ(本文を含まない)。"""
        return {
            "id": self.id,
            "source": self.source,
            "url": self.url,
            "headline": self.headline,
            "published_at": self.published_at.isoformat(),
        }


def _parse_published(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        value = getattr(entry, key, None)
        if value:
            return datetime(*value[:6], tzinfo=timezone.utc)
    return None


def fetch_candidate_news(
    config: dict[str, Any],
    *,
    existing_ids: set | None = None,
    existing_headlines: set | None = None,
) -> list[NewsItem]:
    """設定済みRSSフィードから、未採点かつ48時間以内に公開されたニュースを取得する。

    制約B(過去日付ニュースの採点禁止)の一次フィルタをここで行う。最終ガードは
    common.check_freshness 側で再検証する(score.py がレコード追記時に必ず通す)。
    「現在時刻」は常に実時刻を用いる。基準時刻を注入できる引数は意図的に設けない
    (過去日付ニュースの遡及取得・バックテストの経路を作らないため, SPEC §0/§8)。
    """
    now = datetime.now(timezone.utc)
    max_age = timedelta(hours=config["news"]["max_news_age_hours"])
    existing_ids = existing_ids or set()
    existing_headlines = existing_headlines or set()

    seen_ids: set = set()
    seen_headlines: set = set()
    items: list[NewsItem] = []

    for feed_cfg in config["news"]["feeds"]:
        parsed = feedparser.parse(feed_cfg["url"])
        for entry in parsed.entries:
            url = getattr(entry, "link", None)
            headline = getattr(entry, "title", None)
            if not url or not headline:
                continue

            published_at = _parse_published(entry)
            if published_at is None:
                continue
            if published_at > now or now - published_at > max_age:
                continue

            item_id = url_to_id(url)
            if item_id in existing_ids or item_id in seen_ids:
                continue
            if headline in existing_headlines or headline in seen_headlines:
                continue

            items.append(
                NewsItem(
                    id=item_id,
                    source=feed_cfg["name"],
                    url=normalize_url(url),
                    headline=headline,
                    published_at=published_at,
                    body_excerpt=getattr(entry, "summary", "") or "",
                )
            )
            seen_ids.add(item_id)
            seen_headlines.add(headline)

    return items


def main() -> None:
    argparse.ArgumentParser(description=__doc__).parse_args()

    config = load_config()
    existing = read_all_records(SCORES_PATH)
    existing_ids = {r["id"] for r in existing}
    existing_headlines = {r["headline"] for r in existing}

    items = fetch_candidate_news(config, existing_ids=existing_ids, existing_headlines=existing_headlines)
    json.dump([item.to_public_dict() for item in items], sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
