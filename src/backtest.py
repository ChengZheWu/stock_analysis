"""投組回測引擎。

時序嚴格遵守：第 t 日收盤後產生訊號與觸發價，第 t+1 日才可能成交。
進場模擬停損買單（觸及最高價才成交），出場模擬停損賣單（觸及最低價才成交），
跳空時以開盤價成交。同一日同時觸及進出場價時一律採不利假設。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from config import (ATR_INIT_STOP, ATR_TRAIL_STOP, FEE_DISCOUNT, FEE_MIN,
                    FEE_RATE, INITIAL_CAPITAL, MAX_NEW_PER_DAY, MAX_POSITIONS,
                    MAX_UNITS_PER_STOCK, MIN_POSITION_VALUE, POSITION_PCT,
                    RANK_TOP_N, SLIPPAGE, TAX_RATE)
from src.strategy import market_regime, round_to_tick

_FIELDS = ("open", "high", "low", "close", "atr", "ma50", "trigger", "mom_adj")


@dataclass
class Position:
    """單一檔股票的部位。多次加碼合併為一個部位，以均價計算，出場時全數賣出。"""
    stock_id: str
    entry_date: pd.Timestamp         # 第一次買進日
    entry_price: float               # 加權平均成本（不含費用）
    shares: int                      # 累計股數
    entry_atr: float                 # 最近一次買進時的 ATR
    highest_close: float             # 首次買進以來的最高收盤
    cost: float                      # 含手續費的總成本
    j: int = -1                      # 股票在矩陣中的索引
    units: int = 1                   # 買進次數
    last_entry_price: float = 0.0    # 最近一次買進價，供加碼條件判斷

    def add(self, price: float, shares: int, atr: float, fee: float, close: float) -> None:
        """加碼：股數累加，成本價改以加權平均計算。"""
        total = self.shares + shares
        self.entry_price = (self.entry_price * self.shares + price * shares) / total
        self.shares = total
        self.cost += price * shares + fee
        self.entry_atr = atr
        self.last_entry_price = price
        self.highest_close = max(self.highest_close, close)
        self.units += 1

    def stop(self, atr_now: float) -> float:
        """出場價：初始停損以均價計算，與移動停損取較高者。"""
        initial = self.entry_price - ATR_INIT_STOP * self.entry_atr
        trailing = self.highest_close - ATR_TRAIL_STOP * atr_now
        return round_to_tick(max(initial, trailing), up=False)


@dataclass
class Trade:
    stock_id: str
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry_price: float
    exit_price: float
    shares: int
    pnl: float
    pnl_pct: float
    hold_days: int
    reason: str
    units: int = 1                   # 買進次數（>1 表示加碼過，entry_price 為均價）


@dataclass
class Result:
    equity: pd.Series
    trades: pd.DataFrame
    daily_candidates: dict = field(default_factory=dict)


def buy_fee(amount: float) -> float:
    return max(amount * FEE_RATE * FEE_DISCOUNT, FEE_MIN)


def sell_cost(amount: float) -> float:
    return max(amount * FEE_RATE * FEE_DISCOUNT, FEE_MIN) + amount * TAX_RATE


def to_matrices(panel: pd.DataFrame) -> tuple[pd.DatetimeIndex, np.ndarray, dict, np.ndarray]:
    """把長表轉成 date × stock 的矩陣，讓每日迴圈能用純 numpy 取值。"""
    panel = panel.sort_values(["date", "stock_id"])
    dates = pd.DatetimeIndex(np.sort(panel["date"].unique()))
    stocks = np.sort(panel["stock_id"].unique())
    sidx = {s: i for i, s in enumerate(stocks)}

    di = panel["date"].map({d: i for i, d in enumerate(dates)}).to_numpy()
    si = panel["stock_id"].map(sidx).to_numpy()

    mats = {}
    for f in _FIELDS:
        m = np.full((len(dates), len(stocks)), np.nan, dtype=np.float32)
        m[di, si] = panel[f].to_numpy(dtype=np.float32)
        mats[f] = m

    elig = np.zeros((len(dates), len(stocks)), dtype=bool)
    elig[di, si] = panel["eligible"].to_numpy()
    return dates, stocks, mats, elig


def run(panel: pd.DataFrame, taiex: pd.DataFrame, start: str, end: str,
        capital: float = INITIAL_CAPITAL, top_n: int = RANK_TOP_N,
        max_new: int = MAX_NEW_PER_DAY, max_positions: int = MAX_POSITIONS,
        max_units: int = MAX_UNITS_PER_STOCK, alloc_per_unit: float | None = None,
        add_advance_atr: float = 0.0, fixed_amount: float | None = None,
        record_candidates: bool = False) -> Result:
    """
    max_units        單一檔股票最多買進幾個單位（1 代表不加碼）
    alloc_per_unit   每個單位佔淨值的比例，預設為 POSITION_PCT / max_units
    add_advance_atr  加碼條件：價格須較前一次進場上漲這麼多個 ATR（0 代表不限制）
    fixed_amount     每筆固定投入金額；設定後即忽略 alloc_per_unit。
                     注意：資金成長後固定金額會使投入比例逐年下降。
    """
    unit_pct = alloc_per_unit if alloc_per_unit is not None else POSITION_PCT / max_units
    dates, stocks, mats, elig = to_matrices(panel)

    regime = market_regime(taiex).reindex(dates).ffill().fillna(False).to_numpy()
    in_range = (dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))
    day_idx = np.flatnonzero(in_range)
    if len(day_idx) == 0:
        raise ValueError(f"區間內沒有交易日: {start} ~ {end}")

    o, h, l, c = mats["open"], mats["high"], mats["low"], mats["close"]
    atr_m, ma_m, trig_m, score_m = mats["atr"], mats["ma50"], mats["trigger"], mats["mom_adj"]
    has_data = ~np.isnan(c)
    # 估算淨值時用最後已知收盤價，避免停止交易的持股在清算前被當成零值
    c_valued = pd.DataFrame(c).ffill().to_numpy(dtype=np.float32)

    cash = capital
    positions: list[Position] = []          # 同一檔可持有多個單位
    trades: list[Trade] = []
    equity = np.zeros(len(day_idx))
    pending: list[tuple[int, float]] = []       # 前一日決定的 (股票索引, 觸發價)
    ma_exit_flags: set[int] = set()
    candidates_log: dict = {}

    for n, t in enumerate(day_idx):
        # ---------------------------------------------------------- 出場
        survivors: list[Position] = []
        for pos in positions:
            j = pos.j
            if not has_data[t, j]:
                prev = t - 1
                while prev >= 0 and not has_data[prev, j]:
                    prev -= 1
                if prev < 0 or (t - prev) < 5:
                    survivors.append(pos)      # 只是當日無量，先留著
                    continue
                px = float(c[prev, j])
                proceeds = px * pos.shares - sell_cost(px * pos.shares)
                cash += proceeds
                trades.append(_close(pos, dates[t], px, proceeds, "下市/停止交易"))
                continue

            atr_now = float(atr_m[t - 1, j]) if t > 0 and not np.isnan(atr_m[t - 1, j]) else pos.entry_atr
            stop = pos.stop(atr_now)
            exit_px = None
            if float(l[t, j]) <= stop:
                exit_px, reason = min(float(o[t, j]), stop), "移動停損"
            elif j in ma_exit_flags:
                exit_px, reason = float(o[t, j]), "跌破50日均線"

            if exit_px is None:
                survivors.append(pos)
                continue

            exit_px *= (1 - SLIPPAGE)
            proceeds = exit_px * pos.shares - sell_cost(exit_px * pos.shares)
            cash += proceeds
            trades.append(_close(pos, dates[t], exit_px, proceeds, reason))

        positions = survivors
        ma_exit_flags.clear()

        # ---------------------------------------------------------- 進場
        book: dict[int, Position] = {p.j: p for p in positions}
        filled = 0
        # 盤中下單時只知道前一日收盤，用它估算淨值以決定部位大小
        prev = max(t - 1, 0)
        equity_now = cash + sum(p.shares * float(c_valued[prev, p.j]) for p in positions)

        for j, trigger in pending:
            if filled >= max_new or not has_data[t, j]:
                continue
            existing = book.get(j)
            if existing is not None:
                if existing.units >= max_units:
                    continue               # 該檔已買滿
                if add_advance_atr > 0:
                    # 須較前次進場上漲一定幅度才加碼，避免在原地反覆買進
                    ref = existing.last_entry_price or existing.entry_price
                    if trigger < ref + add_advance_atr * existing.entry_atr:
                        continue
            elif max_positions is not None and len(book) >= max_positions:
                continue                   # 名額已滿，且非加碼

            if float(h[t, j]) < trigger:
                continue                   # 未觸及觸發價，不成交
            fill = max(float(o[t, j]), trigger) * (1 + SLIPPAGE)
            target = fixed_amount if fixed_amount is not None else unit_pct * max(equity_now, 0.0)
            budget = min(target, cash)
            shares = int(budget // fill)
            if shares <= 0:
                continue
            amount = fill * shares
            if amount < MIN_POSITION_VALUE:
                continue
            fee = buy_fee(amount)
            if amount + fee > cash:
                continue
            cash -= amount + fee
            entry_atr = float(atr_m[t - 1, j]) if t > 0 and not np.isnan(atr_m[t - 1, j]) else fill * 0.05

            if existing is not None:
                existing.add(fill, shares, entry_atr, fee, float(c[t, j]))
            else:
                pos = Position(str(stocks[j]), dates[t], fill, shares, entry_atr,
                               float(c[t, j]), amount + fee, j=j, last_entry_price=fill)
                positions.append(pos)
                book[j] = pos
            filled += 1

        # ---------------------------------------------------------- 更新持股狀態
        for pos in positions:
            j = pos.j
            if has_data[t, j]:
                pos.highest_close = max(pos.highest_close, float(c[t, j]))
                if not np.isnan(ma_m[t, j]) and float(c[t, j]) < float(ma_m[t, j]):
                    ma_exit_flags.add(j)

        mkt_value = sum(p.shares * float(c_valued[t, p.j]) for p in positions)
        equity[n] = cash + mkt_value

        # ---------------------------------------------------------- 產生明日候選
        pending = []
        if regime[t]:
            mask = elig[t] & ~np.isnan(score_m[t]) & ~np.isnan(trig_m[t])
            # 已買滿的股票不再推薦；未買滿者保留在名單中可加碼
            full = [k for k, p in book.items() if p.units >= max_units]
            if full:
                mask[full] = False
            idxs = np.flatnonzero(mask)
            if len(idxs):
                order = idxs[np.argsort(-score_m[t, idxs])][:top_n]
                pending = [(int(j), float(trig_m[t, j])) for j in order]
                if record_candidates:
                    candidates_log[dates[t]] = [
                        {"stock_id": str(stocks[j]), "score": float(score_m[t, j]),
                         "trigger": float(trig_m[t, j]), "close": float(c[t, j])}
                        for j in order
                    ]

    eq = pd.Series(equity, index=dates[day_idx], name="equity")
    tdf = pd.DataFrame([t.__dict__ for t in trades])
    return Result(equity=eq, trades=tdf, daily_candidates=candidates_log)


def _close(pos: Position, date: pd.Timestamp, price: float, proceeds: float, reason: str) -> Trade:
    pnl = proceeds - pos.cost
    return Trade(
        stock_id=pos.stock_id, entry_date=pos.entry_date, exit_date=date,
        entry_price=pos.entry_price, exit_price=price, shares=pos.shares,
        pnl=pnl, pnl_pct=pnl / pos.cost if pos.cost else 0.0,
        hold_days=(date - pos.entry_date).days, reason=reason, units=pos.units,
    )
