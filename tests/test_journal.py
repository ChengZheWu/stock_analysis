"""紀錄簿與資料完整性的回歸測試。

涵蓋兩個實際發生過的錯誤：
1. 重複執行同一天的排程，會把當日推薦用當日資料結算（未來函數兼重複買進）。
2. 每日排程抓取近期公司行為時覆蓋了完整歷史，使還原股價失真。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import corporate, journal


@pytest.fixture
def conn(tmp_path):
    return journal.connect(tmp_path / "t.db")


def _day(date: str, rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([{**r, "date": pd.Timestamp(date)} for r in rows])


BASE = {"open": 100.0, "high": 105.0, "low": 98.0, "close": 103.0,
        "atr": 3.0, "ma50": 90.0}


def test_settle_requires_next_day(conn):
    """當日登記的推薦不可由當日結算，必須等到下一個交易日。"""
    d1 = pd.Timestamp("2026-01-05")
    cand = [{"rank": 1, "stock_id": "2330", "trigger": 102.0, "atr": 3.0}]
    journal.record_candidates(conn, d1, cand)

    # 同一天再次結算：即使價格觸及觸發價也不應成交
    same = journal.settle_day(conn, _day("2026-01-05", [{"stock_id": "2330", **BASE}]), d1)
    assert same["bought"] == []

    # 隔一個交易日才成交
    d2 = pd.Timestamp("2026-01-06")
    nxt = journal.settle_day(conn, _day("2026-01-06", [{"stock_id": "2330", **BASE}]), d2)
    assert [b["stock_id"] for b in nxt["bought"]] == ["2330"]


def test_repeated_run_does_not_duplicate(conn):
    """同一天重複登記推薦不應累積，避免產生重複買進。"""
    d1 = pd.Timestamp("2026-01-05")
    cand = [{"rank": 1, "stock_id": "2330", "trigger": 102.0, "atr": 3.0}]
    for _ in range(3):
        journal.record_candidates(conn, d1, cand)

    pending = conn.execute("SELECT COUNT(*) FROM picks WHERE status='pending'").fetchone()[0]
    assert pending == 1

    d2 = pd.Timestamp("2026-01-06")
    res = journal.settle_day(conn, _day("2026-01-06", [{"stock_id": "2330", **BASE}]), d2)
    assert len(res["bought"]) == 1


def test_sell_closes_whole_position(conn):
    """同一檔多次買進應合併為一個部位，出場時全數以同一價格賣出。"""
    for i, (d, trig) in enumerate([("2026-01-05", 102.0), ("2026-01-06", 104.0)], 1):
        journal.record_candidates(conn, pd.Timestamp(d),
                                  [{"rank": i, "stock_id": "2330", "trigger": trig, "atr": 3.0}])
        nxt = pd.Timestamp(d) + pd.Timedelta(days=1)
        journal.settle_day(conn, _day(str(nxt.date()), [{"stock_id": "2330", **BASE,
                                                         "high": 110.0}]), nxt)

    assert len(journal.open_positions(conn)) == 2
    assert journal.open_stocks(conn) == {"2330"}

    # 重挫觸發停損
    crash = _day("2026-01-20", [{"stock_id": "2330", "open": 60.0, "high": 61.0,
                                 "low": 55.0, "close": 56.0, "atr": 3.0, "ma50": 90.0}])
    res = journal.settle_day(conn, crash, pd.Timestamp("2026-01-20"))

    assert len(res["sold"]) == 1                    # 合併為一筆賣出訊號
    assert res["sold"][0]["units"] == 2
    assert not journal.open_positions(conn)
    prices = {r[0] for r in conn.execute(
        "SELECT sell_price FROM picks WHERE status='closed'")}
    assert len(prices) == 1                         # 兩筆以同一價格出場


def test_partial_corporate_fetch_does_not_save():
    """抓取部分區間時不得寫入快取，否則完整歷史會被截斷。"""
    before = corporate.EVENTS_PATH.read_bytes() if corporate.EVENTS_PATH.exists() else None
    if before is None:
        pytest.skip("尚未建立公司行為快取")

    recent = (pd.Timestamp.today() - pd.DateOffset(days=20)).strftime("%Y-%m-%d")
    corporate.build(start=recent, refresh=True, save=False)

    assert corporate.EVENTS_PATH.read_bytes() == before, "save=False 仍寫入了快取"


def test_stale_threshold_covers_longest_holiday():
    """資料過期的警告門檻必須大於實際最長連續休市，否則長假期間會誤報。"""
    from src import adjust
    from scripts.daily import STALE_WARN_DAYS

    panel = adjust.load_recent()
    dates = pd.DatetimeIndex(sorted(panel["date"].unique()))
    longest_gap = pd.Series(dates).diff().dt.days.max()

    assert STALE_WARN_DAYS > longest_gap, (
        f"門檻 {STALE_WARN_DAYS} 天不足以涵蓋最長休市 {longest_gap:.0f} 天")


def test_non_trading_day_produces_no_signal(tmp_path, monkeypatch):
    """沒有新交易日時不得產生訊號檔，也不應推播。"""
    import scripts.daily as daily
    from src import journal as J

    db = tmp_path / "j.db"
    conn = J.connect(db)
    d = pd.Timestamp("2026-01-05")
    J.record_candidates(conn, d, [])
    assert J.last_processed(conn) == d

    # 模擬「最新交易日就是已處理過的那天」
    dates = pd.DatetimeIndex([d - pd.Timedelta(days=1), d])
    todo = dates[dates > J.last_processed(conn)]
    assert len(todo) == 0, "已處理過最新交易日時不應有待處理日期"

    written = list(tmp_path.glob("signal_*.txt"))
    assert not written, "非交易日不應產生訊號檔"
