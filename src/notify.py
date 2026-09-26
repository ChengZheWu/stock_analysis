"""每日推播內容：賣出訊號、買進推薦、持有現況。

不含手續費與資金配置，純粹是訊號與紀錄，方便事後對照推薦與實際走勢。
"""

from __future__ import annotations

import pandas as pd

from config import (ATR_INIT_STOP, MAX_NEW_PER_DAY, MAX_POSITIONS,
                    MAX_UNITS_PER_STOCK, RANK_TOP_N)
from src import journal, strategy


def build_candidates(day: pd.DataFrame, units_held: dict[str, int],
                     top_n: int = RANK_TOP_N) -> list[dict]:
    """產生依動能排序的買進候選。

    已持有的股票仍可入列（代表持續看好、可加碼），但已買滿單檔上限者排除。
    """
    full = {s for s, n in units_held.items() if n >= MAX_UNITS_PER_STOCK}
    pool = day[~day["stock_id"].isin(full)]
    ranked = strategy.rank_candidates(pool, top_n)
    out = []
    for i, (_, r) in enumerate(ranked.iterrows(), 1):
        trigger = strategy.round_to_tick(float(r["trigger"]), up=True)
        atr = float(r["atr"])
        sid = r["stock_id"]
        n = units_held.get(sid, 0)
        out.append({
            "rank": i,
            "stock_id": sid,
            "close": round(float(r["close"]), 2),
            "trigger": trigger,
            "atr": round(atr, 2),
            "stop": strategy.round_to_tick(trigger - ATR_INIT_STOP * atr, up=False),
            "score": round(float(r["mom_adj"]), 2),
            "units_held": n,
            "is_add": n > 0,
        })
    return out


def format_report(date: pd.Timestamp, market_ok: bool, result: dict,
                  holdings: list[dict]) -> str:
    """組成推播文字。日期一律顯示，方便回頭對照。"""
    L = [f"📅 {date.date()} 盤後訊號",
         f"大盤：{'✅ 多頭' if market_ok else '⛔ 空頭，停止新進場'}"]

    sold = result.get("sold", [])
    if sold:
        L.append(f"\n🔴 賣出 ({len(sold)} 檔，整檔全數賣出)")
        for s in sold:
            n = s.get("units", 1)
            label = f"均價 {s['buy_price']:.2f}（買進 {n} 次）" if n > 1 else f"@ {s['buy_price']:.2f}"
            L.append(f"  {s['stock_id']}")
            for bd, bp in s.get("buys", []):
                L.append(f"    買 {bd} @ {bp:.2f}")
            if n > 1:
                L.append(f"    成本 {label}")
            L.append(f"    賣 {date.date()} @ {s['sell_price']:.2f}")
            L.append(f"    持有 {s['days']} 天　損益 {s['pnl_pct']*100:+.2f}%　{s['reason']}")

    bought = result.get("bought", [])
    if bought:
        L.append(f"\n🟢 今日成交買進 ({len(bought)} 檔)")
        for b in bought:
            L.append(f"  {b['stock_id']}　買 {date.date()} @ {b['price']:.2f}")

    rec = result.get("recommended", [])
    if rec:
        L.append(f"\n📈 明日買進推薦（依動能排序，最多 {RANK_TOP_N} 檔）")
        L.append(f"  {'順位':<4}{'代號':<7}{'收盤':>9}{'觸發價':>10}{'停損':>9}")
        for c in rec:
            tag = f"  加碼{c['units_held']+1}/{MAX_UNITS_PER_STOCK}" if c["is_add"] else ""
            L.append(f"  {c['rank']:<4}{c['stock_id']:<7}{c['close']:>9.2f}"
                     f"{c['trigger']:>10.2f}{c['stop']:>9.2f}{tag}")
        L.append("  ※ 觸發價為停損買單價位，明日最高價觸及才成交")
        L.append(f"  ※ 單日最多進場 {MAX_NEW_PER_DAY} 筆，買多少由你決定")
    elif market_ok:
        full = MAX_POSITIONS is not None and len(holdings) >= MAX_POSITIONS
        L.append("\n📈 明日無新推薦" + ("（已達持股上限）" if full else "（無個股符合條件）"))

    if holdings:
        cap = "無上限" if MAX_POSITIONS is None else f"上限 {MAX_POSITIONS}"
        L.append(f"\n📊 持有中 ({len(holdings)} 檔，{cap})")

        for h in holdings:
            if "note" in h:
                L.append(f"  {h['stock_id']}　買 {h['buy_date']}　{h['note']}")
                continue
            warn = " ⚠️跌破50MA" if h["ma_break"] else ""
            n = h.get("units", 1)
            cost = f"均價 {h['buy_price']:.2f}" if n > 1 else f"成本 {h['buy_price']:.2f}"
            L.append(f"  {h['stock_id']}　{cost}　現 {h['close']:.2f}"
                     f" ({h['pnl_pct']*100:+.1f}%)　停損 {h['stop']:.2f}"
                     f"　{h['days']}天{warn}")
            if n > 1:
                for bd, bp in h.get("buys", []):
                    L.append(f"      └ 買 {bd} @ {bp:.2f}")

    return "\n".join(L)


def run_day(conn, panel: pd.DataFrame, taiex: pd.DataFrame,
            date: pd.Timestamp) -> tuple[str, dict]:
    """處理指定交易日並產生推播內容。"""
    day = panel[panel["date"] == date]
    if day.empty:
        raise ValueError(f"{date.date()} 無資料")

    regime = strategy.market_regime(taiex)
    market_ok = bool(regime.reindex([date]).ffill().iloc[0]) if len(regime) else False

    # 先結算成交與出場，持股狀態更新後才產生新推薦，
    # 否則當日剛買進的股票會被重複推薦
    result = journal.settle_day(conn, day, date, max_new=MAX_NEW_PER_DAY)

    units: dict[str, int] = {}
    for p in journal.open_positions(conn):
        units[p.stock_id] = units.get(p.stock_id, 0) + 1
    candidates = build_candidates(day, units) if market_ok else []
    result["recommended"] = journal.record_candidates(
        conn, date, candidates, max_open=MAX_POSITIONS)

    holdings = journal.holdings_view(conn, day)
    return format_report(date, market_ok, result, holdings), result
