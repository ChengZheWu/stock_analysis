"""FinMind API 客戶端：滑動視窗限流、自動重試、本地快取。

免費帳號額度為 600 次/小時。限流器以滑動視窗記錄每次請求時間，
逼近上限時自動等待，讓長時間的全量抓取可以無人看管地跑完。
"""

from __future__ import annotations

import os
import time
import threading
from collections import deque
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

from config import ROOT

load_dotenv(ROOT / ".env")

API_URL = "https://api.finmindtrade.com/api/v4/data"
HOURLY_LIMIT = 600
WINDOW = 3600.0
SAFETY_MARGIN = 10        # 保留餘裕，避免剛好踩線
MAX_RETRY = 5


class RateLimiter:
    """滑動視窗限流：確保任何 60 分鐘內的請求數不超過上限。"""

    def __init__(self, limit: int = HOURLY_LIMIT - SAFETY_MARGIN, window: float = WINDOW):
        self.limit = limit
        self.window = window
        self.calls: deque[float] = deque()
        self.lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self.lock:
                now = time.time()
                while self.calls and now - self.calls[0] >= self.window:
                    self.calls.popleft()
                if len(self.calls) < self.limit:
                    self.calls.append(now)
                    return
                wait = self.window - (now - self.calls[0]) + 1.0
            print(f"[rate-limit] 已達額度上限，等待 {wait/60:.1f} 分鐘…", flush=True)
            time.sleep(wait)


_limiter = RateLimiter()


class FinMindError(RuntimeError):
    pass


def _token() -> str:
    tok = os.getenv("FINMIND_TOKEN", "")
    if not tok:
        raise FinMindError("找不到 FINMIND_TOKEN，請確認專案根目錄的 .env 檔")
    return tok


def request(dataset: str, **params) -> pd.DataFrame:
    """呼叫 FinMind API，回傳 DataFrame。遇到限流或暫時性錯誤會自動退避重試。"""
    headers = {"Authorization": f"Bearer {_token()}"}
    payload = {"dataset": dataset, **params}

    for attempt in range(MAX_RETRY):
        _limiter.acquire()
        try:
            resp = requests.get(API_URL, params=payload, headers=headers, timeout=60)
        except requests.RequestException as exc:
            wait = 5 * (attempt + 1)
            print(f"[retry] {dataset} {params.get('data_id','')} 連線失敗 {exc}，{wait}s 後重試", flush=True)
            time.sleep(wait)
            continue

        if resp.status_code in (402, 429):
            print(f"[retry] {dataset} 被限流 (HTTP {resp.status_code})，等待 10 分鐘", flush=True)
            time.sleep(600)
            continue

        try:
            body = resp.json()
        except ValueError:
            time.sleep(5 * (attempt + 1))
            continue

        msg = str(body.get("msg", ""))
        if body.get("status") == 200:
            return pd.DataFrame(body.get("data", []))

        if "limit" in msg.lower() or "request" in msg.lower():
            print(f"[retry] {dataset} 額度訊息：{msg[:60]}，等待 10 分鐘", flush=True)
            time.sleep(600)
            continue

        raise FinMindError(f"{dataset} {params.get('data_id','')}: {msg[:120]}")

    raise FinMindError(f"{dataset} {params.get('data_id','')}: 重試 {MAX_RETRY} 次仍失敗")


def cached(path: Path, dataset: str, *, refresh: bool = False, **params) -> pd.DataFrame:
    """帶檔案快取的請求。已存在且未指定 refresh 時直接讀本地檔，不耗用額度。"""
    if path.exists() and not refresh:
        return pd.read_parquet(path)
    df = request(dataset, **params)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    return df
