"""驗證還原股價與回測引擎的正確性。

還原價的驗證方式是與 Yahoo Finance 的 Adj Close 對比累積報酬。
兩者的還原慣例不同（基準點、四捨五入），因此比對「正規化後的走勢」而非絕對價位。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import adjust, backtest, corporate, strategy

SAMPLE = ["2330", "2317", "2454", "1301", "2412"]


@pytest.fixture(scope="module")
def events():
    return corporate.build()


def test_tick_rounding():
    assert strategy.round_to_tick(9.997, up=True) == pytest.approx(10.0)
    assert strategy.round_to_tick(23.31, up=True) == pytest.approx(23.35)
    assert strategy.round_to_tick(23.34, up=False) == pytest.approx(23.30)
    assert strategy.round_to_tick(101.2, up=True) == pytest.approx(101.5)
    assert strategy.round_to_tick(1001.0, up=True) == pytest.approx(1005.0)


def test_ohlc_internally_consistent():
    """還原後的 OHLC 必須自洽：開收盤都落在當日高低點之間。

    FinMind 對興櫃期間會以前一日均價填充開盤價，導致開盤價高於最高價；
    改用官方每日報表後不應再出現這種情況。
    """
    panel = adjust.build_panel()
    tol = 1e-6
    bad_open = ((panel["open"] > panel["high"] * (1 + tol)) |
                (panel["open"] < panel["low"] * (1 - tol))).sum()
    bad_close = ((panel["close"] > panel["high"] * (1 + tol)) |
                 (panel["close"] < panel["low"] * (1 - tol))).sum()
    assert bad_open == 0, f"{bad_open} 列的開盤價落在高低點之外"
    assert bad_close == 0, f"{bad_close} 列的收盤價落在高低點之外"


def test_adjusted_prices_are_continuous(events):
    """還原後不應殘留除權息造成的跳空缺口。"""
    panel = adjust.build_panel()
    for sid in SAMPLE:
        df = panel[panel["stock_id"] == sid]
        if len(df) < 100:
            continue
        ret = df["close"].pct_change().dropna()
        # 台股漲跌幅上限 10%，還原後單日變動不應大幅超過此範圍
        assert ret.abs().max() < 0.25, f"{sid} 出現異常跳空 {ret.abs().max():.1%}"


@pytest.mark.parametrize("sid,yahoo", [("2330", "2330.TW"), ("2412", "2412.TW")])
def test_matches_yahoo(sid, yahoo, events):
    """與 Yahoo 還原價的累積報酬比對，差異應在可接受範圍。"""
    yf = pytest.importorskip("yfinance")
    panel = adjust.build_panel()
    mine = panel[panel["stock_id"] == sid]
    if mine.empty:
        pytest.skip(f"{sid} 無本地資料")

    ext = yf.download(yahoo, start="2016-09-01", end="2026-09-19",
                      auto_adjust=False, progress=False)
    if ext.empty:
        pytest.skip("Yahoo 無回應")
    if isinstance(ext.columns, pd.MultiIndex):
        ext.columns = [c[0] for c in ext.columns]

    a = mine.set_index("date")["close"]
    b = ext["Adj Close"]
    both = pd.concat([a.rename("mine"), b.rename("yahoo")], axis=1).dropna()
    both = both[both.index >= "2016-09-01"]
    assert len(both) > 1000

    # 各自正規化到最後一天，比較整段走勢
    norm = both / both.iloc[-1]
    diff = (norm["mine"] / norm["yahoo"] - 1).abs()
    assert diff.mean() < 0.02, f"{sid} 平均偏差 {diff.mean():.2%} 過大"
    assert diff.max() < 0.10, f"{sid} 最大偏差 {diff.max():.2%} 過大"


def test_breakout_uses_only_known_data():
    """觸發價須等於「含當日」的 20 日高點：收盤時已知，隔日執行不構成未來函數。"""
    from src import indicators
    n = 300
    rng = np.random.default_rng(0)
    close = pd.Series(100 + np.cumsum(rng.normal(0, 1, n)))
    df = pd.DataFrame({
        "date": pd.bdate_range("2020-01-01", periods=n),
        "open": close, "high": close * 1.01, "low": close * 0.99,
        "close": close, "volume": 1000,
    })
    out = indicators.compute(df)
    for i in range(250, n):
        expected = df["high"].iloc[i - 19:i + 1].max()
        assert out["breakout_high"].iloc[i] == pytest.approx(expected)
        # 絕不可用到隔日以後的資料
        assert out["breakout_high"].iloc[i] <= df["high"].iloc[:i + 1].max()


def test_fees_and_tax():
    amount = 100_000
    assert backtest.buy_fee(amount) == pytest.approx(amount * 0.001425 * 0.6)
    expected_sell = amount * 0.001425 * 0.6 + amount * 0.003
    assert backtest.sell_cost(amount) == pytest.approx(expected_sell)
    # 零股小額交易須套用最低手續費
    assert backtest.buy_fee(100) == pytest.approx(1.0)
