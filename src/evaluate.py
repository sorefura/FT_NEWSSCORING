"""フォワードリターン結合・仮説評価レポート生成(SPEC.md §6)。

- 評価対象は hypotheses.md 登録済みの H1〜H3 のみ(§6.4)。
- model / prompt_version が異なるスコアは決して混ぜない(制約B, B-4)。
- レコード単位で「採点時刻 < リターン計測開始時刻」を検証し、違反は除外+警告する(制約A, A-3)。
- コスト控除前の数字は参考値としてのみ表示し、結論(棄却判定)には使わない(§6.2, D-4)。
"""

from __future__ import annotations

import argparse
import math
import random
import re
import statistics
import sys
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from common import (
    JST,
    PRICES_PATH,
    REPO_ROOT,
    REPORTS_DIR,
    GuardViolation,
    check_lookahead,
    load_config,
    read_all_records,
)

HYPOTHESES_PATH = REPO_ROOT / "hypotheses.md"

# SPEC §1.3 の棄却条件で使う固定閾値(config.yaml の min_n_for_judgement とは別物。
# こちらは仮説レベルの正式な棄却基準としてSPECに明記された値)。
HYPOTHESIS_REJECTION_MIN_N = 60
HYPOTHESIS_REJECTION_MIN_MONTHS_SIGN = 6
HYPOTHESIS_REJECTION_MIN_MONTHS_COST = 12
SIGN_AGREEMENT_P_THRESHOLD = 0.10

BUCKETS = [
    (-5, -3, "-5〜-3"),
    (-2, -1, "-2〜-1"),
    (0, 0, "0"),
    (1, 2, "+1〜+2"),
    (3, 5, "+3〜+5"),
]

EXPLORATORY_DISCLAIMER = (
    "> **注意:** 本結果は事前登録されておらず、結論には使用できません(SPEC.md §6.4)。\n\n"
)


# --- 事前登録仮説の読み込み(§6.4: 多重検定の規律) -----------------------------------


def count_registered_hypotheses(path: Path = HYPOTHESES_PATH) -> int:
    text = path.read_text(encoding="utf-8")
    section = text.split("## 事前登録仮説")[1].split("## 棄却条件")[0]
    return len(re.findall(r"^### H", section, flags=re.MULTILINE))


def parse_registration_date(path: Path = HYPOTHESES_PATH) -> date:
    text = path.read_text(encoding="utf-8")
    m = re.search(r"登録日:\s*(\d{4}-\d{2}-\d{2})", text)
    if not m:
        raise ValueError(f"hypotheses.md に登録日が見つかりません: {path}")
    return date.fromisoformat(m.group(1))


# --- 採点器の再現性検査(校正, SPEC §4.4) --------------------------------------------


def calibration_report(
    records: list[dict[str, Any]], *, range_threshold: float, failure_rate_threshold: float, lookback_months: int
) -> dict[str, Any]:
    """3回採点の範囲(max-min)が range_threshold を超える比率を月次集計し、直近数ヶ月の合否を判定する。

    直近 lookback_months ヶ月のうち、いずれか1ヶ月でも比率が failure_rate_threshold を
    超えた場合に不合格とする(計測の汚染を防ぐ方向に倒す, SPEC §0)。
    """
    monthly: dict[str, list[int]] = {}
    for r in records:
        scored_at = datetime.fromisoformat(r["scored_at"]).astimezone(JST)
        month_key = scored_at.strftime("%Y-%m")
        score_range = max(r["scores"]) - min(r["scores"])
        monthly.setdefault(month_key, []).append(1 if score_range > range_threshold else 0)

    months_sorted = sorted(monthly.keys())
    recent_months = months_sorted[-lookback_months:]
    ratios = {m: (sum(monthly[m]) / len(monthly[m])) for m in recent_months}
    failed = any(ratio > failure_rate_threshold for ratio in ratios.values())
    return {"monthly_ratios": ratios, "failed": failed}


