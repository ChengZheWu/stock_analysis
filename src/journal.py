"""推薦與買賣紀錄簿。

與回測分離：這裡不模擬現金與手續費，只記錄「何時建議買、何時建議賣」，
方便事後把推薦結果和實際市場走勢做對照。

同一檔股票可以有多筆各自獨立的紀錄，因此可以連續買進、也可以分批賣出。
時序與回測一致：第 D 日盤後產生觸發價，第 D+1 日觸及才算買進。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from config import ATR_INIT_STOP, ATR_TRAIL_STOP, RESULTS
from src import strategy

DB_PATH = RESULTS / "journal.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS picks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    rec_date    TEXT NOT NULL,      -- 推薦日（盤後）
    stock_id    TEXT NOT NULL,
    rank        INTEGER NOT NULL,   -- 當日推薦順位
    trigger     REAL NOT NULL,      -- 隔日進場觸發價
    entry_atr   REAL NOT NULL,
    status      TEXT NOT NULL,      -- pending / open / closed / expired
    buy_date    TEXT,
    buy_price   REAL,
    high_close  REAL,               -- 持有期間最高收盤，供移動停損使用
    sell_date   TEXT,
    sell_price  REAL,
    sell_reason TEXT,
    ma_flag     INTEGER DEFAULT 0,  -- 前一日已跌破 50MA，隔日開盤出場
    last_close  REAL,               -- 最後一次有資料的收盤價
    missing     INTEGER DEFAULT 0   -- 連續無資料天數，用於偵測下市
);
CREATE INDEX IF NOT EXISTS idx_status ON picks(status);
CREATE TABLE IF NOT EXISTS progress (last_date TEXT);
"""

# 連續無資料達此天數即認定停止交易，以最後收盤價認列出場
MISSING_LIMIT = 5


@dataclass
class Pick:
    id: int
    rec_date: str
    stock_id: str
    rank: int
    trigger: float
    entry_atr: float
    status: str
    buy_date: str | None
    buy_price: float | None
    high_close: float | None
    sell_date: str | None = None
    sell_price: float | None = None
    sell_reason: str | None = None
    ma_flag: int = 0
    last_close: float | None = None
    missing: int = 0

    @property
    def pnl_pct(self) -> float | None:
        if self.buy_price and self.sell_price:
            return self.sell_price / self.buy_price - 1.0
        return None


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # 既有資料庫補上後來新增的欄位
    have = {r["name"] for r in conn.execute("PRAGMA table_info(picks)")}
    for col, decl in (("last_close", "REAL"), ("missing", "INTEGER DEFAULT 0")):
        if col not in have:
            conn.execute(f"ALTER TABLE picks ADD COLUMN {col} {decl}")
    conn.commit()
    return conn


def last_processed(conn: sqlite3.Connection) -> pd.Timestamp | None:
    row = conn.execute("SELECT last_date FROM progress LIMIT 1").fetchone()
    return pd.Timestamp(row["last_date"]) if row and row["last_date"] else None


def _set_progress(conn: sqlite3.Connection, date: pd.Timestamp) -> None:
    conn.execute("DELETE FROM progress")
    conn.execute("INSERT INTO progress(last_date) VALUES (?)", (str(date.date()),))


def _rows(conn: sqlite3.Connection, status: str) -> list[Pick]:
    cur = conn.execute(
        "SELECT id,rec_date,stock_id,rank,trigger,entry_atr,status,buy_date,buy_price,"
        "high_close,sell_date,sell_price,sell_reason,ma_flag,last_close,missing "
        "FROM picks WHERE status=?", (status,))
    return [Pick(**dict(r)) for r in cur.fetchall()]


def open_positions(conn: sqlite3.Connection) -> list[Pick]:
    return _rows(conn, "open")


def open_stocks(conn: sqlite3.Connection) -> set[str]:
    """目前持有的「檔數」。同一檔多次加碼視為一檔，不另佔持股名額。"""
    return {p.stock_id for p in _rows(conn, "open")}


