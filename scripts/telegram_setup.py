"""協助取得 Telegram 的 chat id 並驗證推播設定。

用法:
    python scripts/telegram_setup.py           # 列出可用的聊天室
    python scripts/telegram_setup.py --save    # 找到唯一一個時寫回 .env
    python scripts/telegram_setup.py --test    # 發送一則測試訊息
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import ROOT

ENV_PATH = ROOT / ".env"
API = "https://api.telegram.org/bot{token}/{method}"


def _token() -> str:
    load_dotenv(ENV_PATH)
    tok = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not tok:
        raise SystemExit(
            f"請先在 {ENV_PATH} 加入一行：\n"
            "    TELEGRAM_BOT_TOKEN=你的token\n"
            "（token 由 Telegram 的 @BotFather 建立 bot 後取得）")
    return tok


def _call(token: str, method: str, **params) -> dict:
    resp = requests.get(API.format(token=token, method=method), params=params, timeout=30)
    body = resp.json()
    if not body.get("ok"):
        raise SystemExit(f"Telegram 回應錯誤：{body.get('description', body)}")
    return body["result"]


def find_chats(token: str) -> list[dict]:
    """從最近的更新中找出所有出現過的聊天室。"""
    updates = _call(token, "getUpdates", timeout=0, allowed_updates='["message","channel_post"]')
    seen: dict[int, dict] = {}
    for u in updates:
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat")
        if chat and chat["id"] not in seen:
            seen[chat["id"]] = chat
    return list(seen.values())


def _describe(chat: dict) -> str:
    kind = {"private": "私訊", "group": "群組",
            "supergroup": "超級群組", "channel": "頻道"}.get(chat["type"], chat["type"])
    name = chat.get("title") or " ".join(
        filter(None, [chat.get("first_name"), chat.get("last_name")])) or chat.get("username", "")
    return f"{kind}　{name}"


def save_chat_id(chat_id: int) -> None:
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    lines = [l for l in lines if not l.startswith("TELEGRAM_CHAT_ID=")]
    lines.append(f"TELEGRAM_CHAT_ID={chat_id}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ENV_PATH.chmod(0o600)
    print(f"已寫入 {ENV_PATH}：TELEGRAM_CHAT_ID={chat_id}")


def main() -> None:
    args = set(sys.argv[1:])
    token = _token()

    me = _call(token, "getMe")
    print(f"Bot：@{me['username']}（{me.get('first_name', '')}）\n")

    if "--test" in args:
        from src import telegram_bot
        ok = telegram_bot.send("✅ 台股動能選股系統：推播測試成功")
        print("測試訊息已送出" if ok else "測試訊息送出失敗")
        return

    chats = find_chats(token)
    if not chats:
        print("找不到任何聊天室。請依情境操作後再執行一次：\n"
              f"  ・群組：在群組中傳一則 /start@{me['username']}\n"
              "  　（bot 預設的隱私模式只看得到指令或提及它的訊息）\n"
              "  ・私訊：直接對 bot 傳任意訊息\n"
              "  ・頻道：把 bot 設為管理員後發一則訊息")
        return

    print(f"找到 {len(chats)} 個聊天室：")
    for c in chats:
        print(f"  chat_id = {c['id']:<16} {_describe(c)}")

    if "--save" in args:
        if len(chats) == 1:
            save_chat_id(chats[0]["id"])
            print("\n接著執行：.venv/bin/python scripts/telegram_setup.py --test")
        else:
            print("\n有多個聊天室，請手動把想要的 chat_id 填入 .env：")
            print("    TELEGRAM_CHAT_ID=上面其中一個數字")
    else:
        print("\n確認無誤後加上 --save 寫入 .env，或手動填入。")


if __name__ == "__main__":
    main()
