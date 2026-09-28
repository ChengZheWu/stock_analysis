"""每日盤後排程：更新資料 → 產生訊號 → 記錄 → 推播。

紀錄存放於 data/results/journal.db，可重複查詢與事後對照。

用法:
    python scripts/daily.py                    # 更新資料並輸出今日訊號
    python scripts/daily.py --push             # 同上，並推播到 Telegram
    python scripts/daily.py --no-update        # 只用現有資料
    python scripts/daily.py --catchup 30       # 補跑最近 30 個交易日
    python scripts/daily.py --history          # 顯示已結束的交易紀錄

非交易日（假日、颱風假）執行時不會產生訊號，也不會推播。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import CACHE, RESULTS
from src import adjust, corporate, daily_update, journal, notify

# 連續這麼多天沒有新資料就發出警告。
# 實測近兩年最長連續休市為 12 個日曆日（2026 年農曆年 2/13～2/20 加上前後週末），
# 因此設為 15 天以留出餘裕，避免長假期間誤報。
STALE_WARN_DAYS = 15


def refresh_data() -> None:
    """補齊最新交易日，並重新整理近期的公司行為。

    面板採增量更新而非重建：重建需要約 5 GB 記憶體，增量只需數百 MB，
    讓每日排程能在小型主機上執行。
    """
    print("更新每日行情…", flush=True)
    daily_update.update(days_back=10)
    # 有完整面板（本機）就一併更新，否則只維護近期面板（伺服器）
    if adjust.PANEL_PATH.exists():
        adjust.update_panel()
    else:
        adjust.update_recent()

    print("更新公司行為…", flush=True)
    recent = (pd.Timestamp.today() - pd.DateOffset(months=3)).strftime("%Y-%m-%d")
    # save=False：只取近三個月，不可覆蓋完整歷史
    fresh = corporate.build(start=recent, refresh=True, save=False)
    path = CACHE / "corporate_actions.parquet"
    if path.exists() and len(fresh):
        old = pd.read_parquet(path)
        merged = pd.concat([old, fresh], ignore_index=True)
        merged = merged.drop_duplicates(subset=["stock_id", "date", "kind"])
        merged = merged.sort_values(["stock_id", "date"])
        if len(merged) < len(old):
            raise RuntimeError(
                f"合併後事件數減少（{len(old)} → {len(merged)}），已中止寫入以免損壞歷史")
        merged.to_parquet(path, index=False)
        print(f"  公司行為 {len(old):,} → {len(merged):,} 筆")


def main() -> None:
    args = sys.argv[1:]
    conn = journal.connect()

    if "--history" in args:
        h = journal.history(conn, limit=100)
        if h.empty:
            print("尚無已結束的交易紀錄")
        else:
            h["pnl_pct"] = (h["pnl_pct"] * 100).round(2).astype(str) + "%"
            print(h.to_string(index=False))
        return

    if "--no-update" not in args:
        refresh_data()

    from scripts.run_backtest import load_signals
    catchup = int(args[args.index("--catchup") + 1]) if "--catchup" in args else 0
    # 只計算需要的區間，避免每日排程載入十年全量資料
    panel = load_signals(tail_days=max(catchup, 5))
    taiex = adjust.load_taiex()

    dates = pd.DatetimeIndex(sorted(panel["date"].unique()))
    last = journal.last_processed(conn)

    if catchup:
        todo = dates[-catchup:]
    elif last is None:
        todo = dates[-1:]          # 首次執行只處理最新交易日
    else:
        todo = dates[dates > last]

    if len(todo) == 0:
        # 沒有新的交易日：假日、颱風假，或盤後報表尚未更新。
        # 此時不應推播，否則會把前一日的訊號重複送一次。
        latest = dates[-1]
        stale = (pd.Timestamp.today().normalize() - latest).days
        print(f"沒有新的交易日（最新資料為 {latest.date()}），不產生訊號")
        if stale > STALE_WARN_DAYS and "--push" in args:
            # 連續多日沒有新資料，多半是資料來源或排程出問題，值得通知
            from src import telegram_bot
            telegram_bot.send(
                f"⚠️ 資料已 {stale} 天未更新\n最新交易日：{latest.date()}\n"
                "若非連假，請檢查排程與資料來源")
        return

    text = ""
    for d in todo:
        text, _ = notify.run_day(conn, panel, taiex, d)
        if len(todo) > 1:
            print(f"處理 {d.date()}", flush=True)

    print("\n" + text)
    (RESULTS / f"signal_{todo[-1].date()}.txt").write_text(text, encoding="utf-8")

    if "--push" in args:
        from src import telegram_bot
        telegram_bot.send(text)
        print("\n已推播至 Telegram")


def _notify_failure(exc: Exception) -> None:
    """排程失敗時也推播，避免無人看管時靜默中斷。"""
    import traceback
    detail = traceback.format_exc(limit=3)
    text = (f"⚠️ 每日排程失敗\n{pd.Timestamp.now():%Y-%m-%d %H:%M}\n\n"
            f"{type(exc).__name__}: {str(exc)[:300]}\n\n{detail[-600:]}")
    print(text, file=sys.stderr)
    try:
        from src import telegram_bot
        telegram_bot.send(text)
    except Exception as push_err:                      # 推播本身也可能失敗
        print(f"[警告] 失敗通知無法送出：{push_err}", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # 只有在有設定推播時才嘗試通知，否則僅寫入 log
        if "--push" in sys.argv:
            _notify_failure(exc)
        else:
            raise
        sys.exit(1)
