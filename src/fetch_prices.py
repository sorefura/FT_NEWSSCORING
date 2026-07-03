"""対象銘柄/指数の日足を取得し、prices.parquet を更新する(SPEC.md §5)。

SPEC §0.3(記録は追記専用)に倣い、既に保存済みの (date, symbol) 行を書き換える
コードは書かない。新しい日付の行のみを追加する。
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import yfinance as yf

from common import PRICES_PATH, load_config

COLUMNS = ["date", "symbol", "open", "close", "adj_close"]
DEFAULT_INITIAL_PERIOD = "10y"


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


def update_prices(config: dict[str, Any], *, path: Path = PRICES_PATH) -> dict[str, int]:
    """新規日付の行のみを追加する。既存行の値を上書きするコードパスは持たない。"""
    existing = _load_existing(path)
    existing_keys = set(zip(existing["date"], existing["symbol"])) if not existing.empty else set()

    new_rows: list[pd.Series] = []
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

    return {"added": added, "total": len(existing) + added}


def main() -> None:
    argparse.ArgumentParser(description=__doc__).parse_args()
    config = load_config()
    summary = update_prices(config)
    print(summary)


if __name__ == "__main__":
    main()