def settle_day(conn: sqlite3.Connection, day: pd.DataFrame, date: pd.Timestamp,
               max_new: int | None = None, prev_day: pd.DataFrame | None = None) -> dict:
    """結算當日：先處理昨日推薦的成交，再檢查出場。

    必須在產生新推薦「之前」呼叫，否則當日剛買進的股票會因為狀態尚未更新
    而被重複推薦。
    max_new 限制單日新進場筆數，與回測的 MAX_NEW_PER_DAY 一致；
    超過上限時依推薦順位取前幾名，其餘視為未成交。

    prev_day 提供前一交易日的資料，用來計算當日的停損價。停損必須以前一日
    收盤為止的資訊決定，當日 ATR 要收盤後才知道，拿來判斷當日是否觸價
    等同使用未來資訊。
    """
    idx = day.set_index("stock_id")
    prev_idx = prev_day.set_index("stock_id") if prev_day is not None else None

    def _stop_atr(sid: str, fallback: float) -> float:
        """取前一交易日的 ATR；沒有前一日資料時退回進場時的 ATR。"""
        if prev_idx is not None and sid in prev_idx.index:
            v = prev_idx.loc[sid]["atr"]
            if pd.notna(v):
                return float(v)
        return fallback
    bought, sold = [], []

    # ---------------------------------------------------------- 1. 結算昨日推薦
    filled = 0
    # 只結算推薦日早於今日者：當日盤後產生的觸發價，最快也要隔一個交易日才可能成交。
    # 若同一天重複執行本函式，這個條件可避免當日推薦被當日資料結算。
    today_str = str(date.date())
    pending = [p for p in _rows(conn, "pending") if p.rec_date < today_str]
    for p in sorted(pending, key=lambda x: x.rank):
        triggered = (p.stock_id in idx.index
                     and float(idx.loc[p.stock_id]["high"]) >= p.trigger)
        if triggered and (max_new is None or filled < max_new):
            row = idx.loc[p.stock_id]
            fill = max(float(row["open"]), p.trigger)
            conn.execute(
                "UPDATE picks SET status='open', buy_date=?, buy_price=?, high_close=?, "
                "last_close=? WHERE id=?",
                (str(date.date()), fill, float(row["close"]), float(row["close"]), p.id))
            bought.append({"stock_id": p.stock_id, "price": fill, "rank": p.rank})
            filled += 1
        else:
            # 未觸及觸發價或已達單日上限；隔日若仍符合條件會重新推薦
            conn.execute("UPDATE picks SET status='expired' WHERE id=?", (p.id,))

    # ---------------------------------------------------------- 2. 檢查出場
    # 同一檔的多次買進視為一個部位：以均價算停損，觸及時全部一起賣出。
    by_stock: dict[str, list[Pick]] = {}
    for p in open_positions(conn):
        by_stock.setdefault(p.stock_id, []).append(p)

    for sid, lots in by_stock.items():
        avg = sum(x.buy_price for x in lots) / len(lots)
        high = max((x.high_close or 0.0) for x in lots)
        atr_entry = max(lots, key=lambda x: x.buy_date).entry_atr
        first_buy = min(x.buy_date for x in lots)

        if sid not in idx.index:
            # 連續多日無資料多半是下市或長期停牌，以最後收盤價認列出場
            miss = max((x.missing or 0) for x in lots) + 1
            last_close = next((x.last_close for x in lots if x.last_close), None)
            if miss >= MISSING_LIMIT and last_close:
                _close_all(conn, lots, date, last_close, "下市/停止交易")
                sold.append(_sold_record(sid, lots, avg, first_buy, date,
                                         last_close, "下市/停止交易"))
            else:
                for x in lots:
                    conn.execute("UPDATE picks SET missing=? WHERE id=?", (miss, x.id))
            continue

        row = idx.loc[sid]
        stop = strategy.stop_level(avg, atr_entry, high, _stop_atr(sid, atr_entry))

        exit_price = exit_reason = None
        if float(row["low"]) <= stop:
            exit_price, exit_reason = min(float(row["open"]), stop), "移動停損"
        elif any(x.ma_flag for x in lots):
            exit_price, exit_reason = float(row["open"]), "跌破50日均線"

        if exit_price is not None:
            _close_all(conn, lots, date, exit_price, exit_reason)
            sold.append(_sold_record(sid, lots, avg, first_buy, date, exit_price, exit_reason))
            continue

        # 更新持有期間最高收盤，並標記是否跌破 50MA（隔日開盤出場）
        close = float(row["close"])
        ma50 = row["ma50"]
        flag = int(pd.notna(ma50) and close < float(ma50))
        new_high = max(high, close)
        for x in lots:
            conn.execute(
                "UPDATE picks SET high_close=?, ma_flag=?, last_close=?, missing=0 WHERE id=?",
                (new_high, flag, close, x.id))

    conn.commit()
    return {"bought": bought, "sold": sold}