# --- フォワードリターンの結合(制約A) --------------------------------------------------


def trading_dates_for_symbol(prices_df: pd.DataFrame, symbol: str) -> list[date]:
    subset = prices_df.loc[prices_df["symbol"] == symbol, "date"]
    return sorted(pd.to_datetime(subset).dt.date.unique().tolist())


def _find_entry_index(trading_dates: list[date], score_date: date) -> int | None:
    for i, d in enumerate(trading_dates):
        if d > score_date:
            return i
    return None


def compute_forward_returns(
    score_date: date, trading_dates: list[date], opens: dict[str, float]
) -> dict[str, Any]:
    """SPEC §5: リターン起点は「スコア確定日の翌営業日の始値」。t+1→t+2(1日)/t+6(5日)/t+21(20日)。"""
    result: dict[str, Any] = {"entry_date": None, "ret_1d": None, "ret_5d": None, "ret_20d": None}
    entry_idx = _find_entry_index(trading_dates, score_date)
    if entry_idx is None:
        return result

    entry_date = trading_dates[entry_idx]
    entry_open = opens.get(entry_date.isoformat())
    result["entry_date"] = entry_date.isoformat()
    if not entry_open:
        return result

    for label, offset in (("ret_1d", 1), ("ret_5d", 5), ("ret_20d", 20)):
        exit_idx = entry_idx + offset
        if exit_idx < len(trading_dates):
            exit_open = opens.get(trading_dates[exit_idx].isoformat())
            if exit_open:
                result[label] = exit_open / entry_open - 1

    return result


