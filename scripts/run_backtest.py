"""執行 10 年 / 5 年 / 1 年三個窗口的回測，輸出績效與月結算。

用法:
    python scripts/run_backtest.py            # 三個窗口
    python scripts/run_backtest.py 10y        # 指定單一窗口
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from config import (BACKFILL_START, BACKTEST_WINDOWS, BENCHMARK, CACHE,
                    INITIAL_CAPITAL, RESULTS)
from src import adjust, backtest, indicators, report, strategy

SIGNAL_PATH = CACHE / "signals.parquet"


KEEP = ["stock_id", "date", "open", "high", "low", "close", "volume", "atr",
        "ma50", "trigger", "mom_adj", "eligible"]


def load_signals(refresh: bool = False, tail_days: int | None = None) -> pd.DataFrame:
    """還原股價 → 技術指標 → 選股訊號，結果快取起來供重複回測使用。

    tail_days 只計算最近 N 個交易日的訊號。指標最長回看 252 日，
    每日排程用不到十年全量，限制範圍可把峰值記憶體從約 5 GB 降到數百 MB。
    """
    if SIGNAL_PATH.exists() and not refresh and tail_days is None:
        return pd.read_parquet(SIGNAL_PATH)

    if tail_days is not None:
        # 讀取近期面板而非完整面板：完整面板約 500 萬筆，小型主機記憶體不足
        panel = adjust.load_recent()
        dates = pd.DatetimeIndex(sorted(panel["date"].unique()))
        # 多留 300 日供指標暖身，確保最近 tail_days 的指標數值正確
        start = dates[max(0, len(dates) - tail_days - 300)]
        panel = panel[panel["date"] >= start]
    else:
        print("建立還原股價面板…", flush=True)
        panel = adjust.build_panel(refresh=refresh)

    print("計算技術指標…", flush=True)
    panel = indicators.add_indicators(panel)
    print("產生選股訊號…", flush=True)
    panel = strategy.add_signals(panel)[KEEP]

    if tail_days is None:
        panel.to_parquet(SIGNAL_PATH, index=False)
    return panel


def benchmark_series(dates: pd.DatetimeIndex) -> pd.Series:
    """0050 買進持有的淨值曲線（已還原除權息）。

    基準只需要單一標的，改用 Yahoo 取得可避免佔用 FinMind 額度；
    0050 至今仍在交易，不涉及存活者偏差。
    """
    path = CACHE / f"benchmark_{BENCHMARK}.parquet"
    if path.exists():
        cached = pd.read_parquet(path)
        s = pd.Series(cached["close"].values, index=pd.to_datetime(cached["date"]))
        if s.index.max() >= dates.max() - pd.Timedelta(days=7):
            return s.reindex(dates).ffill()

    try:
        import yfinance as yf
        df = yf.download(f"{BENCHMARK}.TW", start=BACKFILL_START, progress=False,
                         auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]
        if df.empty:
            raise RuntimeError("Yahoo 無回應")
        out = pd.DataFrame({"date": df.index, "close": df["Close"].values})
        out.to_parquet(path, index=False)
        return pd.Series(out["close"].values, index=out["date"]).reindex(dates).ffill()
    except Exception as exc:
        print(f"[警告] 無法取得 {BENCHMARK} 基準資料，將略過比較：{exc}")
        return pd.Series(dtype=float)


def main() -> None:
    wanted = sys.argv[1:] or list(BACKTEST_WINDOWS)
    panel = load_signals()
    taiex = adjust.load_taiex()

    last_date = panel["date"].max()
    print(f"\n資料截至 {last_date.date()}，共 {panel['stock_id'].nunique()} 檔股票\n")

    for msg in config.check_sizing(INITIAL_CAPITAL):
        print(f"[警告] {msg}\n")

    rows, monthlies = [], {}
    for name in wanted:
        years = BACKTEST_WINDOWS[name]
        start = (last_date - pd.DateOffset(years=years)).strftime("%Y-%m-%d")
        end = last_date.strftime("%Y-%m-%d")
        print(f"回測 {name}：{start} ~ {end}", flush=True)

        res = backtest.run(panel, taiex, start, end, capital=INITIAL_CAPITAL)
        bench = benchmark_series(res.equity.index)

        rows.append(report.metrics(res.equity, res.trades, label=f"策略 {name}"))
        # 雙基準：0050 代表大型股，大盤報酬指數代表全市場，兩者皆為含息基礎
        market = (taiex.set_index("date")["price"].reindex(res.equity.index).ffill())
        for label, series in (("0050", bench), ("大盤", market)):
            s = series.dropna()
            if len(s) > 1:
                rows.append(report.metrics(
                    (s / s.iloc[0] * INITIAL_CAPITAL).rename("equity"),
                    None, label=f"{label} {name}"))

        monthlies[name] = report.monthly_table(res.equity, bench)
        res.equity.to_csv(RESULTS / f"equity_{name}.csv")
        if len(res.trades):
            res.trades.to_csv(RESULTS / f"trades_{name}.csv", index=False)
        monthlies[name].to_csv(RESULTS / f"monthly_{name}.csv")

    print("\n" + "=" * 100)
    print("績效總覽")
    print("=" * 100)
    print(report.format_metrics(rows))

    for name, tbl in monthlies.items():
        print(f"\n{'=' * 100}\n{name} 月結算（最後 12 個月）\n{'=' * 100}")
        show = tbl.tail(12).copy()
        for col in ("月報酬", "累積報酬", "基準月報酬", "超額報酬"):
            if col in show:
                show[col] = (show[col] * 100).round(2).astype(str) + "%"
        print(show.to_string())

    print(f"\n明細已輸出至 {RESULTS}")


if __name__ == "__main__":
    main()
