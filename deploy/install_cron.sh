#!/usr/bin/env bash
# 設定每日盤後排程。台股 13:30 收盤，官方報表約 14:00-15:00 齊備，
# 因此設在 15:30 並留緩衝。

set -euo pipefail
cd "$(dirname "$0")/.."
PROJECT_DIR="$(pwd)"
TZ_NAME="Asia/Taipei"

CURRENT_TZ=$(timedatectl show -p Timezone --value 2>/dev/null || echo "unknown")
if [ "$CURRENT_TZ" != "$TZ_NAME" ]; then
    echo "==> 目前時區為 $CURRENT_TZ，設為 $TZ_NAME"
    sudo timedatectl set-timezone "$TZ_NAME"
fi

ENTRY="30 15 * * 1-5 cd $PROJECT_DIR && .venv/bin/python scripts/daily.py --push >> data/cron.log 2>&1"
WEEKLY="0 20 * * 6 cd $PROJECT_DIR && .venv/bin/python -c \"import sys; sys.path.insert(0,'.'); sys.argv=['x']; from scripts.run_backtest import load_signals; load_signals(refresh=True)\" >> data/cron.log 2>&1"

# 移除本專案舊的排程項目後重新寫入，避免重複
( crontab -l 2>/dev/null | grep -v "$PROJECT_DIR" || true
  echo "$ENTRY"
  echo "$WEEKLY" ) | crontab -

echo "==> 已設定排程："
crontab -l | grep "$PROJECT_DIR"
echo
echo "    週一至週五 15:30　產生訊號並推播"
echo "    每週六 20:00　　　完整重建面板，校正增量更新的累積誤差"
echo
echo "    查看紀錄：tail -f $PROJECT_DIR/data/cron.log"
