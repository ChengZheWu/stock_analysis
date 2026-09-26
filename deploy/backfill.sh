#!/usr/bin/env bash
# 建立歷史資料。可中斷續跑，已完成的部分會自動略過。
# 總耗時約 2 小時，建議用 nohup 或 tmux 在背景執行。

set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python

if ! grep -q "FINMIND_TOKEN=." .env 2>/dev/null; then
    echo "!! .env 缺少 FINMIND_TOKEN，請先填入（免費申請：finmindtrade.com）" >&2
    exit 1
fi

echo "==> [1/4] 加權指數、股票分割、面額變更（約 1 分鐘）"
$PY scripts/backfill.py market

echo "==> [2/4] 除權息與減資（約 15 分鐘）"
$PY -m src.corporate

echo "==> [3/4] 十年逐日行情（約 105 分鐘，可中斷續跑）"
$PY scripts/backfill_twse.py

echo "==> [4/4] 建立還原面板與選股訊號（約 2 分鐘，峰值記憶體約 5GB）"
$PY -c "
import sys; sys.path.insert(0, '.')
sys.argv = ['x']
from scripts.run_backtest import load_signals
p = load_signals(refresh=True)
print(f'  完成：{p.stock_id.nunique()} 檔、{len(p):,} 筆，資料至 {p.date.max().date()}')
"

echo
echo "==> 歷史資料建立完成"
echo "    驗證：.venv/bin/python -m pytest tests/ -q"
echo "    試跑：.venv/bin/python scripts/daily.py --no-update"
echo "    排程：bash deploy/install_cron.sh"
