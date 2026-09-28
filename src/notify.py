"""每日推播內容：賣出訊號、買進推薦、持有現況。

不含手續費與資金配置，純粹是訊號與紀錄，方便事後對照推薦與實際走勢。
"""

from __future__ import annotations

import unicodedata

import pandas as pd

from config import (ATR_INIT_STOP, MAX_NEW_PER_DAY, MAX_POSITIONS,
                    MAX_UNITS_PER_STOCK, RANK_TOP_N)
from src import journal, strategy


def _width(text: str) -> int:
    """等寬字型下的顯示寬度。中日韓字元佔兩格，但 len() 只算一格。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _truncate(text: str, width: int) -> str:
    """依顯示寬度截斷，避免少數過長的股名（如「北極星藥業-KY」）撐破欄位。"""
    if _width(text) <= width:
        return text
    out = ""
    for ch in text:
        if _width(out + ch) > width:
            break
        out += ch
    return out


def stock_names() -> dict[str, str]:
    """股票代號對應名稱。取不到時回傳空字典，顯示端會退回只顯示代號。"""
    try:
        from src import universe
        uni = universe.build()
        return dict(zip(uni["stock_id"], uni["stock_name"]))
    except Exception:
        return {}


def _cell(text: str, width: int, align: str = "<") -> str:
    """依顯示寬度補齊欄位，讓含中文的表頭能與數字對齊。"""
    pad = max(0, width - _width(str(text)))
    if align == ">":
        return " " * pad + str(text)
    return str(text) + " " * pad


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
                  holdings: list[dict], names: dict[str, str] | None = None) -> str:
    """組成推播文字。日期一律顯示，方便回頭對照。"""
    names = names or {}
    L = [f"📅 {date.date()} 盤後訊號",
         f"大盤：{'✅ 多頭' if market_ok else '⛔ 空頭，停止新進場'}"]

    sold = result.get("sold", [])
    if sold:
        L.append(f"\n🔴 賣出 ({len(sold)} 檔，整檔全數賣出)")
        for s in sold:
            n = s.get("units", 1)
            label = f"均價 {s['buy_price']:.2f}（買進 {n} 次）" if n > 1 else f"@ {s['buy_price']:.2f}"
            L.append(f"  {s['stock_id']} {names.get(s['stock_id'], '')}")
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
            L.append(f"  {b['stock_id']} {names.get(b['stock_id'], '')}"
                     f"　買 {date.date()} @ {b['price']:.2f}")

    rec = result.get("recommended", [])
    if rec:
        L.append(f"\n📈 明日買進推薦（依動能排序，最多 {RANK_TOP_N} 檔）")
        # 不設順位欄：清單由上而下即為動能排序
        L.append("  " + _cell("代號", 6) + _cell("名稱", 11)
                 + _cell("收盤", 10, ">") + _cell("觸發價", 11, ">")
                 + _cell("停損", 10, ">"))
        for c in rec:
            tag = f"  加碼 {c['units_held']+1}/{MAX_UNITS_PER_STOCK}" if c["is_add"] else ""
            L.append("  " + _cell(c["stock_id"], 6)
                     + _cell(_truncate(names.get(c["stock_id"], ""), 10), 11)
                     + _cell(f"{c['close']:.2f}", 10, ">")
                     + _cell(f"{c['trigger']:.2f}", 11, ">")
                     + _cell(f"{c['stop']:.2f}", 10, ">") + tag)
        L.append("  ※ 觸發價為停損買單價位，明日最高價觸及才成交")
        L.append(f"  ※ 單日最多進場 {MAX_NEW_PER_DAY} 筆，買多少由你決定")
    elif market_ok:
        full = MAX_POSITIONS is not None and len(holdings) >= MAX_POSITIONS
        L.append("\n📈 明日無新推薦" + ("（已達持股上限）" if full else "（無個股符合條件）"))

    if not holdings:
        L.append("\n📊 持有中 (0 檔)")
        L.append("  尚未買進任何股票")
        L.append("  推薦股票需在隔日觸及觸發價才會成交")
    else:
        cap = "無上限" if MAX_POSITIONS is None else f"上限 {MAX_POSITIONS}"
        L.append(f"\n📊 持有中 ({len(holdings)} 檔，{cap})")

        for h in holdings:
            if "note" in h:
                L.append(f"  * {h['stock_id']} {names.get(h['stock_id'], '')}"
                         f"　買 {h['buy_date']}　{h['note']}")
                continue
            warn = " ⚠️跌破50MA" if h["ma_break"] else ""
            n = h.get("units", 1)
            cost = f"均價 {h['buy_price']:.2f}" if n > 1 else f"成本 {h['buy_price']:.2f}"
            L.append(f"  * {h['stock_id']} {names.get(h['stock_id'], '')}　{cost}"
                     f"　現 {h['close']:.2f} ({h['pnl_pct']*100:+.1f}%)"
                     f"　停損 {h['stop']:.2f}　{h['days']}天{warn}")
            if n > 1:
                for bd, bp in h.get("buys", []):
                    L.append(f"      └ 買 {bd} @ {bp:.2f}")

    return "\n".join(L)


def format_compact(date: pd.Timestamp, market_ok: bool, result: dict,
                   holdings: list[dict], names: dict[str, str] | None = None) -> str:
    """手機用的精簡版。

    完整版一行較寬，在手機的等寬字型下會折行而難以閱讀；
    這裡把每行壓在 32 個顯示格內，細節則另以附檔提供。
    """
    names = names or {}

    def nm(sid: str) -> str:
        return _cell(_truncate(names.get(sid, ""), 8), 9)

    L = [f"📅 {date:%m/%d} 盤後　大盤 {'✅' if market_ok else '⛔'}"]

    for s_ in result.get("sold", []):
        L.append(f"\n🔴 賣出 {s_['stock_id']} {names.get(s_['stock_id'], '')}"
                 f" {s_['pnl_pct']*100:+.1f}% ({s_['days']}天)")
    for b in result.get("bought", []):
        L.append(f"\n🟢 買進 {b['stock_id']} {names.get(b['stock_id'], '')}"
                 f" @{b['price']:.0f}")

    rec = result.get("recommended", [])
    if rec:
        L.append(f"\n📈 明日買進 {len(rec)} 檔")
        L.append("代號 " + _cell("名稱", 9) + _cell("觸發", 8, ">")
                 + _cell("停損", 8, ">"))
        for c in rec:
            tag = f" 加{c['units_held']+1}" if c["is_add"] else ""
            L.append(f"{c['stock_id']} {nm(c['stock_id'])}"
                     f"{c['trigger']:>8.1f}{c['stop']:>8.1f}{tag}")
    elif market_ok:
        L.append("\n📈 明日無新推薦")

    if not holdings:
        L.append("\n📊 持有 0 檔")
        L.append("尚未買進，等待觸價成交")
    else:
        valid = [h for h in holdings if "note" not in h]
        avg = (sum(h["pnl_pct"] for h in valid) / len(valid) * 100) if valid else 0.0
        win = sum(1 for h in valid if h["pnl_pct"] > 0)
        L.append(f"\n📊 持有 {len(holdings)} 檔　均 {avg:+.1f}%　賺 {win}/{len(valid)}")
        L.append(_cell("", 2) + "代號 " + _cell("名稱", 9)
                 + _cell("損益", 8, ">") + _cell("停損", 9, ">"))
        for h in sorted(valid, key=lambda x: -x["pnl_pct"]):
            # 星號標示持有中，跌破均線者改用警示符號；兩者共用同一欄
            mark = _cell("⚠" if h["ma_break"] else "*", 2)
            L.append(f"{mark}{h['stock_id']} {nm(h['stock_id'])}"
                     f"{h['pnl_pct']*100:>+7.1f}%{h['stop']:>9.1f}")

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
    dates = pd.DatetimeIndex(sorted(panel["date"].unique()))
    pos = dates.get_loc(date)
    prev = panel[panel["date"] == dates[pos - 1]] if pos > 0 else None
    result = journal.settle_day(conn, day, date, max_new=MAX_NEW_PER_DAY, prev_day=prev)

    units: dict[str, int] = {}
    for p in journal.open_positions(conn):
        units[p.stock_id] = units.get(p.stock_id, 0) + 1
    candidates = build_candidates(day, units) if market_ok else []
    result["recommended"] = journal.record_candidates(
        conn, date, candidates, max_open=MAX_POSITIONS)

    holdings = journal.holdings_view(conn, day)
    names = stock_names()
    result["compact"] = format_compact(date, market_ok, result, holdings, names)
    return format_report(date, market_ok, result, holdings, names), result
