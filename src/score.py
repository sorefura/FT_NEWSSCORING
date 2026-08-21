"""凍結プロンプトでLLM採点し、scores.jsonl に追記する(SPEC.md §4)。

制約A/Bのガードは common.append_score_record 内で強制される。ここではガードを
回避する経路(過去日付指定、id/レコードの上書きなど)を一切設けないこと。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anthropic

from common import (
    FEED_HEALTH_PATH,
    REPO_ROOT,
    ScoreRecord,
    append_score_record,
    append_feed_health_observation,
    check_allowed_model,
    hash_prompt_file,
    load_config,
    feed_zero_streaks,
    read_all_records,
    read_feed_health_observations,
)
from fetch_news import NewsItem, fetch_candidate_news

PROMPT_PATH = REPO_ROOT / "prompts" / "scorer_v1.md"

# SPEC §4.3 が定めるプロトコル定数(採点タスクの一部であり、運用で変えてよい
# 設定値ではないため config.yaml には置かず、ここに固定値として持つ)。
SCORING_TEMPERATURE = 0
SCORING_REPEATS = 3


class ScoringError(RuntimeError):
    """LLM応答が期待するJSONスキーマに従わない場合に送出する。"""


def load_prompt_template(path: Path = PROMPT_PATH) -> str:
    return path.read_text(encoding="utf-8")


def render_prompt(template: str, item: NewsItem) -> str:
    return template.format(
        source=item.source,
        published_at=item.published_at.isoformat(),
        headline=item.headline,
        body=item.body_excerpt or "(本文なし。見出しのみで判断してください)",
    )


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[len("json"):]
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError as e:
        raise ScoringError(f"LLM response is not valid JSON: {text!r}") from e


def _validate_response(data: dict[str, Any]) -> dict[str, Any]:
    try:
        score = int(data["score"])
        relevance = int(data["relevance"])
        rationale = str(data["rationale"])
    except (KeyError, TypeError, ValueError) as e:
        raise ScoringError(f"LLM response missing/invalid fields: {data!r}") from e

    if not (-5 <= score <= 5):
        raise ScoringError(f"score out of range: {score}")
    if relevance not in (0, 1, 2):
        raise ScoringError(f"relevance out of range: {relevance}")
    if len(rationale) > 40:
        rationale = rationale[:40]

    return {"score": score, "relevance": relevance, "rationale": rationale}


def call_model(
    client: anthropic.Anthropic, model: str, prompt: str, *, temperature: float = SCORING_TEMPERATURE
) -> dict[str, Any]:
    response = client.messages.create(
        model=model,
        max_tokens=200,
        # Anthropic SDK v1 では sampling 引数がシグネチャから削除された。対象の旧モデルで
        # SPEC固定値 temperature=0 を維持するには extra_body 経由で送る(INSTRUCTION_004)。
        extra_body={"temperature": temperature},
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(block.text for block in response.content if getattr(block, "type", None) == "text")
    return _validate_response(_extract_json(text))


def three_pass_score(
    client: anthropic.Anthropic,
    model: str,
    prompt: str,
    *,
    temperature: float = SCORING_TEMPERATURE,
    repeats: int = SCORING_REPEATS,
) -> dict[str, Any]:
    """SPEC §4.3: 1ニュースにつき3回採点し、全scoreを記録。代表値は中央値。

    relevance/rationale は3回の応答で揺れうるため、score が中央値と一致した応答の
    ものを代表値として採用する(スキーマ上 relevance/rationale は1つしか持てないため)。
    """
    responses = [call_model(client, model, prompt, temperature=temperature) for _ in range(repeats)]
    scores = [r["score"] for r in responses]
    median_score = sorted(scores)[len(scores) // 2]
    representative = next(r for r in responses if r["score"] == median_score)
    return {
        "scores": scores,
        "score_median": median_score,
        "relevance": representative["relevance"],
        "rationale": representative["rationale"],
    }


def score_pending_news(
    config: dict[str, Any],
    *,
    client: anthropic.Anthropic | None = None,
    prompt_path: Path | None = None,
    feed_health_path: Path = FEED_HEALTH_PATH,
) -> dict[str, Any]:
    """未採点ニュースを取得し、採点してscores.jsonlに追記する一連の処理。"""
    model = config["scoring"]["model"]
    check_allowed_model(model, config["scoring"]["allowed_models"])

    # config.yaml の scoring.prompt_path を実際に読む(v2改訂時はconfig側だけ変えれば
    # 追随する。プロンプト本文とprompt_versionの対応が食い違う事故を防ぐ)。
    prompt_path = prompt_path or (REPO_ROOT / config["scoring"]["prompt_path"])
    prompt_template = load_prompt_template(prompt_path)
    prompt_sha256 = hash_prompt_file(prompt_path)
    prompt_version = config["scoring"]["prompt_version"]
    pipeline_version = config["pipeline_version"]

    existing = read_all_records()
    existing_ids = {r["id"] for r in existing}
    existing_headlines = {r["headline"] for r in existing}

    fetch_result = fetch_candidate_news(config, existing_ids=existing_ids, existing_headlines=existing_headlines)
    candidates = fetch_result.items
    feed_urls = {feed["name"]: feed["url"] for feed in config["news"]["feeds"]}
    append_feed_health_observation(
        fetch_result.raw_entry_counts,
        feed_urls,
        pipeline_version,
        path=feed_health_path,
    )
    observations = read_feed_health_observations(feed_health_path)
    zero_streaks = feed_zero_streaks(observations, list(feed_urls))
    zero_limit = config["news"]["feed_health"]["consecutive_zero_limit"]
    unhealthy_feeds = {name: streak for name, streak in zero_streaks.items() if streak >= zero_limit}
    all_feeds_empty = sum(fetch_result.raw_entry_counts.values()) == 0

    limit = config["news"]["daily_scoring_limit"]
    # SPEC §7: 上限超過分は繰り越さず切り捨て、欠測として記録する
    to_score = candidates[:limit]
    skipped_over_limit = len(candidates) - len(to_score)

    client = client or anthropic.Anthropic()

    scored = 0
    failed = 0
    for item in to_score:
        try:
            result = three_pass_score(client, model, render_prompt(prompt_template, item))
            scored_at = datetime.now(timezone.utc)
            record = ScoreRecord(
                id=item.id,
                scored_at=scored_at.isoformat(),
                published_at=item.published_at.isoformat(),
                source=item.source,
                url=item.url,
                headline=item.headline,
                scores=result["scores"],
                score_median=result["score_median"],
                relevance=result["relevance"],
                rationale=result["rationale"],
                model=model,
                prompt_sha256=prompt_sha256,
                prompt_version=prompt_version,
                pipeline_version=pipeline_version,
                feed_set=config["news"]["feed_set"],
            )
            append_score_record(record)
            scored += 1
        except Exception as e:  # noqa: BLE001 — 1件の失敗で他の採点を止めない。欠測として記録する。
            print(f"[score.py] skip {item.url}: {e}", file=sys.stderr)
            failed += 1

    return {
        "candidates": len(candidates),
        "scored": scored,
        "failed": failed,
        "skipped_over_limit": skipped_over_limit,
        "all_feeds_empty": all_feeds_empty,
        "feed_zero_streaks": zero_streaks,
        "unhealthy_feeds": unhealthy_feeds,
        "raw_entry_counts": fetch_result.raw_entry_counts,
    }


def main() -> None:
    argparse.ArgumentParser(description=__doc__).parse_args()
    config = load_config()
    summary = score_pending_news(config)
    print(json.dumps(summary, ensure_ascii=False))

    if summary["all_feeds_empty"]:
        # 全フィードが0エントリ = フィード設定/疎通の障害。欠測に気づけないサイレント失敗を防ぐ(G-1)。
        print("[score.py] 全フィードが0エントリでした。フィードURLの疎通を確認してください。", file=sys.stderr)
        sys.exit(1)

    if summary["unhealthy_feeds"]:
        print(
            "[score.py] フィード単位の連続0件ガードが発火しました: "
            f"{summary['unhealthy_feeds']}",
            file=sys.stderr,
        )
        sys.exit(1)

    if summary["candidates"] > 0 and summary["scored"] == 0:
        # 候補があったのに1件も採点できなかった場合はジョブを失敗させ、サイレント失敗にしない(SPEC G-1)。
        sys.exit(1)


if __name__ == "__main__":
    main()
