"""台指期連續合約序列與微台交易成本。

微台（TMF）2024 年才推出，歷史太短不足以回測；但微台、小台、大台追蹤
同一個標的指數，價格幾乎一致，因此用小台（MTX）建立十年連續序列，
再套用微台的每點金額與成本即可。

轉倉採「近月合約 + 到期前切換」：接續時以價差調整，避免換月缺口被
誤判為真實漲跌。
"""

from __future__ import annotations

import pandas as pd

from config import CACHE
from src import finmind

# ---------------------------------------------------------------- 契約規格
POINT_VALUE = {"TX": 200, "MTX": 50, "TMF": 10}      # 每點新台幣
TAX_RATE = 0.00002                                   # 期交稅：契約金額的十萬分之二，買賣各一次
TICK = 1                                             # 最小跳動一點

CONT_PATH = CACHE / "futures_continuous.parquet"


def load_raw(symbol: str = "MTX", start: str = "2016-01-01",
             end: str | None = None, refresh: bool = False) -> pd.DataFrame:
    end = end or pd.Timestamp.today().strftime("%Y-%m-%d")
    path = CACHE / f"futures_{symbol}.parquet"
    return finmind.cached(path, "TaiwanFuturesDaily", refresh=refresh,
                          data_id=symbol, start_date=start, end_date=end)


def _expiry(contract: str) -> pd.Timestamp:
    """台指期的最後交易日為契約月份的第三個星期三。"""
    year, month = int(contract[:4]), int(contract[4:6])
    first = pd.Timestamp(year=year, month=month, day=1)
    # 第一個星期三往後推兩週
    offset = (2 - first.weekday()) % 7
    return first + pd.Timedelta(days=offset + 14)


def build_continuous(symbol: str = "MTX", roll_days_before: int = 1,
                     refresh: bool = False) -> pd.DataFrame:
    """組成價差調整後的連續序列。

    只取日盤、單一月份合約（排除價差商品與週選），每日選用未到期的最近月，
    並在到期前 roll_days_before 個交易日換倉。
    """
    if CONT_PATH.exists() and not refresh:
        return pd.read_parquet(CONT_PATH)

    raw = load_raw(symbol, refresh=refresh)
    raw = raw.rename(columns={"max": "high", "min": "low"})
    raw["date"] = pd.to_datetime(raw["date"])

    # 排除價差商品（contract_date 含 "/"）與週選（含 "W"）
    cd = raw["contract_date"].astype(str)
    raw = raw[~cd.str.contains("/") & ~cd.str.contains("W")]
    if "trading_session" in raw.columns:
        raw = raw[raw["trading_session"] == "position"]      # 只取日盤
    raw = raw[raw[["open", "high", "low", "close"]].gt(0).all(axis=1)]
    raw["contract"] = raw["contract_date"].astype(str)

    # 到期日以合約月份的第三個星期三推算，不能用「資料中最後出現的日期」：
    # 尚未到期的合約其最後出現日就是資料結束日，會被誤判為即將到期。
    last_day = {c: _expiry(c) for c in raw["contract"].unique()}

    rows = []
    for date, grp in raw.groupby("date"):
        grp = grp.sort_values("contract")
        # 取尚未接近到期的最近月合約
        for _, r in grp.iterrows():
            remaining = (last_day[r["contract"]] - date).days
            if remaining > roll_days_before:
                rows.append(r)
                break
        else:
            rows.append(grp.iloc[-1])

    df = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)

    # 換月時以前後合約的收盤價差調整歷史，讓序列連續
    df["roll"] = df["contract"] != df["contract"].shift(1)
    adj = 0.0
    offsets = []
    for i in range(len(df) - 1, -1, -1):
        offsets.append(adj)
        if i > 0 and df.loc[i, "roll"]:
            prev_close = raw[(raw["date"] == df.loc[i - 1, "date"]) &
                             (raw["contract"] == df.loc[i, "contract"])]["close"]
            if len(prev_close):
                adj += df.loc[i - 1, "close"] - float(prev_close.iloc[0])
    df["offset"] = list(reversed(offsets))

    for col in ("open", "high", "low", "close"):
        df[col] = df[col] - df["offset"]

    out = df[["date", "open", "high", "low", "close", "volume", "contract"]].copy()
    out.to_parquet(CONT_PATH, index=False)
    return out


def round_trip_cost(index_level: float, symbol: str = "TMF",
                    fee_per_side: float = 15.0, spread_ticks: float = 1.0) -> float:
    """單口來回成本（新台幣）：手續費兩邊 + 期交稅兩邊 + 吃價差。"""
    mult = POINT_VALUE[symbol]
    tax = index_level * mult * TAX_RATE * 2
    return fee_per_side * 2 + tax + spread_ticks * TICK * mult
