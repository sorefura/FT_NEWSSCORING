"""制約A(ルックアヘッド禁止)・制約B(学習データ汚染対策)のガードが機能するかの単体テスト。

SPEC.md §3.1 / §4.5 参照。common.py のガードが1つでも壊れるとルックアヘウド混入や
過去日付採点(学習データ汚染)を検出できなくなるため、このテストは全て通ることを
他の実装に着手する前提条件とする(SPEC.md §10)。
"""

from datetime import datetime, timedelta, timezone

import pytest

import common
from common import GuardViolation, ScoreRecord


def make_record(**overrides) -> ScoreRecord:
    now = overrides.pop("_now", datetime(2026, 7, 2, 7, 0, 0, tzinfo=timezone.utc))
    published_at = overrides.pop("_published_delta_hours", 1)
    defaults = dict(
        id="a" * 16,
        scored_at=now.isoformat(),
        published_at=(now - timedelta(hours=published_at)).isoformat(),
        source="reuters_jp",
        url="https://example.com/article",
        headline="見出し",
        scores=[1, 2, 2],
        score_median=2,
        relevance=2,
        rationale="短い根拠",
        model="claude-haiku-4-5-20251001",
        prompt_sha256="deadbeef",
        prompt_version="v1",
        pipeline_version="1.0.0",
    )
    defaults.update(overrides)
    return ScoreRecord(**defaults)


# --- 制約B: 過去日付ニュースの採点拒否 -------------------------------------------------


def test_freshness_guard_rejects_news_older_than_48h():
    record = make_record(_published_delta_hours=49)
    with pytest.raises(GuardViolation):
        common.validate_record(record)


def test_freshness_guard_allows_news_within_48h():
    record = make_record(_published_delta_hours=47)
    common.validate_record(record)  # raises on failure


def test_freshness_guard_rejects_published_after_scored():
    now = datetime(2026, 7, 2, 7, 0, 0, tzinfo=timezone.utc)
    record = make_record(
        _now=now,
        published_at=(now + timedelta(hours=1)).isoformat(),
    )
    with pytest.raises(GuardViolation):
        common.validate_record(record)


# --- scored_at 未来日付の書き込み拒否 --------------------------------------------------


def test_rejects_future_scored_at():
    future = datetime.now(timezone.utc) + timedelta(days=1)
    record = make_record(_now=future, _published_delta_hours=1)
    with pytest.raises(GuardViolation):
        common.validate_record(record)


def test_rejects_naive_datetime_scored_at():
    record = make_record()
    bad = ScoreRecord(**{**record.to_dict(), "scored_at": "2026-07-02T07:00:00"})
    with pytest.raises(GuardViolation):
        common.validate_record(bad)


# --- 制約A: 追記専用・既存レコードへの再書き込み禁止 ------------------------------------


def test_append_only_rejects_duplicate_id(tmp_path):
    path = tmp_path / "scores.jsonl"
    record = make_record(id="b" * 16)
    common.append_score_record(record, path=path)

    with pytest.raises(GuardViolation):
        common.append_score_record(record, path=path)

    # 1行しか書き込まれていないこと(重複拒否が実際にファイルへの二重追記を防いでいる)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


def test_append_only_allows_new_ids(tmp_path):
    path = tmp_path / "scores.jsonl"
    common.append_score_record(make_record(id="c" * 16, url="https://example.com/1"), path=path)
    common.append_score_record(make_record(id="d" * 16, url="https://example.com/2"), path=path)
    records = common.read_all_records(path)
    assert len(records) == 2


def test_append_only_never_rewrites_existing_lines(tmp_path):
    path = tmp_path / "scores.jsonl"
    common.append_score_record(make_record(id="e" * 16, rationale="最初の根拠"), path=path)
    original_content = path.read_text(encoding="utf-8")

    with pytest.raises(GuardViolation):
        common.append_score_record(make_record(id="e" * 16, rationale="書き換え試行"), path=path)

    assert path.read_text(encoding="utf-8") == original_content


