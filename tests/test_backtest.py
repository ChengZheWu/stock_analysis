"""回測引擎的行為測試。"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from src import backtest


def _panel(rows: list[dict]) -> pd.DataFrame:
    """組出最小可用的訊號面板。"""
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    for col in ("volume",):
        df[col] = df.get(col, 1_000_000)
    return df


def _taiex(dates: list[str], price: float = 20000.0) -> pd.DataFrame:
    """大盤濾網恆為多頭的假資料（價格長期持平即高於均線）。"""
    d = pd.date_range(pd.Timestamp(dates[0]) - pd.Timedelta(days=400), dates[-1], freq="D")
    return pd.DataFrame({"date": d, "price": np.linspace(price * 0.5, price, len(d))})


def test_position_take_splits_proportionally():
    """部分出場應依單位比例切分股數與成本。"""
    pos = backtest.Position(
        stock_id="2330", entry_date=pd.Timestamp("2026-01-05"), entry_price=100.0,
        shares=400, entry_atr=3.0, highest_close=100.0, cost=40_000.0, j=0, units=4)

    shares, cost = pos.take(2)
    assert shares == 200
    assert cost == pytest.approx(20_000.0)
    assert pos.shares == 200
    assert pos.cost == pytest.approx(20_000.0)
    assert pos.units == 2


def test_position_take_never_exceeds_holding():
    """要求賣出的單位數超過持有時，最多賣光，不得產生負股數。"""
    pos = backtest.Position(
        stock_id="2330", entry_date=pd.Timestamp("2026-01-05"), entry_price=100.0,
        shares=100, entry_atr=3.0, highest_close=100.0, cost=10_000.0, j=0, units=1)

    shares, _ = pos.take(5)
    assert shares == 100
    assert pos.shares == 0
    assert pos.units == 0


def test_add_uses_weighted_average_cost():
    """加碼後成本須為加權平均，而非最後一次買價。"""
    pos = backtest.Position(
        stock_id="2330", entry_date=pd.Timestamp("2026-01-05"), entry_price=100.0,
        shares=100, entry_atr=3.0, highest_close=100.0, cost=10_000.0, j=0, units=1)

    pos.add(price=200.0, shares=100, atr=4.0, fee=0.0, close=200.0)
    assert pos.entry_price == pytest.approx(150.0)
    assert pos.shares == 200
    assert pos.units == 2
    assert pos.entry_atr == 4.0          # 停損改用最近一次進場的 ATR


def test_scale_out_disabled_by_default():
    """預設為跌破均線整檔出清；分批減碼是選用行為。"""
    import inspect
    assert inspect.signature(backtest.run).parameters["scale_out"].default is False


def test_cell_aligns_by_display_width():
    """含中文的表頭需依顯示寬度補齊。

    中日韓字元在等寬字型佔兩格，但 len() 只算一格；若以字元數補齊，
    表頭與數字欄位會錯開（實測曾差 11 格）。
    """
    from src.notify import _cell, _width

    assert _width("順位") == 4          # 兩個中文字 = 四格
    assert _width("3037") == 4
    assert _width("代號") == 4

    header = _cell("代號", 6) + _cell("名稱", 11)
    row = _cell("3037", 6) + _cell("欣興", 11)
    assert _width(header) == _width(row)


def test_truncate_respects_display_width():
    """過長的股名須依顯示寬度截斷，避免撐破欄位。"""
    from src.notify import _truncate, _width

    assert _truncate("北極星藥業-KY", 10) == "北極星藥業"
    assert _width(_truncate("北極星藥業-KY", 10)) <= 10
    assert _truncate("欣興", 10) == "欣興"       # 未超寬者不變
