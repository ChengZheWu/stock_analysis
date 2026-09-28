"""每日增量更新：用證交所與櫃買中心的全市場單日報表補上最新交易日。

一天只需兩次請求就能涵蓋上市與上櫃全部股票，不必動用 FinMind 額度，
因此適合每日排程執行。
"""

from __future__ import annotations

import time

import pandas as pd
import requests

from config import CACHE

HEADERS = {"User-Agent": "Mozilla/5.0"}
TIMEOUT = 40
INCREMENTAL_PATH = CACHE / "incremental.parquet"
# 增量資料只是暫存，併入面板後即可捨棄；保留此天數以涵蓋排程中斷數日的情況
KEEP_DAYS = 30

TWSE_URL = ("https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
            "?date={d}&type=ALLBUT0999&response=json")
TPEX_URL = ("https://www.tpex.org.tw/www/zh-tw/afterTrading/otc"
            "?date={d}&type=EW&response=json")


def _num(text) -> float | None:
    s = str(text).replace(",", "").replace("+", "").strip()
    if s in ("", "--", "---", "N/A", "null"):
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return v


def _twse_day(date: pd.Timestamp) -> list[dict]:
    body = requests.get(TWSE_URL.format(d=date.strftime("%Y%m%d")),
                        headers=HEADERS, timeout=TIMEOUT).json()
    if body.get("stat") != "OK":
        return []
    rows = []
    for tb in body.get("tables", []):
        fields = tb.get("fields", [])
        if "證券代號" not in fields or "收盤價" not in fields:
            continue
        for r in tb.get("data", []):
            d = dict(zip(fields, r))
            sid = str(d.get("證券代號", "")).strip()
            if not (sid.isdigit() and len(sid) == 4):
                continue
            o, h, l, c = (_num(d.get(k)) for k in ("開盤價", "最高價", "最低價", "收盤價"))
            if None in (o, h, l, c):
                continue
            rows.append({"stock_id": sid, "date": date, "open": o, "high": h,
                         "low": l, "close": c, "volume": _num(d.get("成交股數")) or 0})
    return rows


def _tpex_day(date: pd.Timestamp) -> list[dict]:
    body = requests.get(TPEX_URL.format(d=date.strftime("%Y/%m/%d")),
                        headers=HEADERS, timeout=TIMEOUT).json()
    if str(body.get("stat", "")).lower() != "ok":
        return []
    rows = []
    for tb in body.get("tables", []):
        fields = [f.strip() for f in tb.get("fields", [])]
        if "代號" not in fields or "收盤" not in fields:
            continue
        for r in tb.get("data", []):
            d = dict(zip(fields, r))
            sid = str(d.get("代號", "")).strip()
            if not (sid.isdigit() and len(sid) == 4):
                continue
            o, h, l, c = (_num(d.get(k)) for k in ("開盤", "最高", "最低", "收盤"))
            if None in (o, h, l, c):
                continue
            rows.append({"stock_id": sid, "date": date, "open": o, "high": h,
                         "low": l, "close": c, "volume": _num(d.get("成交股數")) or 0})
    return rows


def fetch_day(date: pd.Timestamp) -> pd.DataFrame:
    """抓取單一交易日的全市場報價（未還原）。非交易日回傳空表。"""
    rows = _twse_day(date) + _tpex_day(date)
    return pd.DataFrame(rows)


def update(days_back: int = 10) -> pd.DataFrame:
    """補齊最近 N 個日曆日中尚未收錄的交易日。"""
    store = pd.read_parquet(INCREMENTAL_PATH) if INCREMENTAL_PATH.exists() else pd.DataFrame()
    have = set(pd.to_datetime(store["date"]).dt.normalize()) if len(store) else set()

    today = pd.Timestamp.today().normalize()
    new_frames = []
    for i in range(days_back):
        day = today - pd.Timedelta(days=i)
        if day.weekday() >= 5 or day in have:
            continue
        df = fetch_day(day)
        if len(df):
            print(f"  {day.date()}: {len(df)} 檔", flush=True)
            new_frames.append(df)
        time.sleep(2)

    if not new_frames:
        return store

    merged = pd.concat([store] + new_frames, ignore_index=True) if len(store) else pd.concat(new_frames, ignore_index=True)
    merged["date"] = pd.to_datetime(merged["date"])
    merged = merged.drop_duplicates(subset=["stock_id", "date"], keep="last")
    # 只保留近期：這些資料一經併入面板就不再需要，若持續累積，
    # 一年會膨脹到數百萬筆（實測每個交易日約 2,000 檔）。
    cutoff = merged["date"].max() - pd.Timedelta(days=KEEP_DAYS)
    merged = merged[merged["date"] >= cutoff]
    merged = merged.sort_values(["stock_id", "date"]).reset_index(drop=True)
    merged.to_parquet(INCREMENTAL_PATH, index=False)
    return merged


if __name__ == "__main__":
    out = update()
    if len(out):
        print(f"增量資料共 {len(out):,} 筆，最新日期 {pd.to_datetime(out['date']).max().date()}")
