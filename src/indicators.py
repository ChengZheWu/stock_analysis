"""技術指標計算。

所有指標都只使用當日與之前的資料，計算後再由策略層整體延遲一日使用，
確保不會出現未來函數。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from config import (ATR_PERIOD, BREAKOUT_LOOKBACK, EXIT_MA, MOM_LONG, MOM_MID,
                    MOM_SHORT, MOM_SKIP, MOM_WEIGHTS, TT_MA_LONG,
                    TT_MA_LONG_RISING_DAYS, TT_MA_MID, TT_MA_SHORT)


def atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    """Wilder 平均真實區間。"""
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def momentum(close: pd.Series, lookback: int, skip: int = 0) -> pd.Series:
    """區間報酬率；skip 用來排除最近 N 日，避免短期反轉效應。"""
    end = close.shift(skip)
    start = close.shift(lookback)
    return end / start - 1.0


def compute(df: pd.DataFrame) -> pd.DataFrame:
    """對單檔股票計算全部指標。輸入須為已還原、依日期排序的資料。"""
    out = df.copy()
    close = out["close"]

    for ma in (TT_MA_SHORT, TT_MA_MID, TT_MA_LONG, EXIT_MA):
        out[f"ma{ma}"] = close.rolling(ma, min_periods=ma).mean()

    out["ma_long_prev"] = out[f"ma{TT_MA_LONG}"].shift(TT_MA_LONG_RISING_DAYS)
    out["atr"] = atr(out)

    out["high_52w"] = out["high"].rolling(252, min_periods=200).max()
    out["low_52w"] = out["low"].rolling(252, min_periods=200).min()
    # 收盤時已知當日高點，因此觸發價含當日；隔日須突破這個價位才進場
    out["breakout_high"] = out["high"].rolling(BREAKOUT_LOOKBACK, min_periods=BREAKOUT_LOOKBACK).max()

    out["mom_long"] = momentum(close, MOM_LONG, MOM_SKIP)
    out["mom_mid"] = momentum(close, MOM_MID, MOM_SKIP)
    out["mom_short"] = momentum(close, MOM_SHORT)

    w_long, w_mid, w_short = MOM_WEIGHTS
    out["mom_score"] = (w_long * out["mom_long"] + w_mid * out["mom_mid"] + w_short * out["mom_short"])

    # 以年化波動度調整動能分數，偏好「穩定上漲」而非「暴漲暴跌」
    daily_ret = close.pct_change()
    out["volatility"] = daily_ret.rolling(63, min_periods=40).std() * np.sqrt(252)
    out["mom_adj"] = out["mom_score"] / out["volatility"].clip(lower=0.05)

    out["pct_from_high"] = close / out["high_52w"] - 1.0
    out["pct_above_low"] = close / out["low_52w"] - 1.0
    return out


def add_indicators(panel: pd.DataFrame, min_rows: int = 260) -> pd.DataFrame:
    """對整個面板逐檔計算指標，並剔除資料長度不足以暖身的股票。"""
    frames = []
    for sid, grp in panel.groupby("stock_id", sort=False):
        if len(grp) < min_rows:
            continue
        frames.append(compute(grp.sort_values("date").reset_index(drop=True)))
    return pd.concat(frames, ignore_index=True)
