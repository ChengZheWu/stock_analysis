"""十年逐年績效報表：每年報酬、回撤、交易數與同時持股數量。

用法:
    python scripts/yearly_report.py           # 預設本金與十年區間
    python scripts/yearly_report.py 1000000   # 指定本金
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from src import adjust, backtest, report


def main() -> None:
    capital = float(sys.argv[1]) if len(sys.argv) > 1 else float(config.INITIAL_CAPITAL)

    from scripts.run_backtest import load_signals
    panel = load_signals()
    taiex = adjust.load_taiex()

    end = panel["date"].max()
    start = (end - pd.DateOffset(years=10)).strftime("%Y-%m-%d")

    for msg in config.check_sizing(capital):
        print(f"[警告] {msg}\n")

    print(f"回測 {start} ~ {end.date()}，本金 {capital:,.0f} 元\n", flush=True)
    res = backtest.run(panel, taiex, start, end.strftime("%Y-%m-%d"), capital=capital)
    eq, tr = res.equity, res.trades

    # 逐日同時持股檔數。以股票計而非筆數：同一檔多次加碼仍算一檔。
    held = pd.Series(0, index=eq.index, dtype=int)
    for d in eq.index:
        held[d] = tr[(tr["entry_date"] <= d) & (tr["exit_date"] > d)]["stock_id"].nunique()

    print("=" * 78)
    print("同時持股數量（以檔數計）")
    print("=" * 78)
    print(f"  最大 {held.max()} 檔　平均 {held.mean():.1f} 檔　中位數 {held.median():.0f} 檔")
    print(f"  完全空手 {(held == 0).sum()} 天（{(held == 0).mean()*100:.1f}%）\n")
    vc = held.value_counts().sort_index()
    for k, v in vc.items():
        pct = v / len(held) * 100
        if pct >= 1:
            print(f"   {k:2d} 檔 {v:5d} 天 {pct:5.1f}% {'█' * int(pct / 1.5)}")

    bench = pd.read_parquet(config.CACHE / f"benchmark_{config.BENCHMARK}.parquet")
    bench["date"] = pd.to_datetime(bench["date"])
    b = bench.set_index("date")["close"].reindex(eq.index).ffill()

    print("\n" + "=" * 78)
    print(f"逐年狀況（{capital:,.0f} 元滾動）")
    print("=" * 78)
    head = (f"{'年':<6}{'年初':>11}{'年末':>11}{'報酬':>9}"
            f"{'0050':>9}{'超額':>9}{'年內回撤':>9}{'交易':>6}{'均持股':>7}")
    print(head)
    print("-" * len(head))

    ye, yb = eq.resample("YE").last(), b.resample("YE").last()
    prev_e, prev_b = eq.iloc[0], b.iloc[0]
    for d in ye.index:
        mask = eq.index.year == d.year
        if mask.sum() < 20:
            continue
        e1, b1 = ye[d], yb[d]
        sub = eq[mask]
        dd = (sub / sub.cummax() - 1).min()
        n = len(tr[tr["exit_date"].dt.year == d.year])
        print(f"{d.year:<6}{prev_e:>11,.0f}{e1:>11,.0f}{(e1/prev_e-1)*100:>8.1f}%"
              f"{(b1/prev_b-1)*100:>8.1f}%{((e1/prev_e)-(b1/prev_b))*100:>8.1f}%"
              f"{dd*100:>8.1f}%{n:>6}{held[mask].mean():>7.1f}")
        prev_e, prev_b = e1, b1

    print("\n" + "=" * 78)
    print("十年總計")
    print("=" * 78)
    m = report.metrics(eq, tr)
    mb = report.metrics((b / b.iloc[0] * capital).rename("equity"), None)
    rows = [
        ("期初資金", f"{capital:,.0f}", f"{capital:,.0f}"),
        ("期末資金", f"{m['期末資金']:,.0f}", f"{mb['期末資金']:,.0f}"),
        ("總報酬率", f"{m['總報酬率']*100:,.1f}%", f"{mb['總報酬率']*100:,.1f}%"),
        ("年化報酬", f"{m['年化報酬率']*100:.2f}%", f"{mb['年化報酬率']*100:.2f}%"),
        ("年化波動", f"{m['年化波動度']*100:.2f}%", f"{mb['年化波動度']*100:.2f}%"),
        ("夏普值", f"{m['夏普值']:.2f}", f"{mb['夏普值']:.2f}"),
        ("最大回撤", f"{m['最大回撤']*100:.2f}%", f"{mb['最大回撤']*100:.2f}%"),
        ("最大回撤日", str(m["最大回撤日"]), str(mb["最大回撤日"])),
    ]
    print(f"{'':12}{'策略':>14}{'0050':>14}")
    for k, a, c in rows:
        print(f"{k:<12}{a:>14}{c:>14}")

    print(f"\n交易 {m['交易次數']:,.0f} 筆　勝率 {m['勝率']*100:.1f}%　"
          f"獲利因子 {m['獲利因子']:.2f}")
    print(f"平均獲利 {m['平均獲利']*100:+.1f}%　平均虧損 {m['平均虧損']*100:+.1f}%")
    print(f"平均持有 {m['平均持有天數']:.0f} 天　最長 {m['最長持有天數']:.0f} 天")
    print(f"最大單筆 {m['最大單筆獲利']*100:+.1f}% ／ {m['最大單筆虧損']*100:+.1f}%")


if __name__ == "__main__":
    main()
