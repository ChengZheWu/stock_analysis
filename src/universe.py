"""建立股票池：上市、上櫃普通股，並納入期間內曾下市的股票以降低存活者偏差。"""

from __future__ import annotations

import pandas as pd

from config import CACHE, EXCLUDE_INDUSTRIES, HISTORY_START, UNIVERSE_TYPES
from src import finmind

UNIVERSE_PATH = CACHE / "universe.parquet"
_FOUR_DIGIT = r"\d{4}"


def build(refresh: bool = False) -> pd.DataFrame:
    """回傳欄位為 stock_id / stock_name / industry / type / delisted_date 的股票池。"""
    if UNIVERSE_PATH.exists() and not refresh:
        return pd.read_parquet(UNIVERSE_PATH)

    info = finmind.cached(CACHE / "stock_info.parquet", "TaiwanStockInfo", refresh=refresh)
    live = info[info["type"].isin(UNIVERSE_TYPES)].copy()
    live = live[live["stock_id"].str.fullmatch(_FOUR_DIGIT)]
    live = live[~live["industry_category"].isin(EXCLUDE_INDUSTRIES)]
    live = live.drop_duplicates("stock_id")[["stock_id", "stock_name", "industry_category", "type"]]
    live = live.rename(columns={"industry_category": "industry"})

    delist = finmind.cached(CACHE / "delisting.parquet", "TaiwanStockDelisting", refresh=refresh)
    delist = delist[delist["stock_id"].str.fullmatch(_FOUR_DIGIT)].drop_duplicates("stock_id")
    # 只保留回測期間內才下市的，更早下市的股票沒有可用的歷史
    delist = delist[delist["date"] >= HISTORY_START]

    extra = delist[~delist["stock_id"].isin(live["stock_id"])].copy()
    extra = extra.rename(columns={"date": "delisted_date"})
    extra["industry"] = "已下市"
    extra["type"] = "delisted"

    uni = pd.concat(
        [live.assign(delisted_date=pd.NA), extra[["stock_id", "stock_name", "industry", "type", "delisted_date"]]],
        ignore_index=True,
    )
    # 標記仍在清單中但已下市的股票
    dmap = delist.set_index("stock_id")["date"]
    uni["delisted_date"] = uni["delisted_date"].fillna(uni["stock_id"].map(dmap))
    uni = uni.sort_values("stock_id").reset_index(drop=True)

    uni.to_parquet(UNIVERSE_PATH, index=False)
    return uni


if __name__ == "__main__":
    u = build(refresh=True)
    print(f"股票池共 {len(u)} 檔")
    print(u["type"].value_counts().to_string())
    print(f"其中標記已下市：{u['delisted_date'].notna().sum()} 檔")
