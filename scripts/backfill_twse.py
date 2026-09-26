"""以證交所與櫃買中心的每日全市場報表重建歷史行情。

相較於逐檔 API，這個來源有三個優勢：
1. OHLC 為官方值，不會出現開盤價異常（FinMind 對興櫃期間會以前一日均價填充）。
2. 自動排除興櫃期間，只涵蓋真正在集中市場交易的日子。
3. 逐日快照天然包含「當時在市、後來下市」的股票，大幅降低存活者偏差。

可中斷續跑：已抓過的日期會略過。

用法:
    python scripts/backfill_twse.py                    # 從設定的起始日抓到今天
    python scripts/backfill_twse.py 2020-01-01         # 指定起始日
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import BACKFILL_START, RAW
from src import daily_update

OUT = RAW / "daily"
DELAY = 1.0          # 對公開站台保持禮貌的間隔
MAX_RETRY = 3


def fetch_with_retry(date: pd.Timestamp) -> pd.DataFrame:
    for attempt in range(MAX_RETRY):
        try:
            return daily_update.fetch_day(date)
        except Exception as exc:
            if attempt == MAX_RETRY - 1:
                print(f"  {date.date()} 失敗：{str(exc)[:60]}", flush=True)
                return pd.DataFrame()
            time.sleep(5 * (attempt + 1))
    return pd.DataFrame()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    start = sys.argv[1] if len(sys.argv) > 1 else BACKFILL_START
    days = pd.bdate_range(start, pd.Timestamp.today().normalize())
    todo = [d for d in days if not (OUT / f"{d:%Y%m%d}.parquet").exists()]

    print(f"共 {len(days)} 個營業日，已完成 {len(days)-len(todo)}，待抓 {len(todo)}", flush=True)
    started, empty = time.time(), 0

    for i, day in enumerate(todo, 1):
        df = fetch_with_retry(day)
        # 空表代表非交易日（例假日、颱風假），同樣建檔以免重複嘗試
        df.to_parquet(OUT / f"{day:%Y%m%d}.parquet", index=False)
        if df.empty:
            empty += 1

        if i % 50 == 0 or i == len(todo):
            elapsed = time.time() - started
            eta = (len(todo) - i) / (i / elapsed) if elapsed else 0
            print(f"[{i}/{len(todo)}] {day.date()} 已用 {elapsed/60:.1f} 分，"
                  f"預估剩餘 {eta/60:.1f} 分（非交易日 {empty}）", flush=True)
        time.sleep(DELAY)

    print(f"完成，總耗時 {(time.time()-started)/60:.1f} 分", flush=True)


if __name__ == "__main__":
    main()
