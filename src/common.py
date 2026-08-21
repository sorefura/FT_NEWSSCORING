"""共通スキーマ・追記専用I/O・ガード関数。

SPEC.md §3.1, §4.5 で定義された共通基盤。
制約A(ルックアヘッド禁止)・制約B(学習データ汚染対策)のガードはすべてここに集約し、
tests/test_guards.py で検証する。score.py / evaluate.py はこのモジュール経由でのみ
scores.jsonl を読み書きすること。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.yaml"
SCORES_PATH = REPO_ROOT / "data" / "scores.jsonl"
FEED_HEALTH_PATH = REPO_ROOT / "data" / "feed_health.jsonl"
PRICES_PATH = REPO_ROOT / "data" / "prices.parquet"
REPORTS_DIR = REPO_ROOT / "data" / "reports"

# 日本株市場の時刻はJST固定(DSTなし)で扱う。tzdataへの依存を避けるため固定オフセットを使う。
JST = timezone(timedelta(hours=9))


class GuardViolation(ValueError):
    """制約A/Bのガードに抵触した際に送出する例外。呼び出し側は書き込みを中止すること。"""


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_hex(text.encode("utf-8"))


def url_to_id(url: str) -> str:
    """SPEC §4.5: id = sha256(url) の先頭16桁。"""
    return sha256_text(normalize_url(url))[:16]


def normalize_url(url: str) -> str:
    """トラッキングパラメータ等によるURL表記ゆれを吸収した重複排除用の正規化(§4.1)。"""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, "", ""))


def hash_prompt_file(path: Path) -> str:
    """SPEC §0.4: 採点プロンプトはコードに埋め込まず、ファイルのSHA-256をレコードに保存する。"""
    with open(path, "rb") as f:
        return sha256_hex(f.read())


@dataclass(frozen=True)
class ScoreRecord:
    """SPEC §4.5 のレコードスキーマ(scores.jsonl, 1行1採点イベント)。"""

    id: str
    scored_at: str
    published_at: str
    source: str
    url: str
    headline: str
    scores: list
    score_median: int
    relevance: int
    rationale: str
    model: str
    prompt_sha256: str
    prompt_version: str
    pipeline_version: str
    feed_set: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_iso(label: str, ts: str) -> datetime:
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError as e:
        raise GuardViolation(f"{label} is not a valid ISO8601 timestamp: {ts!r}") from e
    if dt.tzinfo is None:
        raise GuardViolation(f"{label} must be timezone-aware: {ts!r}")
    return dt


def check_not_future(scored_at: datetime) -> None:
    """SPEC §4.5 ガード: scored_at が未来なら書き込み拒否。

    現在時刻は常に実時刻(datetime.now)を用いる。呼び出し元から任意の基準時刻を
    注入できるパラメータは意図的に設けない(過去日付採点・バックテストの経路を
    作らないため, SPEC §0/§8, REVIEW_CHECKLIST B-2/F-2)。
    """
    now = datetime.now(timezone.utc)
    if scored_at > now:
        raise GuardViolation(f"scored_at is in the future: {scored_at.isoformat()}")


def check_freshness(published_at: datetime, scored_at: datetime, *, max_age_hours: int = 48) -> None:
    """制約B: published_at が scored_at の48時間より前のニュースは採点拒否。
    未来日付の published_at(scored_at より後)も同時に拒否する(ルックアヘッド対策)。
    """
    if published_at > scored_at:
        raise GuardViolation(
            "published_at is after scored_at: "
            f"published_at={published_at.isoformat()} scored_at={scored_at.isoformat()}"
        )
    age = scored_at - published_at
    if age > timedelta(hours=max_age_hours):
        raise GuardViolation(
            f"published_at is older than {max_age_hours}h before scored_at "
            f"(learned-data contamination risk): published_at={published_at.isoformat()} "
            f"scored_at={scored_at.isoformat()}"
        )


def check_lookahead(scored_at: datetime, return_start_at: datetime) -> None:
    """制約A: 評価時に「採点時刻 < リターン計測開始時刻」を検証する。"""
    if not scored_at < return_start_at:
        raise GuardViolation(
            f"lookahead violation: scored_at={scored_at.isoformat()} is not before "
            f"return_start_at={return_start_at.isoformat()}"
        )


def check_allowed_model(model: str, allowed_models: list) -> None:
    """SPEC §4.3/C-5: 採点にHaiku系以外(Fable/Opus等の高価モデル)を使うことを防ぐ。"""
    if model not in allowed_models:
        raise GuardViolation(
            f"model '{model}' is not in allowed_models {allowed_models}. "
            "採点にはFable/Opus等の高価モデルを使用してはならない(SPEC §4.3)。"
        )


def read_existing_ids(path: Path = SCORES_PATH) -> set:
    if not path.exists():
        return set()
    ids = set()
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                ids.add(json.loads(line)["id"])
    return ids


def validate_record(record: ScoreRecord) -> None:
    scored_at = _parse_iso("scored_at", record.scored_at)
    published_at = _parse_iso("published_at", record.published_at)
    check_not_future(scored_at)
    check_freshness(published_at, scored_at)

    if not (-5 <= record.score_median <= 5):
        raise GuardViolation(f"score_median out of range [-5, 5]: {record.score_median}")
    if not record.scores:
        raise GuardViolation("scores must not be empty")
    for s in record.scores:
        if not (-5 <= s <= 5):
            raise GuardViolation(f"score out of range [-5, 5]: {s}")
    if record.relevance not in (0, 1, 2):
        raise GuardViolation(f"relevance out of range {{0,1,2}}: {record.relevance}")
    if len(record.rationale) > 40:
        raise GuardViolation(f"rationale exceeds 40 characters ({len(record.rationale)}): {record.rationale!r}")
    if not record.feed_set.strip():
        raise GuardViolation("feed_set must not be empty")


def append_score_record(record: ScoreRecord, path: Path = SCORES_PATH) -> None:
    """追記専用I/O。制約A-2/A-5(更新・削除禁止)と id 重複拒否を強制する。

    既存レコードを書き換えるコードパスは存在しない — 常にファイル末尾への追記のみ。
    """
    validate_record(record)
    existing_ids = read_existing_ids(path)
    if record.id in existing_ids:
        raise GuardViolation(f"id already exists; append-only storage forbids overwrite: {record.id}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")


def read_all_records(path: Path = SCORES_PATH) -> list:
    if not path.exists():
        return []
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def append_feed_health_observation(
    raw_entry_counts: dict[str, int],
    feed_urls: dict[str, str],
    pipeline_version: str,
    *,
    path: Path = FEED_HEALTH_PATH,
    checked_at: datetime | None = None,
) -> dict[str, Any]:
    """フィード単位の生エントリ数を append-only の監査ログへ追記する。

    scores.jsonl と同様、過去の観測は更新せず、各収集実行につき1行を末尾へ追加する。
    """
    checked_at = checked_at or datetime.now(timezone.utc)
    if checked_at.tzinfo is None:
        raise GuardViolation("feed health checked_at must be timezone-aware")
    if any(not isinstance(count, int) or count < 0 for count in raw_entry_counts.values()):
        raise GuardViolation(f"feed entry counts must be non-negative integers: {raw_entry_counts}")
    if set(raw_entry_counts) != set(feed_urls):
        raise GuardViolation("feed health counts and URL names must match")

    observation = {
        "checked_at": checked_at.isoformat(),
        "raw_entry_counts": raw_entry_counts,
        "feed_urls": feed_urls,
        "pipeline_version": pipeline_version,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(observation, ensure_ascii=False) + "\n")
    return observation


def read_feed_health_observations(path: Path = FEED_HEALTH_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    observations = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                observations.append(json.loads(line))
    return observations


def feed_zero_streaks(
    observations: list[dict[str, Any]], feed_names: list[str]
) -> dict[str, int]:
    """各フィードについて、末尾から連続する生エントリ0件の観測回数を返す。"""
    streaks: dict[str, int] = {}
    for feed_name in feed_names:
        streak = 0
        for observation in reversed(observations):
            counts = observation.get("raw_entry_counts", {})
            if feed_name not in counts or counts[feed_name] != 0:
                break
            streak += 1
        streaks[feed_name] = streak
    return streaks


def resolve_feed_set(record: dict[str, Any]) -> str:
    """INSTRUCTION_004 案A: 既存レコードを更新せず、欠落した系列IDを補完する。

    2026-08-10までは旧NHK+Yahoo、2026-08-11以降の既存レコードはYahoo単独。
    差し替え後のレコードには score.py が明示的な feed_set を保存するため、この
    日付補完は移行前の append-only レコードだけに適用される。
    """
    explicit = record.get("feed_set")
    if isinstance(explicit, str) and explicit.strip():
        return explicit
    scored_at = _parse_iso("scored_at", record["scored_at"]).astimezone(JST)
    if scored_at.date() <= datetime(2026, 8, 10, tzinfo=JST).date():
        return "nhk+yahoo"
    return "yahoo"
