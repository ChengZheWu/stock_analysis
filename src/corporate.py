"""公司行為（除權息、減資、面額變更）——改用證交所與櫃買中心的免費批次資料。

這些端點一次回傳全市場的「事件前收盤價」與「事件後參考價」，
兩者的比值就是還原因子。相較於逐檔查詢 FinMind，可省下數千次 API 額度，
而且涵蓋當時在市但後來下市的股票，有助於降低存活者偏差。
"""

from __future__ import annotations

import time

import pandas as pd
import requests

from config import BACKFILL_START, CACHE

HEADERS = {"User-Agent": "Mozilla/5.0"}
TIMEOUT = 40
POLITE_DELAY = 3.0          # 對公開站台保持禮貌的間隔
EVENTS_PATH = CACHE / "corporate_actions.parquet"

TWSE = "https://www.twse.com.tw/rwd/zh/{path}?startDate={s}&endDate={e}&response=json"
TPEX = "https://www.tpex.org.tw/www/zh-tw/bulletin/{path}?startDate={s}&endDate={e}&response=json"

# (來源, 路徑, 事件類型, 前價欄位, 後價欄位)
SOURCES = [
    ("twse", "exRight/TWT49U", "dividend", "除權息前收盤價", "除權息參考價"),
    ("twse", "reducation/TWTAUU", "reduction", "停止買賣前收盤價格", "恢復買賣參考價"),
    ("twse", "change/TWTB8U", "par_value", "停止買賣前收盤價格", "恢復買賣參考價"),
    ("tpex", "exDailyQ", "dividend", "除權息前收盤價", "除權息參考價"),
    ("tpex", "revivt", "reduction", "最後交易日之收盤價格", "減資恢復買賣開始日參考價格"),
]

# 面額變更與股票分割改用 FinMind 的批次資料集（櫃買中心無對應的公開端點）
FINMIND_EVENTS = [
    ("split.parquet", "before_price", "after_price", "split"),
    ("par_value.parquet", "before_close", "after_ref_close", "par_value"),
]


def _finmind_events() -> list[dict]:
    """讀取先前快取的分割與面額變更資料，轉成統一格式。"""
    rows: list[dict] = []
    for fname, col_before, col_after, kind in FINMIND_EVENTS:
        path = CACHE / fname
        if not path.exists():
            continue
        df = pd.read_parquet(path)
        for _, r in df.iterrows():
            before, after = _num(r.get(col_before)), _num(r.get(col_after))
            sid = str(r.get("stock_id", "")).strip()
            if before is None or after is None or not sid.isdigit():
                continue
            rows.append({"stock_id": sid, "date": pd.Timestamp(r["date"]), "before": before,
                         "after": after, "kind": kind, "source": "finmind"})
    print(f"  finmind (split/par_value): {len(rows)} 筆", flush=True)
    return rows


def _roc_date(text: str) -> pd.Timestamp | None:
    """解析民國日期：'113年01月04日'、'111/01/06'、'1130104' 皆可。"""
    s = str(text).strip()
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) not in (6, 7):
        return None
    year = int(digits[:-4]) + 1911
    month, day = int(digits[-4:-2]), int(digits[-2:])
    try:
        return pd.Timestamp(year=year, month=month, day=day)
    except ValueError:
        return None


def _num(text: str) -> float | None:
    s = str(text).replace(",", "").replace("$", "").strip()
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v > 0 else None


def _fetch(source: str, path: str, start: str, end: str) -> list[dict]:
    """抓取單一區間，回傳 fields 對應後的 dict 列表。"""
    url = (TWSE if source == "twse" else TPEX).format(path=path, s=start, e=end)
    for attempt in range(3):
        try:
            body = requests.get(url, headers=HEADERS, timeout=TIMEOUT).json()
        except Exception:
            time.sleep(5 * (attempt + 1))
            continue

        tables = []
        if source == "twse":
            if body.get("stat") != "OK":
                return []
            tables = [(body.get("fields", []), body.get("data", []))]
        else:
            for tb in body.get("tables", []):
                tables.append((tb.get("fields", []), tb.get("data", [])))

        rows = []
        for fields, data in tables:
            if not fields or not data:
                continue
            for r in data:
                if len(r) >= len(fields):
                    rows.append(dict(zip(fields, r)))
                elif r:
                    rows.append(dict(zip(fields, r + [""] * (len(fields) - len(r)))))
        return rows
    return []


def _periods(start: str, end: str):
    """切成季度區間，避免單次查詢資料量過大被截斷。"""
    cur = pd.Timestamp(start).normalize().replace(day=1)
    last = pd.Timestamp(end)
    while cur <= last:
        stop = min(cur + pd.DateOffset(months=3) - pd.Timedelta(days=1), last)
        yield cur.strftime("%Y%m%d"), stop.strftime("%Y%m%d")
        cur = cur + pd.DateOffset(months=3)


def build(start: str = BACKFILL_START, end: str | None = None,
          refresh: bool = False, save: bool = True) -> pd.DataFrame:
    """回傳欄位 stock_id / date / before / after / factor / kind 的公司行為表。

    save=False 時不寫入快取。抓取部分區間（例如每日排程只取近三個月）時
    必須如此，否則會把完整歷史覆蓋成片段，導致所有還原股價失真。
    """
    if EVENTS_PATH.exists() and not refresh:
        return pd.read_parquet(EVENTS_PATH)

    end = end or pd.Timestamp.today().strftime("%Y-%m-%d")
    records: list[dict] = []

    for source, path, kind, col_before, col_after in SOURCES:
        count = 0
        for s, e in _periods(start, end):
            # TPEX 端點使用斜線格式的日期
            if source == "tpex":
                s_fmt = f"{s[:4]}/{s[4:6]}/{s[6:]}"
                e_fmt = f"{e[:4]}/{e[4:6]}/{e[6:]}"
            else:
                s_fmt, e_fmt = s, e
            for row in _fetch(source, path, s_fmt, e_fmt):
                sid = str(row.get("股票代號") or row.get("代號") or "").strip()
                if not sid.isdigit() or len(sid) != 4:
                    continue
                date_raw = (row.get("資料日期") or row.get("除權息日期")
                            or row.get("恢復買賣日期") or row.get("日期"))
                date = _roc_date(date_raw)
                before, after = _num(row.get(col_before)), _num(row.get(col_after))
                if date is None or before is None or after is None:
                    continue
                records.append({"stock_id": sid, "date": date, "before": before,
                                "after": after, "kind": kind, "source": source})
                count += 1
            time.sleep(POLITE_DELAY)
        print(f"  {source}/{path} ({kind}): {count} 筆", flush=True)

    records.extend(_finmind_events())
    df = pd.DataFrame(records)
    if df.empty:
        raise RuntimeError("未取得任何公司行為資料，請檢查網路或端點是否異動")

    df = df.drop_duplicates(subset=["stock_id", "date", "kind"]).sort_values(["stock_id", "date"])
    df["factor"] = df["after"] / df["before"]
    # 過濾明顯異常的比值，避免來源資料錯誤污染還原價
    df = df[(df["factor"] > 0.05) & (df["factor"] < 3.0)].reset_index(drop=True)
    if save:
        df.to_parquet(EVENTS_PATH, index=False)
    return df


if __name__ == "__main__":
    ev = build(refresh=True)
    print(f"\n公司行為事件共 {len(ev)} 筆，涵蓋 {ev['stock_id'].nunique()} 檔")
    print(ev.groupby("kind").size().to_string())
    print(f"日期範圍 {ev['date'].min().date()} ~ {ev['date'].max().date()}")
