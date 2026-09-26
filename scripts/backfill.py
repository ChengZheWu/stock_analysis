"""全量抓取歷史資料。可中斷、可續跑：已抓過的股票會跳過，不耗用 API 額度。

用法:
    python scripts/backfill.py price       # 逐檔日 K（最耗時）
    python scripts/backfill.py corporate   # 除權息、減資
    python scripts/backfill.py market      # 大盤指數、分割、面額變更
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import BACKFILL_START, CACHE, RAW, TAIEX_ID
from src import finmind, universe

TODAY = pd.Timestamp.today().strftime("%Y-%m-%d")


def _progress(done: int, total: int, started: float, label: str) -> None:
    elapsed = time.time() - started
    rate = done / elapsed if elapsed else 0
    eta = (total - done) / rate if rate else 0
    print(
        f"[{label}] {done}/{total} ({done/total*100:.1f}%) "
        f"已用 {elapsed/60:.1f} 分，預估剩餘 {eta/60:.1f} 分",
        flush=True,
    )


def fetch_market() -> None:
    """大盤指數與全市場性質的公司行為資料（不需逐檔查詢）。"""
    idx = finmind.cached(
        CACHE / "taiex.parquet", "TaiwanStockTotalReturnIndex",
        data_id=TAIEX_ID, start_date=BACKFILL_START, end_date=TODAY,
    )
    print(f"加權指數 {len(idx)} 筆 {idx['date'].min()} ~ {idx['date'].max()}")

    for name, ds in [("split", "TaiwanStockSplitPrice"), ("par_value", "TaiwanStockParValueChange")]:
        df = finmind.cached(CACHE / f"{name}.parquet", ds, start_date=BACKFILL_START, end_date=TODAY)
        print(f"{name}: {len(df)} 筆")


def fetch_per_stock(dataset: str, subdir: str, label: str) -> None:
    uni = universe.build()
    ids = uni["stock_id"].tolist()
    out = RAW / subdir
    out.mkdir(parents=True, exist_ok=True)

    todo = [s for s in ids if not (out / f"{s}.parquet").exists()]
    print(f"[{label}] 股票池 {len(ids)} 檔，已完成 {len(ids)-len(todo)} 檔，待抓 {len(todo)} 檔", flush=True)

    started = time.time()
    failed: list[tuple[str, str]] = []
    for i, sid in enumerate(todo, 1):
        try:
            df = finmind.request(dataset, data_id=sid, start_date=BACKFILL_START, end_date=TODAY)
        except finmind.FinMindError as exc:
            # 不建檔，讓下次執行能重新嘗試；建空檔會讓失敗永久固化
            failed.append((sid, str(exc)[:80]))
            continue
        df.to_parquet(out / f"{sid}.parquet", index=False)
        if i % 25 == 0 or i == len(todo):
            _progress(i, len(todo), started, label)

    if failed:
        pd.DataFrame(failed, columns=["stock_id", "error"]).to_csv(
            CACHE / f"failed_{subdir}.csv", index=False
        )
        print(f"[{label}] {len(failed)} 檔失敗，明細見 cache/failed_{subdir}.csv", flush=True)
    print(f"[{label}] 完成，總耗時 {(time.time()-started)/60:.1f} 分", flush=True)


def clear_empty(subdir: str) -> int:
    """刪除空的快取檔，讓下次執行重新抓取。

    早期版本在抓取失敗時會留下空檔案，續跑時會被誤認為已完成。
    """
    removed = 0
    for path in (RAW / subdir).glob("*.parquet"):
        try:
            if pd.read_parquet(path).empty:
                path.unlink()
                removed += 1
        except Exception:
            path.unlink()
            removed += 1
    print(f"[{subdir}] 清除 {removed} 個空檔案")
    return removed


def main() -> None:
    what = sys.argv[1] if len(sys.argv) > 1 else "price"
    if what == "clean":
        for sub in ("price", "dividend", "reduction"):
            if (RAW / sub).exists():
                clear_empty(sub)
    elif what == "market":
        fetch_market()
    elif what == "price":
        fetch_per_stock("TaiwanStockPrice", "price", "日K")
    elif what == "corporate":
        fetch_per_stock("TaiwanStockDividendResult", "dividend", "除權息")
        fetch_per_stock("TaiwanStockCapitalReductionReferencePrice", "reduction", "減資")
    else:
        raise SystemExit(f"未知的任務: {what}")


if __name__ == "__main__":
    main()
