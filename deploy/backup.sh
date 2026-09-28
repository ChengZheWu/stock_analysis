#!/usr/bin/env bash
# 備份買賣紀錄與公司行為資料。
#
# journal.db 記錄每一筆推薦與買賣，無法從其他資料重建，遺失即永久損失。
# corporate_actions.parquet 重建需約 15 分鐘，一併備份可加快復原。
#
# 用法：
#   bash deploy/backup.sh                      # 備份到 data/backups/
#   bash deploy/backup.sh root@1.2.3.4         # 從伺服器拉回本機備份

set -euo pipefail
cd "$(dirname "$0")/.."

REMOTE="${1:-}"
DEST="data/backups"
STAMP=$(date +%Y%m%d-%H%M)
KEEP=30                     # 保留最近 30 份

mkdir -p "$DEST"

if [ -n "$REMOTE" ]; then
    REMOTE_DIR="${REMOTE_DIR:-/root/stock_analysis}"
    echo "==> 從 $REMOTE 取回紀錄"
    TMP=$(mktemp -d)
    trap 'rm -rf "$TMP"' EXIT
    scp -q "$REMOTE:$REMOTE_DIR/data/results/journal.db" "$TMP/" || {
        echo "!! 伺服器上找不到 journal.db" >&2; exit 1; }
    SRC_DB="$TMP/journal.db"
else
    SRC_DB="data/results/journal.db"
    [ -f "$SRC_DB" ] || { echo "!! 找不到 $SRC_DB" >&2; exit 1; }
fi

OUT="$DEST/journal-$STAMP.db"
# 用 sqlite3 的 .backup 而非直接複製：可在程式寫入時安全取得一致的快照
if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$SRC_DB" ".backup '$OUT'"
else
    cp "$SRC_DB" "$OUT"
fi
gzip -f "$OUT"
echo "  $OUT.gz  ($(du -h "$OUT.gz" | cut -f1))"

if [ -f data/cache/corporate_actions.parquet ]; then
    cp data/cache/corporate_actions.parquet "$DEST/corporate-$STAMP.parquet"
    gzip -f "$DEST/corporate-$STAMP.parquet"
    echo "  $DEST/corporate-$STAMP.parquet.gz"
fi

# 清掉過舊的備份，避免無限累積
ls -1t "$DEST"/journal-*.db.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
ls -1t "$DEST"/corporate-*.parquet.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f

echo "==> 完成，目前保留 $(ls -1 "$DEST"/journal-*.db.gz 2>/dev/null | wc -l) 份紀錄備份"
