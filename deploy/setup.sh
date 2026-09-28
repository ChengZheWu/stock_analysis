#!/usr/bin/env bash
# 在全新的 Ubuntu 主機上建置執行環境（適用 Oracle Cloud ARM / x86）。
#
# 用法：
#   bash deploy/setup.sh
#
# 完成後仍需手動：
#   1. 建立 .env（FINMIND_TOKEN、TELEGRAM_BOT_TOKEN、TELEGRAM_CHAT_ID）
#   2. 執行 deploy/backfill.sh 建立歷史資料（約 2 小時）
#   3. 執行 deploy/install_cron.sh 設定每日排程

set -euo pipefail
cd "$(dirname "$0")/.."
PROJECT_DIR="$(pwd)"

echo "==> 專案目錄：$PROJECT_DIR"
echo "==> 系統架構：$(uname -m)"

echo "==> 安裝系統套件"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-dev build-essential curl sqlite3

PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "==> Python 版本：$PY_VER"
if [ "$(printf '%s\n3.11\n' "$PY_VER" | sort -V | head -1)" != "3.11" ]; then
    echo "!! 需要 Python 3.11 以上，目前為 $PY_VER" >&2
    exit 1
fi

echo "==> 建立虛擬環境"
if [ ! -d .venv ]; then
    python3 -m venv .venv 2>/dev/null || {
        # 部分精簡版映像缺少 ensurepip，改以 get-pip 引導
        python3 -m venv --without-pip .venv
        curl -sS https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
        .venv/bin/python /tmp/get-pip.py -q
        rm -f /tmp/get-pip.py
    }
fi

echo "==> 安裝 Python 套件（ARM 主機可能需要較久）"
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

echo "==> 驗證"
.venv/bin/python -c "import pandas, numpy, pyarrow, requests, dotenv; print('  套件載入正常')"
.venv/bin/python -c "
import sys; sys.path.insert(0, '.')
import config
print(f'  設定載入正常：本金 {config.INITIAL_CAPITAL:,}, 每日推薦 {config.RANK_TOP_N} 檔')
for w in config.check_sizing():
    print(f'  [警告] {w}')
"

mkdir -p data/raw/daily data/cache data/results

# 完整重建面板的峰值約 1.2 GB，日常排程約 750 MB。
# 2 GB 記憶體的主機若同時跑其他程式，重建時可能不足；
# 建立 swap 可讓這個一次性步驟安全完成，日常排程不會用到。
TOTAL_MB=$(free -m | awk '/^Mem:/{print $2}')
SWAP_MB=$(free -m | awk '/^Swap:/{print $2}')
if [ "$TOTAL_MB" -lt 3000 ] && [ "$SWAP_MB" -lt 1000 ]; then
    echo "==> 記憶體 ${TOTAL_MB}MB 且無 swap，建立 2GB swap"
    sudo fallocate -l 2G /swapfile || sudo dd if=/dev/zero of=/swapfile bs=1M count=2048
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile >/dev/null
    sudo swapon /swapfile
    grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
    echo "    完成：$(free -h | awk '/^Swap:/{print $2}') swap"
fi

if [ ! -f .env ]; then
    cat > .env <<'ENVEOF'
FINMIND_TOKEN=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
ENVEOF
    chmod 600 .env
    echo "==> 已建立 .env 範本，請填入你的 token"
fi

echo
echo "==> 環境建置完成"
echo "    下一步：編輯 .env 後執行 bash deploy/backfill.sh"
