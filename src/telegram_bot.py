"""Telegram 推播與互動查詢。

推播只依賴 HTTP API，不需要常駐程式；互動模式則需要另外執行 serve()。
設定方式：在 .env 加入 TELEGRAM_BOT_TOKEN 與 TELEGRAM_CHAT_ID。
"""

from __future__ import annotations

import os
from pathlib import Path

import requests
from dotenv import load_dotenv

from config import RESULTS as RESULTS_DIR, ROOT

load_dotenv(ROOT / ".env")

API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 4000          # Telegram 單則訊息上限為 4096 字元


def _config() -> tuple[str, str]:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        raise RuntimeError("請先在 .env 設定 TELEGRAM_BOT_TOKEN 與 TELEGRAM_CHAT_ID")
    return token, chat_id


def send(text: str, chat_id: str | None = None) -> bool:
    """推播訊息，過長時自動分段。回傳是否全部成功。"""
    token, default_chat = _config()
    target = chat_id or default_chat

    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > MAX_LEN:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)

    ok = True
    for chunk in chunks:
        resp = requests.post(
            API.format(token=token, method="sendMessage"),
            json={"chat_id": target, "text": f"```\n{chunk}\n```",
                  "parse_mode": "MarkdownV2"},
            timeout=30,
        )
        if not resp.ok:
            # Markdown 解析失敗時退回純文字，確保訊息一定送得出去
            resp = requests.post(
                API.format(token=token, method="sendMessage"),
                json={"chat_id": target, "text": chunk}, timeout=30,
            )
        ok = ok and resp.ok
        if not resp.ok:
            print(f"[telegram] 推播失敗: {resp.text[:200]}")
    return ok


def send_document(path: Path, caption: str = "", chat_id: str | None = None) -> bool:
    """以附件傳送檔案。

    完整明細在手機上逐行顯示會過長，改為附檔讓使用者需要時才點開；
    Telegram 會為純文字檔提供內建預覽，不必下載即可閱讀。
    """
    token, default_chat = _config()
    with open(path, "rb") as fh:
        resp = requests.post(
            API.format(token=token, method="sendDocument"),
            data={"chat_id": chat_id or default_chat, "caption": caption[:1024]},
            files={"document": (path.name, fh, "text/plain")},
            timeout=60,
        )
    if not resp.ok:
        print(f"[telegram] 附件傳送失敗: {resp.text[:200]}")
    return resp.ok


def check() -> dict:
    """驗證設定是否正確，回傳 bot 基本資訊。"""
    token, chat_id = _config()
    info = requests.get(API.format(token=token, method="getMe"), timeout=20).json()
    return {"ok": info.get("ok"), "bot": info.get("result", {}).get("username"), "chat_id": chat_id}


def serve() -> None:
    """互動模式：支援 /today、/holdings、/help 指令。"""
    from telegram import Update
    from telegram.ext import Application, CommandHandler, ContextTypes

    token, _ = _config()

    async def cmd_today(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        from src import adjust, journal, notify
        from scripts.run_backtest import load_signals

        conn = journal.connect()
        panel = load_signals(tail_days=5)
        last = journal.last_processed(conn)
        text = (notify.format_report(panel["date"].max(), True,
                                     {"recommended": []},
                                     journal.holdings_view(
                                         conn, panel[panel["date"] == panel["date"].max()]))
                if last is None else
                (RESULTS_DIR / f"signal_{last.date()}.txt").read_text(encoding="utf-8"))
        await update.message.reply_text(text)

    async def cmd_holdings(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        import pandas as pd
        from src import journal
        from scripts.run_backtest import load_signals

        conn = journal.connect()
        panel = load_signals(tail_days=5)
        day = panel[panel["date"] == panel["date"].max()]
        holdings = journal.holdings_view(conn, day)
        if not holdings:
            await update.message.reply_text("目前無持股")
            return
        lines = []
        for h in holdings:
            if "note" in h:
                lines.append(f"{h['stock_id']}｜買 {h['buy_date']}｜{h['note']}")
                continue
            lines.append(f"{h['stock_id']}｜買 {h['buy_date']} @ {h['buy_price']:.2f}"
                         f"｜現 {h['close']:.2f} ({h['pnl_pct']*100:+.1f}%)"
                         f"｜停損 {h['stop']:.2f}")
        await update.message.reply_text("目前持股：\n" + "\n".join(lines))

    async def cmd_help(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "/today 產生今日盤後推薦\n/holdings 查看目前持股\n/help 顯示說明")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("holdings", cmd_holdings))
    app.add_handler(CommandHandler("help", cmd_help))
    print("Telegram bot 已啟動，按 Ctrl+C 結束")
    app.run_polling()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        serve()
    else:
        print(check())
