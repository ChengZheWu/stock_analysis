#!/usr/bin/env bash
# 把每日預測所需的資料從本機同步到伺服器。
#
# 開發與回測在本機進行，伺服器只負責每天產生訊號，因此不需要完整面板
# （約 150 MB）與十年逐日報表（約 155 MB），只要近期面板等約 23 MB。
#
# 用法：
#   bash deploy/sync.sh ubuntu@1.2.3.4 [~/.ssh/your_key]

set -euo pipefail
cd "$(dirname "$0")/.."

TARGET="${1:-}"
KEY="${2:-}"
REMOTE_DIR="${REMOTE_DIR:-stock_analysis}"

if [ -z "$TARGET" ]; then
    echo "用法: bash deploy/sync.sh user@host [ssh_key]" >&2
    exit 1
fi

SSH_OPT=""
[ -n "$KEY" ] && SSH_OPT="-e \"ssh -i $KEY\""

FILES=(
    data/cache/panel_recent.parquet
    data/cache/corporate_actions.parquet
    data/cache/taiex.parquet
    data/cache/split.parquet
    data/cache/par_value.parquet
    data/cache/stock_info.parquet
    data/cache/delisting.parquet
    data/cache/universe.parquet
    data/cache/benchmark_0050.parquet
)

missing=0
for f in "${FILES[@]}"; do
    [ -f "$f" ] || { echo "!! 缺少 $f" >&2; missing=1; }
done
if [ "$missing" -eq 1 ]; then
    echo "   請先在本機執行 bash deploy/backfill.sh 建立資料" >&2
    exit 1
fi

echo "==> 同步 $(du -ch "${FILES[@]}" | tail -1 | cut -f1) 到 $TARGET:$REMOTE_DIR"
if [ -n "$KEY" ]; then
    rsync -avz --progress -e "ssh -i $KEY" "${FILES[@]}" "$TARGET:$REMOTE_DIR/data/cache/"
else
    rsync -avz --progress "${FILES[@]}" "$TARGET:$REMOTE_DIR/data/cache/"
fi

echo
echo "==> 同步完成"
echo "    伺服器上驗證：cd $REMOTE_DIR && .venv/bin/python scripts/daily.py --no-update"