# --- レコードスキーマの値域検証 --------------------------------------------------------


@pytest.mark.parametrize("score_median", [-6, 6])
def test_rejects_score_median_out_of_range(score_median):
    record = make_record(score_median=score_median)
    with pytest.raises(GuardViolation):
        common.validate_record(record)


@pytest.mark.parametrize("scores", [[-6, 1, 1], [1, 1, 6]])
def test_rejects_individual_scores_out_of_range(scores):
    record = make_record(scores=scores)
    with pytest.raises(GuardViolation):
        common.validate_record(record)


@pytest.mark.parametrize("relevance", [-1, 3])
def test_rejects_relevance_out_of_range(relevance):
    record = make_record(relevance=relevance)
    with pytest.raises(GuardViolation):
        common.validate_record(record)


def test_rejects_rationale_over_40_chars():
    record = make_record(rationale="あ" * 41)
    with pytest.raises(GuardViolation):
        common.validate_record(record)


def test_accepts_rationale_exactly_40_chars():
    record = make_record(rationale="あ" * 40)
    common.validate_record(record)


# --- 制約A: 評価時の採点時刻 < リターン計測開始時刻 検証 ---------------------------------


def test_check_lookahead_passes_when_scored_before_return_start():
    scored_at = datetime(2026, 7, 2, 7, 0, tzinfo=timezone.utc)
    return_start = datetime(2026, 7, 3, 0, 0, tzinfo=timezone.utc)
    common.check_lookahead(scored_at, return_start)


def test_check_lookahead_rejects_when_scored_after_return_start():
    scored_at = datetime(2026, 7, 3, 1, 0, tzinfo=timezone.utc)
    return_start = datetime(2026, 7, 3, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(GuardViolation):
        common.check_lookahead(scored_at, return_start)


def test_check_lookahead_rejects_when_equal():
    t = datetime(2026, 7, 3, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(GuardViolation):
        common.check_lookahead(t, t)


# --- 制約B: モデル固定 -----------------------------------------------------------------


def test_check_allowed_model_accepts_haiku():
    common.check_allowed_model("claude-haiku-4-5-20251001", ["claude-haiku-4-5-20251001"])


def test_check_allowed_model_rejects_expensive_model():
    with pytest.raises(GuardViolation):
        common.check_allowed_model("claude-opus-4-8", ["claude-haiku-4-5-20251001"])


# --- id / URL 正規化(重複排除の基盤) ---------------------------------------------------


def test_url_to_id_is_deterministic_and_16_hex_chars():
    id1 = common.url_to_id("https://example.com/article?utm_source=x")
    id2 = common.url_to_id("https://example.com/article/?utm_source=y")
    assert id1 == id2  # 正規化により表記ゆれを吸収する
    assert len(id1) == 16
    int(id1, 16)  # 16進文字列であること


def test_normalize_url_strips_query_and_trailing_slash():
    assert common.normalize_url("https://Example.com/Path/?a=1") == "https://example.com/Path"


# --- プロンプトファイルのハッシュ化(§0.4) ----------------------------------------------


def test_hash_prompt_file_matches_manual_sha256(tmp_path):
    import hashlib

    p = tmp_path / "scorer_v1.md"
    p.write_text("prompt body", encoding="utf-8")
    expected = hashlib.sha256(p.read_bytes()).hexdigest()
    assert common.hash_prompt_file(p) == expected


# --- GitHub Actions workflow YAMLの構文ガード(INSTRUCTION_002 タスク1) ------------------


def test_github_workflow_yaml_is_valid():
    """ワークフローYAMLが不正だとCI自体が起動せず自己検出できないため、ローカルテストで守る(INSTRUCTION_002 タスク1)。"""
    import yaml

    workflows = list((common.REPO_ROOT / ".github" / "workflows").glob("*.yml"))
    assert workflows, "workflowファイルが見つからない"
    for p in workflows:
        with open(p, encoding="utf-8") as f:
            yaml.safe_load(f)
