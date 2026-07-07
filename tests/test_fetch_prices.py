"""fetch_prices.py のガードのテスト(INSTRUCTION_002 タスク2)。

- 2-a: 16:00 JSTより前の実行では未確定の当日行を保存しない
- 2-b: prices.parquetが存在しない/総行数0はブートストラップ失敗として扱う(main()側)
"""

from datetime import datetime

import pandas as pd

import fetch_prices
from common import JST


def _fake_history(rows):
    def _fetch(symbol, *, start=None, period=None):
        return pd.DataFrame(rows)

    return _fetch


def test_intraday_row_excluded_before_close(tmp_path, monkeypatch):
    """15:00 JST(引け前)実行では当日行が除外され、ファイルも作成されない。"""
    now = datetime(2026, 7, 7, 15, 0, tzinfo=JST)
    today_str = now.date().isoformat()
    monkeypatch.setattr(
        fetch_prices,
        "fetch_symbol_history",
        _fake_history([{"date": today_str, "symbol": "^N225", "open": 100.0, "close": 101.0, "adj_close": 101.0}]),
    )

    config = {"prices": {"symbols": [{"symbol": "^N225", "kind": "index"}]}}
    path = tmp_path / "prices.parquet"
    summary = fetch_prices.update_prices(config, path=path, now=now)

    assert summary["skipped_intraday"] == 1
    assert summary["added"] == 0
    assert not path.exists()


def test_same_day_row_saved_after_close(tmp_path, monkeypatch):
    """16:30 JST(引け後)実行では当日行が保存される。"""
    now = datetime(2026, 7, 7, 16, 30, tzinfo=JST)
    today_str = now.date().isoformat()
    monkeypatch.setattr(
        fetch_prices,
        "fetch_symbol_history",
        _fake_history([{"date": today_str, "symbol": "^N225", "open": 100.0, "close": 101.0, "adj_close": 101.0}]),
    )

    config = {"prices": {"symbols": [{"symbol": "^N225", "kind": "index"}]}}
    path = tmp_path / "prices.parquet"
    summary = fetch_prices.update_prices(config, path=path, now=now)

    assert summary["skipped_intraday"] == 0
    assert summary["added"] == 1
    assert path.exists()
