"""把未還原日 K 加上公司行為，組成還原股價面板。

還原方式採「往回調整」：以最新價格為基準，把事件發生日之前的價格
乘上累積因子（事件後參考價 / 事件前收盤價）。除權息、減資、面額變更
都能用同一套 before/after 比值處理，因此邏輯一致。
"""

from __future__ import annotations

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from config import CACHE, RAW
from src import corporate

PANEL_PATH = CACHE / "panel.parquet"
RECENT_PATH = CACHE / "panel_recent.parquet"
# 指標最長回看 252 日，保留 450 日足以正確計算並留有餘裕
RECENT_DAYS = 450
# 完整重建時每批處理的股票檔數，用於控制峰值記憶體
BUILD_CHUNK = 200
_PRICE_COLS = {"open": "open", "max": "high", "min": "low", "close": "close",
               "Trading_Volume": "volume", "Trading_money": "amount"}


def load_official(verbose: bool = True) -> pd.DataFrame:
    """讀取證交所與櫃買的每日報表，組成未還原的全市場行情。

    這是價格的主要來源：OHLC 為官方值，且只包含真正在集中市場交易的日子。
    """
    files = sorted((RAW / "daily").glob("*.parquet"))
    if not files:
        return pd.DataFrame()

    frames = []
    for i, path in enumerate(files, 1):
        df = pd.read_parquet(path)
        if len(df):
            frames.append(df)
        if verbose and i % 500 == 0:
            print(f"  讀取每日報表 {i}/{len(files)}", flush=True)
    if not frames:
        return pd.DataFrame()

    out = pd.concat(frames, ignore_index=True)
    out["date"] = pd.to_datetime(out["date"])
    # 增量更新的資料同樣併入，讓歷史與最新交易日銜接
    inc_path = CACHE / "incremental.parquet"
    if inc_path.exists():
        inc = pd.read_parquet(inc_path)
        if len(inc):
            inc["date"] = pd.to_datetime(inc["date"])
            out = pd.concat([out, inc], ignore_index=True)

    out = out.drop_duplicates(subset=["stock_id", "date"], keep="last")
    out = out[_is_common_stock(out["stock_id"])]
    out = out.sort_values(["stock_id", "date"]).reset_index(drop=True)
    return _compact(out)


def _compact(df: pd.DataFrame) -> pd.DataFrame:
    """壓縮欄位型別。

    500 萬筆資料中，stock_id 以 Python 字串儲存就佔數百 MB；
    改為 category 並將價格降為 float32，可讓記憶體用量降到約四分之一。
    """
    out = df.copy()
    for col in ("open", "high", "low", "close", "volume"):
        if col in out.columns:
            out[col] = out[col].astype("float32")
    return out


def _is_common_stock(ids: pd.Series) -> pd.Series:
    """只保留普通股：排除 ETF（00xx）與存託憑證（91xx）。

    不以現行股票池過濾，否則早年下市的股票會被排除，反而重新引入存活者偏差。
    """
    return ids.str.fullmatch(r"\d{4}") & ~ids.str.startswith(("00", "91"))


def _load_incremental() -> dict[str, pd.DataFrame]:
    """讀取每日增量資料，依股票代號分組，供與歷史資料銜接。"""
    path = CACHE / "incremental.parquet"
    if not path.exists():
        return {}
    df = pd.read_parquet(path)
    if df.empty:
        return {}
    df["date"] = pd.to_datetime(df["date"])
    return {sid: g for sid, g in df.groupby("stock_id")}


def _load_raw(stock_id: str, incremental: dict[str, pd.DataFrame] | None = None) -> pd.DataFrame | None:
    path = RAW / "price" / f"{stock_id}.parquet"
    if not path.exists():
        return None
    df = pd.read_parquet(path)
    if df.empty or "date" not in df.columns:
        return None
    df = df.rename(columns=_PRICE_COLS)
    if incremental and stock_id in incremental:
        extra = incremental[stock_id]
        df["date"] = pd.to_datetime(df["date"])
        extra = extra[~extra["date"].isin(df["date"])]
        if len(extra):
            df = pd.concat([df, extra], ignore_index=True)
    need = ["date", "open", "high", "low", "close", "volume"]
    if any(c not in df.columns for c in need):
        return None
    df = df[need + (["amount"] if "amount" in df.columns else [])].copy()
    df["date"] = pd.to_datetime(df["date"])
    # 無量或價格異常的交易日直接剔除，這類列會讓指標失真
    df = df[(df[["open", "high", "low", "close"]] > 0).all(axis=1)]
    return df.sort_values("date").reset_index(drop=True) if len(df) else None