def _close_all(conn: sqlite3.Connection, lots: list[Pick], date: pd.Timestamp,
               price: float, reason: str) -> None:
    """整檔出場：該股票所有未結束的買進紀錄一次全部賣出。"""
    for x in lots:
        conn.execute(
            "UPDATE picks SET status='closed', sell_date=?, sell_price=?, sell_reason=? WHERE id=?",
            (str(date.date()), price, reason, x.id))


def _sold_record(sid: str, lots: list[Pick], avg: float, first_buy: str,
                 date: pd.Timestamp, price: float, reason: str) -> dict:
    return {
        "stock_id": sid,
        "buy_date": first_buy,
        "buy_price": avg,
        "units": len(lots),
        "buys": sorted((x.buy_date, x.buy_price) for x in lots),
        "sell_price": price,
        "reason": reason,
        "pnl_pct": price / avg - 1.0,
        "days": (date - pd.Timestamp(first_buy)).days,
    }


def record_candidates(conn: sqlite3.Connection, date: pd.Timestamp,
                      candidates: list[dict], max_open: int | None = None) -> list[dict]:
    """登記明日的買進推薦。名額以「檔數」計，已持有的股票加碼不另佔名額。"""
    # 同一天重複執行時，先移除當日先前登記的推薦，避免重複累積
    conn.execute("DELETE FROM picks WHERE status='pending' AND rec_date=?",
                 (str(date.date()),))
    held = open_stocks(conn)
    room = len(candidates) if max_open is None else max(0, max_open - len(held))
    recorded = []
    for c in candidates:
        if c["stock_id"] not in held:
            if room <= 0:
                continue                   # 名額已滿，只能加碼既有持股
            room -= 1
        conn.execute(
            "INSERT INTO picks(rec_date,stock_id,rank,trigger,entry_atr,status) "
            "VALUES (?,?,?,?,?, 'pending')",
            (str(date.date()), c["stock_id"], c["rank"], c["trigger"], c["atr"]))
        recorded.append(c)

    _set_progress(conn, date)
    conn.commit()
    return recorded


def holdings_view(conn: sqlite3.Connection, day: pd.DataFrame) -> list[dict]:
    """目前持有部位（依股票合併）的現況與出場價位。

    這裡的停損價是「明日要掛的單」，因此用當日收盤後的 ATR 計算，與
    settle_day 不同——後者判斷的是當日是否已觸價，只能用前一日的 ATR。
    """
    idx = day.set_index("stock_id")
    today = pd.Timestamp(day["date"].iloc[0])

    by_stock: dict[str, list[Pick]] = {}
    for p in open_positions(conn):
        by_stock.setdefault(p.stock_id, []).append(p)

    out = []
    for sid, lots in by_stock.items():
        avg = sum(x.buy_price for x in lots) / len(lots)
        first_buy = min(x.buy_date for x in lots)
        buys = sorted((x.buy_date, x.buy_price) for x in lots)
        base = {"stock_id": sid, "buy_date": first_buy, "buy_price": avg,
                "units": len(lots), "buys": buys,
                "days": (today - pd.Timestamp(first_buy)).days}

        if sid not in idx.index:
            out.append({**base, "note": "今日無資料"})
            continue
        row = idx.loc[sid]
        close = float(row["close"])
        high = max((x.high_close or 0.0) for x in lots)
        atr_entry = max(lots, key=lambda x: x.buy_date).entry_atr
        out.append({
            **base,
            "close": close,
            "pnl_pct": close / avg - 1.0,
            "stop": strategy.stop_level(avg, atr_entry, high, float(row["atr"])),
            "ma_break": any(x.ma_flag for x in lots),
        })
    return sorted(out, key=lambda x: x["buy_date"])


def history(conn: sqlite3.Connection, limit: int = 50) -> pd.DataFrame:
    """已結束的交易紀錄，供事後對照。"""
    return pd.read_sql_query(
        "SELECT stock_id,buy_date,buy_price,sell_date,sell_price,sell_reason,"
        "(sell_price/buy_price-1.0) AS pnl_pct FROM picks "
        "WHERE status='closed' ORDER BY sell_date DESC LIMIT ?", conn, params=(limit,))
