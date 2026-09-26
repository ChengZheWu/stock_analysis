"""選股規則與買賣價位計算。

流程：大盤濾網 → 趨勢模板篩選 → 動能排名 → 取前 N 名為候選 →
算出明日的突破觸發價。所有判斷只用到當日收盤為止的資訊。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from config import (ATR_INIT_STOP, ATR_TRAIL_STOP, ENTRY_MAX_ATR_GAP,
                    MKT_FILTER_MA, RANK_TOP_N, TT_MA_LONG,
                    TT_MA_LONG_RISING_DAYS, TT_MA_MID, TT_MA_SHORT,
                    TT_MAX_BELOW_52W_HIGH, TT_MIN_ABOVE_52W_LOW)

# 台股升降單位
_TICKS = [(10, 0.01), (50, 0.05), (100, 0.1), (500, 0.5), (1000, 1.0), (np.inf, 5.0)]


def tick_size(price: float) -> float:
    for bound, tick in _TICKS:
        if price < bound:
            return tick
    return 5.0


def round_to_tick(price: float, up: bool = True) -> float:
    """把價格對齊到合法的升降單位。買進觸發價無條件進位，賣出無條件捨去。"""
    if not np.isfinite(price) or price <= 0:
        return price
    t = tick_size(price)
    steps = price / t
    return round((np.ceil(steps) if up else np.floor(steps)) * t, 4)


def market_regime(taiex: pd.DataFrame, ma: int = MKT_FILTER_MA) -> pd.Series:
    """大盤濾網：加權指數在長期均線之上才允許開新倉。"""
    idx = taiex.set_index("date")["price"]
    return (idx > idx.rolling(ma, min_periods=ma).mean()).rename("market_ok")


def trend_template(df: pd.DataFrame) -> pd.Series:
    """Minervini 趨勢模板。輸入須已含 indicators.compute 產出的欄位。"""
    close = df["close"]
    cond = (
        (close > df[f"ma{TT_MA_SHORT}"])
        & (df[f"ma{TT_MA_SHORT}"] > df[f"ma{TT_MA_MID}"])
        & (df[f"ma{TT_MA_MID}"] > df[f"ma{TT_MA_LONG}"])
        & (df[f"ma{TT_MA_LONG}"] > df["ma_long_prev"])          # 200 日均線上升中
        & (df["pct_above_low"] >= TT_MIN_ABOVE_52W_LOW)
        & (df["pct_from_high"] >= -TT_MAX_BELOW_52W_HIGH)
        & df["atr"].notna()
        & df["breakout_high"].notna()
        # 須貼近突破價才算突破待發，避免掛出不可能成交的遠單
        & ((df["breakout_high"] - close) <= ENTRY_MAX_ATR_GAP * df["atr"])
    )
    return cond.fillna(False)


def add_signals(panel: pd.DataFrame) -> pd.DataFrame:
    """在面板上加入 eligible（通過篩選）與 trigger（明日突破觸發價）。"""
    out = panel.copy()
    out["eligible"] = trend_template(out)
    trigger = out["breakout_high"].astype(float)
    out["trigger"] = [round_to_tick(p, up=True) if np.isfinite(p) else np.nan for p in trigger]
    return out


def rank_candidates(day_slice: pd.DataFrame, top_n: int = RANK_TOP_N) -> pd.DataFrame:
    """對單日通過篩選的股票排名，回傳前 top_n 名。"""
    ok = day_slice[day_slice["eligible"] & day_slice["mom_adj"].notna()]
    if ok.empty:
        return ok
    return ok.nlargest(top_n, "mom_adj")


def stop_level(entry_price: float, entry_atr: float, highest_close: float, atr_now: float) -> float:
    """出場價 = 初始停損與移動停損取較高者。"""
    initial = entry_price - ATR_INIT_STOP * entry_atr
    trailing = highest_close - ATR_TRAIL_STOP * atr_now
    return round_to_tick(max(initial, trailing), up=False)
