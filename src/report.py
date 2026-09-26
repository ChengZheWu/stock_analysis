"""績效指標、月結算與基準比較。"""

from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def drawdown(equity: pd.Series) -> pd.Series:
    return equity / equity.cummax() - 1.0


def metrics(equity: pd.Series, trades: pd.DataFrame, label: str = "") -> dict:
    eq = equity.dropna()
    if len(eq) < 2:
        return {"標籤": label}

    ret = eq.pct_change().dropna()
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    total = eq.iloc[-1] / eq.iloc[0] - 1.0
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1.0 if years > 0 else np.nan
    vol = ret.std() * np.sqrt(TRADING_DAYS)
    sharpe = (ret.mean() * TRADING_DAYS) / vol if vol > 0 else np.nan
    dd = drawdown(eq)
    downside = ret[ret < 0].std() * np.sqrt(TRADING_DAYS)
    sortino = (ret.mean() * TRADING_DAYS) / downside if downside and downside > 0 else np.nan

    out = {
        "標籤": label,
        "起始日": eq.index[0].date(),
        "結束日": eq.index[-1].date(),
        "年數": round(years, 2),
        "期初資金": round(eq.iloc[0]),
        "期末資金": round(eq.iloc[-1]),
        "總報酬率": total,
        "年化報酬率": cagr,
        "年化波動度": vol,
        "夏普值": sharpe,
        "索提諾值": sortino,
        "最大回撤": dd.min(),
        "最大回撤日": dd.idxmin().date() if len(dd) else None,
        "卡瑪值": cagr / abs(dd.min()) if dd.min() < 0 else np.nan,
    }

    if trades is not None and len(trades):
        wins = trades[trades["pnl"] > 0]
        losses = trades[trades["pnl"] <= 0]
        gross_win, gross_loss = wins["pnl"].sum(), abs(losses["pnl"].sum())
        out.update({
            "交易次數": len(trades),
            "勝率": len(wins) / len(trades),
            "平均獲利": wins["pnl_pct"].mean() if len(wins) else np.nan,
            "平均虧損": losses["pnl_pct"].mean() if len(losses) else np.nan,
            "獲利因子": gross_win / gross_loss if gross_loss > 0 else np.inf,
            "平均持有天數": trades["hold_days"].mean(),
            "最長持有天數": trades["hold_days"].max(),
            "最大單筆獲利": trades["pnl_pct"].max(),
            "最大單筆虧損": trades["pnl_pct"].min(),
        })
    else:
        out["交易次數"] = 0
    return out


def monthly_table(equity: pd.Series, benchmark: pd.Series | None = None) -> pd.DataFrame:
    """每月結算：月報酬、月末資金，並附上基準同期表現。"""
    eq = equity.dropna()
    # 資料中若有整月缺口，resample 會產生 NaN 月份；先剔除再計算，
    # 否則月報酬的長度會與月末資金不一致。
    m = eq.resample("ME").last().dropna()
    first = pd.Series([eq.iloc[0]], index=[eq.index[0] - pd.Timedelta(days=1)])
    tbl = pd.DataFrame({
        "月末資金": m.round(0),
        "月報酬": pd.concat([first, m]).pct_change().reindex(m.index).values,
    })
    tbl["累積報酬"] = m / eq.iloc[0] - 1.0

    if benchmark is not None and len(benchmark.dropna()):
        b = benchmark.reindex(eq.index).ffill().dropna()
        if len(b) > 1:
            bm = b.resample("ME").last()
            bfirst = pd.Series([b.iloc[0]], index=[b.index[0] - pd.Timedelta(days=1)])
            tbl["基準月報酬"] = pd.concat([bfirst, bm]).pct_change().dropna().reindex(tbl.index).values
            tbl["超額報酬"] = tbl["月報酬"] - tbl["基準月報酬"]
    return tbl


def format_metrics(rows: list[dict]) -> str:
    """把多個窗口的指標排成對照表。"""
    df = pd.DataFrame(rows).set_index("標籤").T
    pct_rows = {"總報酬率", "年化報酬率", "年化波動度", "最大回撤", "勝率",
                "平均獲利", "平均虧損", "最大單筆獲利", "最大單筆虧損"}
    lines = []
    for name, row in df.iterrows():
        vals = []
        for v in row:
            if isinstance(v, (int, float, np.floating)) and not isinstance(v, bool):
                if name in pct_rows:
                    vals.append("n/a" if pd.isna(v) else f"{v*100:,.2f}%")
                elif name in {"期初資金", "期末資金"}:
                    vals.append(f"{v:,.0f}")
                else:
                    vals.append("n/a" if pd.isna(v) else f"{v:,.2f}")
            else:
                vals.append(str(v))
        lines.append(f"{name:<12}" + "".join(f"{v:>16}" for v in vals))
    header = f"{'指標':<12}" + "".join(f"{c:>16}" for c in df.columns)
    return header + "\n" + "-" * len(header) + "\n" + "\n".join(lines)