def build_daily_dataframe(records: list[dict[str, Any]], prices_df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """relevance>=1のレコードを日次合成し、フォワードリターンを結合する(§6.1, §5)。

    レコード単位で採点時刻<リターン計測開始時刻を検証し、違反は除外+警告する(A-3)。
    """
    trading_dates = trading_dates_for_symbol(prices_df, symbol)
    opens = prices_df.loc[prices_df["symbol"] == symbol].set_index("date")["open"].to_dict()

    per_date: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        if r["relevance"] < 1:
            continue
        scored_at = datetime.fromisoformat(r["scored_at"])
        score_date = scored_at.astimezone(JST).date()
        forward = compute_forward_returns(score_date, trading_dates, opens)

        if forward["entry_date"] is not None:
            return_start_at = datetime.combine(date.fromisoformat(forward["entry_date"]), time(9, 0), tzinfo=JST)
            try:
                check_lookahead(scored_at, return_start_at)
            except GuardViolation as e:
                print(f"[evaluate.py] lookahead違反のため除外: id={r['id']} {e}", file=sys.stderr)
                continue

        per_date.setdefault(score_date.isoformat(), []).append({**r, "_forward": forward})

    rows = []
    for score_date_str, recs in per_date.items():
        weight_sum = sum(r["relevance"] for r in recs)
        if weight_sum == 0:
            continue
        composite = sum(r["score_median"] * r["relevance"] for r in recs) / weight_sum
        forward = recs[0]["_forward"]
        rows.append(
            {
                "date": score_date_str,
                "composite_score": composite,
                "n_records": len(recs),
                "entry_date": forward["entry_date"],
                "ret_1d": forward["ret_1d"],
                "ret_5d": forward["ret_5d"],
                "ret_20d": forward["ret_20d"],
            }
        )

    columns = ["date", "composite_score", "n_records", "entry_date", "ret_1d", "ret_5d", "ret_20d"]
    if not rows:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(rows)[columns].sort_values("date").reset_index(drop=True)


# --- バケット集計(§6.1) --------------------------------------------------------------


def bucket_label(score: float) -> str:
    rounded = max(-5, min(5, int(round(score))))
    for lo, hi, label in BUCKETS:
        if lo <= rounded <= hi:
            return label
    return "0"


def bucket_table(daily_df: pd.DataFrame, *, min_n_for_judgement: int) -> pd.DataFrame:
    """§6.1のバケット別集計。Nが少ないバケットは§6.3に従い「判定保留」を注記する。"""
    order = [b[2] for b in BUCKETS]
    columns = [
        "bucket", "n", "judgement",
        "mean_ret_1d", "std_ret_1d", "mean_ret_5d", "std_ret_5d", "mean_ret_20d", "std_ret_20d",
    ]
    if daily_df.empty:
        return pd.DataFrame(columns=columns)

    df = daily_df.copy()
    df["bucket"] = df["composite_score"].apply(bucket_label)

    rows = []
    for label in order:
        sub = df[df["bucket"] == label]
        n = len(sub)
        row: dict[str, Any] = {
            "bucket": label,
            "n": n,
            "judgement": "判定保留(N不足)" if n < min_n_for_judgement else "-",
        }
        for h in ("ret_1d", "ret_5d", "ret_20d"):
            vals = sub[h].dropna()
            row[f"mean_{h}"] = float(vals.mean()) if len(vals) else None
            row[f"std_{h}"] = float(vals.std(ddof=1)) if len(vals) > 1 else None
        rows.append(row)
    return pd.DataFrame(rows)[columns]


# --- ブートストラップ検定(§6.3) --------------------------------------------------------
#
# numpyはSPEC §3の依存最小セットに含まれないため、標準ライブラリ(random/statistics)のみで
# 実装する(E-4: 不要な依存の混入なし)。


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return float("nan")
    k = (len(sorted_values) - 1) * (pct / 100)
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return sorted_values[int(k)]
    return sorted_values[f] * (c - k) + sorted_values[c] * (k - f)


def bootstrap_diff_of_means(a: list[float], b: list[float], *, n_resamples: int, rng: random.Random) -> dict[str, float]:
    if not a or not b:
        return {"diff": float("nan"), "ci_low": float("nan"), "ci_high": float("nan"), "p_value": float("nan")}
    observed = statistics.mean(a) - statistics.mean(b)
    diffs = []
    for _ in range(n_resamples):
        ra = rng.choices(a, k=len(a))
        rb = rng.choices(b, k=len(b))
        diffs.append(statistics.mean(ra) - statistics.mean(rb))
    diffs.sort()
    p_ge = sum(1 for d in diffs if d <= 0) / len(diffs)
    p_le = sum(1 for d in diffs if d >= 0) / len(diffs)
    p_value = min(1.0, 2 * min(p_ge, p_le))
    return {
        "diff": observed,
        "ci_low": _percentile(diffs, 2.5),
        "ci_high": _percentile(diffs, 97.5),
        "p_value": p_value,
    }


def bootstrap_sign_agreement(returns: list[float], *, n_resamples: int, rng: random.Random) -> dict[str, float]:
    """SPEC §1.3: スコアと将来リターンの符号一致率が50%と区別できるかのブートストラップ検定。"""
    if not returns:
        return {"rate": float("nan"), "ci_low": float("nan"), "ci_high": float("nan"), "p_value": float("nan")}
    observed = sum(1 for r in returns if r > 0) / len(returns)
    rates = []
    for _ in range(n_resamples):
        sample = rng.choices(returns, k=len(returns))
        rates.append(sum(1 for s in sample if s > 0) / len(sample))
    rates.sort()
    p_ge = sum(1 for r in rates if r <= 0.5) / len(rates)
    p_le = sum(1 for r in rates if r >= 0.5) / len(rates)
    p_value = min(1.0, 2 * min(p_ge, p_le))
    return {
        "rate": observed,
        "ci_low": _percentile(rates, 2.5),
        "ci_high": _percentile(rates, 97.5),
        "p_value": p_value,
    }


# --- 仮説評価(H1〜H3) ------------------------------------------------------------------


@dataclass(frozen=True)
class HypothesisSpec:
    key: str
    label: str
    horizon_col: str
    event_mask: Callable[[pd.DataFrame], pd.Series]
    cost_key: str


HYPOTHESIS_SPECS = [
    HypothesisSpec("H1", "指数・翌日", "ret_1d", lambda df: df["composite_score"] >= 2, "round_trip_cost_index"),
    HypothesisSpec("H2", "指数・持続(5日)", "ret_5d", lambda df: df["composite_score"] >= 2, "round_trip_cost_index"),
    HypothesisSpec("H3", "逆張り検証(20日)", "ret_20d", lambda df: df["composite_score"] <= -3, "round_trip_cost_index"),
]


def evaluate_hypothesis(
    daily_df: pd.DataFrame,
    spec: HypothesisSpec,
    *,
    cost: float,
    n_resamples: int,
    rng: random.Random,
    calibration_failed: bool,
    months_elapsed: float,
    total_hypotheses: int,
) -> dict[str, Any]:
    valid = daily_df.dropna(subset=[spec.horizon_col])
    baseline = valid[spec.horizon_col].tolist()
    event = valid[spec.event_mask(valid)][spec.horizon_col].tolist()
    n = len(event)

    diff_test = bootstrap_diff_of_means(event, baseline, n_resamples=n_resamples, rng=rng)
    sign_test = bootstrap_sign_agreement(event, n_resamples=n_resamples, rng=rng)
    mean_return_pre_cost = statistics.mean(event) if n else float("nan")
    cost_adjusted_ev = mean_return_pre_cost - cost if n else float("nan")
    bonferroni_p_sign = min(1.0, sign_test["p_value"] * total_hypotheses) if n else float("nan")
    bonferroni_p_diff = min(1.0, diff_test["p_value"] * total_hypotheses) if n else float("nan")

    status = _hypothesis_status(
        calibration_failed=calibration_failed,
        months_elapsed=months_elapsed,
        n=n,
        sign_p_value=sign_test["p_value"],
        cost_adjusted_ev=cost_adjusted_ev,
    )

    return {
        "key": spec.key,
        "label": spec.label,
        "n": n,
        "mean_return_pre_cost": mean_return_pre_cost,
        "cost_adjusted_ev": cost_adjusted_ev,
        "diff_vs_baseline": diff_test,
        "sign_agreement": sign_test,
        "bonferroni_p_sign": bonferroni_p_sign,
        "bonferroni_p_diff": bonferroni_p_diff,
        "status": status,
    }


def _hypothesis_status(
    *, calibration_failed: bool, months_elapsed: float, n: int, sign_p_value: float, cost_adjusted_ev: float
) -> str:
    """SPEC §1.3 の棄却条件をそのまま適用する。"""
    if calibration_failed:
        return "評価不能(採点器の再現性検査に不合格のため全仮説を凍結, §1.3/§4.4)"
    if (
        months_elapsed >= HYPOTHESIS_REJECTION_MIN_MONTHS_SIGN
        and n >= HYPOTHESIS_REJECTION_MIN_N
        and sign_p_value >= SIGN_AGREEMENT_P_THRESHOLD
    ):
        return "棄却(符号一致率が50%と統計的に区別できない)"
    if months_elapsed >= HYPOTHESIS_REJECTION_MIN_MONTHS_COST and not math.isnan(cost_adjusted_ev) and cost_adjusted_ev <= 0:
        return "棄却(コスト控除後期待値が0以下)"
    if n < HYPOTHESIS_REJECTION_MIN_N or months_elapsed < HYPOTHESIS_REJECTION_MIN_MONTHS_SIGN:
        return "判定保留"
    return "有意(仮説と整合的な結果。引き続き蓄積し再評価する)"


# --- レポート生成(§6.5) ----------------------------------------------------------------


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _fmt(x: Any, digits: int = 4) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "N/A"
    if isinstance(x, float):
        return f"{x:.{digits}f}"
    return str(x)


def render_group_report(
    *, group_key: tuple[str, str], records: list[dict[str, Any]], daily_df: pd.DataFrame,
    calibration: dict[str, Any], hypothesis_results: list[dict[str, Any]], total_hypotheses: int,
    min_n_for_judgement: int,
) -> str:
    model, prompt_version = group_key
    lines = [f"## モデル系列: model={model}, prompt_version={prompt_version}", ""]
    lines.append(f"- 蓄積件数(採点イベント数): {len(records)}")
    lines.append(f"- 日次合成スコア件数: {len(daily_df)}")
    lines.append("")

    lines.append("### 採点器の再現性検査(校正, SPEC §4.4)")
    if calibration["monthly_ratios"]:
        cal_rows = [[m, f"{ratio:.1%}"] for m, ratio in sorted(calibration["monthly_ratios"].items())]
        lines.append(_md_table(["月", "範囲>2の比率"], cal_rows))
    else:
        lines.append("(データなし)")
    lines.append("")
    lines.append(f"- 判定: {'**不合格 — 全仮説を評価不能として凍結**' if calibration['failed'] else '合格'}")
    lines.append("")

    lines.append("### 仮説別現況(事前登録済み: hypotheses.md)")
    lines.append(f"- これまでに評価した仮説の総数(Bonferroni補正の分母): {total_hypotheses}")
    lines.append("")

    lines.append("#### バケット間リターン差のブートストラップ検定(§6.3)")
    diff_rows = [
        [
            h["key"],
            h["label"],
            str(h["n"]),
            _fmt(h["diff_vs_baseline"]["diff"]),
            f"[{_fmt(h['diff_vs_baseline']['ci_low'])}, {_fmt(h['diff_vs_baseline']['ci_high'])}]",
            _fmt(h["diff_vs_baseline"]["p_value"]),
            _fmt(h["bonferroni_p_diff"]),
        ]
        for h in hypothesis_results
    ]
    lines.append(_md_table(["仮説", "内容", "N", "平均差(事象−全期間)", "95%信頼区間", "p値", "Bonferroni補正p値"], diff_rows))
    lines.append("")

    lines.append("#### 符号一致率の検定(§1.3 棄却基準)")
    sign_rows = [
        [
            h["key"],
            str(h["n"]),
            _fmt(h["sign_agreement"]["rate"], 3),
            f"[{_fmt(h['sign_agreement']['ci_low'], 3)}, {_fmt(h['sign_agreement']['ci_high'], 3)}]",
            _fmt(h["sign_agreement"]["p_value"]),
            _fmt(h["bonferroni_p_sign"]),
        ]
        for h in hypothesis_results
    ]
    lines.append(_md_table(["仮説", "N", "符号一致率", "95%信頼区間", "p値", "Bonferroni補正p値"], sign_rows))
    lines.append("")

    lines.append("#### 期待値と現況(結論はコスト控除後のみを使用, §6.2/D-4)")
    ev_rows = [
        [
            h["key"],
            f"{_fmt(h['mean_return_pre_cost'])}(参考・コスト控除前)",
            f"**{_fmt(h['cost_adjusted_ev'])}(結論・コスト控除後)**",
            h["status"],
        ]
        for h in hypothesis_results
    ]
    lines.append(_md_table(["仮説", "平均リターン", "コスト控除後期待値", "現況"], ev_rows))
    lines.append("")

    lines.append("### バケット別フォワードリターン(§6.1, 数表のみ)")
    bt = bucket_table(daily_df, min_n_for_judgement=min_n_for_judgement)
    if bt.empty:
        lines.append("(データなし)")
    else:
        bt_rows = [
            [
                r["bucket"],
                str(r["n"]),
                r["judgement"],
                _fmt(r["mean_ret_1d"]),
                _fmt(r["std_ret_1d"]),
                _fmt(r["mean_ret_5d"]),
                _fmt(r["std_ret_5d"]),
                _fmt(r["mean_ret_20d"]),
                _fmt(r["std_ret_20d"]),
            ]
            for _, r in bt.iterrows()
        ]
        lines.append(
            _md_table(
                ["バケット", "N", "判定", "平均1日", "標準偏差1日", "平均5日", "標準偏差5日", "平均20日", "標準偏差20日"],
                bt_rows,
            )
        )
    lines.append("")
    return "\n".join(lines)


def write_exploratory_report(name: str, body_markdown: str, *, reports_dir: Path = REPORTS_DIR / "exploratory") -> Path:
    """探索的分析の出力には自動的に「結論使用不可」の注意書きを挿入する(§6.4, D-6)。"""
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{name}.md"
    path.write_text(EXPLORATORY_DISCLAIMER + body_markdown, encoding="utf-8")
    return path


# --- メイン処理 --------------------------------------------------------------------------


def run_evaluation(config: dict[str, Any], *, month: str, seed: int = 0) -> str:
    records = read_all_records()
    prices_df = pd.read_parquet(PRICES_PATH) if PRICES_PATH.exists() else pd.DataFrame(
        columns=["date", "symbol", "open", "close", "adj_close"]
    )
    symbol = config["prices"]["symbols"][0]["symbol"]
    total_hypotheses = count_registered_hypotheses()
    registration_date = parse_registration_date()
    n_resamples = config["evaluation"]["bootstrap_resamples"]
    min_n_for_judgement = config["evaluation"]["min_n_for_judgement"]
    cost = config["costs"]["round_trip_cost_index"]
    calib_cfg = config["scoring"]["calibration"]

    # 制約B(B-4): model / prompt_version が異なるスコアは決して混ぜない。系列ごとに分離して評価する。
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in records:
        groups.setdefault((r["model"], r["prompt_version"]), []).append(r)

    report_lines = [
        f"# フォワードテスト評価レポート {month}",
        "",
        f"- 生成日時: {datetime.now(JST).isoformat()}",
        f"- 事前登録仮説の総数: {total_hypotheses}(hypotheses.md, 登録日 {registration_date.isoformat()})",
        f"- 対象銘柄/指数: {symbol}",
        "",
        "以下、model / prompt_version の系列ごとに分離して評価する(異なる系列のスコアは混合しない, SPEC制約B)。",
        "",
    ]

    if not groups:
        report_lines.append("(scores.jsonl にレコードがまだありません)")
    else:
        for group_key, group_records in sorted(groups.items()):
            rng = random.Random(seed)
            daily_df = build_daily_dataframe(group_records, prices_df, symbol)
            calibration = calibration_report(
                group_records,
                range_threshold=calib_cfg["range_threshold"],
                failure_rate_threshold=calib_cfg["failure_rate_threshold"],
                lookback_months=calib_cfg["lookback_months"],
            )

            latest_date = max(datetime.fromisoformat(r["scored_at"]).astimezone(JST).date() for r in group_records)
            months_elapsed = (latest_date - registration_date).days / 30.44

            hypothesis_results = [
                evaluate_hypothesis(
                    daily_df,
                    spec,
                    cost=cost,
                    n_resamples=n_resamples,
                    rng=rng,
                    calibration_failed=calibration["failed"],
                    months_elapsed=months_elapsed,
                    total_hypotheses=total_hypotheses,
                )
                for spec in HYPOTHESIS_SPECS
            ]

            report_lines.append(
                render_group_report(
                    group_key=group_key,
                    records=group_records,
                    daily_df=daily_df,
                    calibration=calibration,
                    hypothesis_results=hypothesis_results,
                    total_hypotheses=total_hypotheses,
                    min_n_for_judgement=min_n_for_judgement,
                )
            )

    return "\n".join(report_lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--month", default=datetime.now(JST).strftime("%Y-%m"), help="レポート対象月 (YYYY-MM)")
    args = parser.parse_args()

    config = load_config()
    report = run_evaluation(config, month=args.month)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORTS_DIR / f"{args.month}.md"
    out_path.write_text(report, encoding="utf-8")
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