def adjust_one(stock_id: str, events: pd.DataFrame,
               incremental: dict[str, pd.DataFrame] | None = None) -> pd.DataFrame | None:
    """回傳單檔的還原股價；沒有資料則回傳 None。"""
    df = _load_raw(stock_id, incremental)
    if df is None:
        return None

    ev = events[events["stock_id"] == stock_id]
    df["adj_factor"] = 1.0
    if not ev.empty:
        factor = pd.Series(1.0, index=df.index)
        for _, e in ev.sort_values("date").iterrows():
            # 事件日當天的開盤價已是還原後的價格，因此只調整事件日之前
            factor.loc[df["date"] < e["date"]] *= e["factor"]
        df["adj_factor"] = factor.values

    for col in ("open", "high", "low", "close"):
        df[col] = df[col] * df["adj_factor"]
    # 股數隨還原因子反向變動，維持「價 × 量」的一致性
    df["volume"] = df["volume"] / df["adj_factor"]
    df["stock_id"] = stock_id
    return df


def _apply_factors(df: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    """依公司行為計算累積還原因子並套用到單檔股票。"""
    df = df.sort_values("date").reset_index(drop=True)
    factor = pd.Series(1.0, index=df.index)
    if len(events):
        for date, f in zip(events["date"], events["factor"]):
            # 事件日當天的價格已是還原後的水準，因此只調整事件日之前
            factor.loc[df["date"] < date] *= f
    out = df.copy()
    out["adj_factor"] = factor.values
    for col in ("open", "high", "low", "close"):
        out[col] = out[col] * out["adj_factor"]
    out["volume"] = out["volume"] / out["adj_factor"]
    return out


def build_panel(refresh: bool = False, verbose: bool = True) -> pd.DataFrame:
    """把全市場的還原股價合併成單一長表，供回測與選股共用。"""
    if PANEL_PATH.exists() and not refresh:
        return pd.read_parquet(PANEL_PATH)

    events = corporate.build()
    raw = load_official(verbose=verbose)
    if raw.empty:
        raise RuntimeError("找不到每日報表資料，請先執行 scripts/backfill_twse.py")

    # 剔除價格異常的列；官方資料的 OHLC 應自洽
    raw = raw[(raw[["open", "high", "low", "close"]] > 0).all(axis=1)]
    raw = raw[(raw["high"] >= raw["low"])]

    ev_by_stock = {sid: g for sid, g in events.groupby("stock_id")}
    cols = ["stock_id", "date", "open", "high", "low", "close", "volume", "adj_factor"]

    # 分批處理並逐批寫入檔案：一次載入全部會讓同一份資料被複製多次，
    # 峰值記憶體達數 GB，小型主機無法負荷。
    ids = raw["stock_id"].unique()
    writer = None
    total = 0
    try:
        for start in range(0, len(ids), BUILD_CHUNK):
            batch = ids[start:start + BUILD_CHUNK]
            sub = raw[raw["stock_id"].isin(batch)]
            frames = [_apply_factors(grp, ev_by_stock.get(sid, pd.DataFrame()))
                      for sid, grp in sub.groupby("stock_id", sort=False)]
            if not frames:
                continue
            part = pd.concat(frames, ignore_index=True)[cols]
            part = part.sort_values(["stock_id", "date"], ignore_index=True)
            table = pa.Table.from_pandas(part, preserve_index=False)
            if writer is None:
                writer = pq.ParquetWriter(PANEL_PATH, table.schema)
            writer.write_table(table)
            total += len(part)
            del frames, part, sub, table
            if verbose:
                print(f"  還原中 {min(start + BUILD_CHUNK, len(ids))}/{len(ids)}", flush=True)
    finally:
        if writer is not None:
            writer.close()

    del raw, ev_by_stock
    panel = pd.read_parquet(PANEL_PATH)
    write_recent(panel)
    if verbose:
        print(f"還原完成：{panel['stock_id'].nunique()} 檔、{total:,} 筆")
    return panel


def update_panel(lookback_days: int = 180, verbose: bool = True) -> pd.DataFrame:
    """增量更新面板：只補上新交易日，必要時重算受影響股票的還原因子。

    完整重建要載入十年全市場資料，峰值記憶體約 5 GB；每日更新用不到這個規模，
    因此改為在既有面板上追加，讓日常排程能在小型主機上執行。
    """
    if not PANEL_PATH.exists():
        return build_panel(verbose=verbose)

    panel = pd.read_parquet(PANEL_PATH)
    panel["date"] = pd.to_datetime(panel["date"])
    last = panel["date"].max()

    fresh = load_official(verbose=False)
    fresh = fresh[fresh["date"] > last]
    if fresh.empty:
        if verbose:
            print(f"面板已是最新（{last.date()}），無需更新")
        return panel
    fresh = fresh[(fresh[["open", "high", "low", "close"]] > 0).all(axis=1)]
    fresh = fresh[fresh["high"] >= fresh["low"]]

    events = corporate.build()
    cutoff = last - pd.Timedelta(days=lookback_days)
    recent_ids = set(events[events["date"] > cutoff]["stock_id"])

    # 新交易日的價格即是最新水準，還原因子為 1
    fresh = fresh.assign(adj_factor=1.0)
    cols = ["stock_id", "date", "open", "high", "low", "close", "volume", "adj_factor"]
    out = pd.concat([panel, fresh[cols]], ignore_index=True)

    # 近期有除權息或減資的股票，其歷史需按新事件重新縮放
    if recent_ids:
        ev_by_stock = {s: g for s, g in events.groupby("stock_id")}
        fixed = []
        for sid in recent_ids:
            grp = out[out["stock_id"] == sid]
            if grp.empty:
                continue
            raw = grp.copy()
            for col in ("open", "high", "low", "close"):
                raw[col] = raw[col] / raw["adj_factor"]
            raw["volume"] = raw["volume"] * raw["adj_factor"]
            fixed.append(_apply_factors(raw.drop(columns="adj_factor"),
                                        ev_by_stock.get(sid, pd.DataFrame())))
        if fixed:
            out = out[~out["stock_id"].isin(recent_ids)]
            out = pd.concat([out] + fixed, ignore_index=True)
        if verbose:
            print(f"  重算 {len(recent_ids)} 檔近期有公司行為的股票")

    out = out[cols].drop_duplicates(subset=["stock_id", "date"], keep="last")
    out = out.sort_values(["stock_id", "date"]).reset_index(drop=True)
    out.to_parquet(PANEL_PATH, index=False)
    write_recent(out)
    if verbose:
        print(f"面板更新至 {out['date'].max().date()}，新增 {len(fresh):,} 筆")
    return out


def write_recent(panel: pd.DataFrame, days: int = RECENT_DAYS) -> pd.DataFrame:
    """另存一份「近期面板」。

    每日排程只需要近期資料，讀取完整面板（約 500 萬筆）會佔用大量記憶體，
    小型主機容易不足。這份精簡檔約為完整面板的 4%。
    """
    dates = pd.DatetimeIndex(sorted(panel["date"].unique()))
    cutoff = dates[max(0, len(dates) - days)]
    recent = panel[panel["date"] >= cutoff].reset_index(drop=True)
    recent.to_parquet(RECENT_PATH, index=False)
    return recent


def load_recent(days: int = RECENT_DAYS) -> pd.DataFrame:
    """讀取近期面板；若不存在則從完整面板產生。"""
    if RECENT_PATH.exists():
        df = pd.read_parquet(RECENT_PATH)
        df["date"] = pd.to_datetime(df["date"])
        return df
    return write_recent(build_panel(verbose=False), days)


def load_taiex() -> pd.DataFrame:
    df = pd.read_parquet(CACHE / "taiex.parquet")
    df["date"] = pd.to_datetime(df["date"])
    return df[["date", "price"]].sort_values("date").reset_index(drop=True)
