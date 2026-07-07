"""対象銘柄/指数の日足を取得し、prices.parquet を更新する(SPEC.md §5)。

SPEC §0.3(記録は追記専用)に倣い、既に保存済みの (date, symbol) 行を書き換える
コードは書かない。新しい日付の行のみを追加する。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, time
from pathlib import Path
from typing import Any

import pandas as pd
import yfinance as yf

from common import JST, PRICES_PATH, load_config

COLUMNS = ["date", "symbol", "open", "close", "adj_close"]
DEFAULT_INITIAL_PERIOD = "10y"

# SPEC §5注記: 東証現物は15:30引け。16:00 JST を境界にすれば、大引け後の定時実行
# (16:30 JST)は当日確定値を取得でき、朝の実行(07:00 JST)や場中の手動実行は
# 未確定の当日行を書かない(INSTRUCTION_002 タスク2-a)。
INTRADAY_CUTOFF_JST = time(16, 0)


def _load_existing(path: Path = PRICES_PATH) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=COLUMNS)
    return pd.read_parquet(path)


def fetch_symbol_history(symbol: str, *, start: str | None = None, period: str | None = None) -> pd.DataFrame:
    ticker = yf.Ticker(symbol)
    if start:
        hist = ticker.history(start=start, auto_adjust=False)
    else:
        hist = ticker.history(period=period or DEFAULT_INITIAL_PERIOD, auto_adjust=False)

    if hist.empty:
        return pd.DataFrame(columns=COLUMNS)

    hist = hist.reset_index()
    dates = pd.to_datetime(hist["Date"]).dt.strftime("%Y-%m-%d")
    adj_close = hist["Adj Close"] if "Adj Close" in hist.columns else hist["Close"]
    out = pd.DataFrame(
        {
            "date": dates,
            "symbol": symbol,
            "open": hist["Open"],
            "close": hist["Close"],
            "adj_close": adj_close,
        }
    )
    return out[COLUMNS]


def update_prices(
    config: dict[str, Any], *, path: Path = PRICES_PATH, now: datetime | None = None
) -> dict[str, int]:
    """新規日付の行のみを追加する。既存行の値を上書きするコードパスは持たない。

    16:00 JSTより前の実行では、東証現物の引け(15:30)未到来につき未確定の
    当日行を保存しない(INSTRUCTION_002 タスク2-a)。
    """
    now_jst = (now or datetime.now(JST)).astimezone(JST)
    today_str = now_jst.date().isoformat()
    is_after_close = now_jst.time() >= INTRADAY_CUTOFF_JST

    existing = _load_existing(path)
    existing_keys = set(zip(existing["date"], existing["symbol"])) if not existing.empty else set()

    new_rows: list[pd.Series] = []
    skipped_intraday = 0
    for symbol_cfg in config["prices"]["symbols"]:
        symbol = symbol_cfg["symbol"]

        last_date = None
        if not existing.empty:
            symbol_rows = existing[existing["symbol"] == symbol]
            if not symbol_rows.empty:
                last_date = symbol_rows["date"].max()

        if last_date:
            start = (pd.Timestamp(last_date) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
            fetched = fetch_symbol_history(symbol, start=start)
        else:
            fetched = fetch_symbol_history(symbol, period=DEFAULT_INITIAL_PERIOD)

        for _, row in fetched.iterrows():
            if row["date"] == today_str and not is_after_close:
                skipped_intraday += 1
                continue
            key = (row["date"], row["symbol"])
            if key not in existing_keys:
                new_rows.append(row)
                existing_keys.add(key)

    added = len(new_rows)
    if new_rows:
        combined = pd.concat([existing, pd.DataFrame(new_rows)], ignore_index=True)
        combined = combined.sort_values(["symbol", "date"]).reset_index(drop=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        combined.to_parquet(path, index=False)

    return {"added": added, "total": len(existing) + added, "skipped_intraday": skipped_intraday}


def main() -> None:
    argparse.ArgumentParser(description=__doc__).parse_args()
    config = load_config()
    summary = update_prices(config)
    print(json.dumps(summary, ensure_ascii=False))

    # ブートストラップ失敗検知(INSTRUCTION_002 タスク2-b)。
    # added==0 自体は土日祝で正常なため異常条件にしない。ファイルが存在しない、
    # または総行数が0の場合のみサイレント失敗にしない。
    if not PRICES_PATH.exists() or summary["total"] == 0:
        print(
            "[fetch_prices.py] prices.parquet が存在しないか総行数0です。取得処理を確認してください。",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
