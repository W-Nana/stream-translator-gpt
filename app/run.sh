#!/bin/bash
# Stream Translator — Linux 快速啟動腳本（開發模式）

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=========================================="
echo "  Stream Translator FloatWindow"
echo "=========================================="
echo ""

# 檢查並啟用虛擬環境
if [ -d "../.venv" ]; then
    echo "啟用虛擬環境 ../.venv"
    source ../.venv/bin/activate
elif [ -d "venv" ]; then
    echo "啟用虛擬環境 venv"
    source venv/bin/activate
else
    echo "⚠️  找不到虛擬環境（../.venv 或 venv），使用系統 Python"
fi

# 檢查 Python 依賴
echo "檢查 Python 依賴..."
REQUIREMENTS="$SCRIPT_DIR/requirements.txt"
if [ -f "$REQUIREMENTS" ]; then
    pip install -q -r "$REQUIREMENTS" 2>/dev/null || echo "⚠️  部分依賴安裝失敗，請手動檢查"
fi

# 檢查前端依賴
FRONTEND_DIR="$SCRIPT_DIR/frontend"
NODE_MODULES="$FRONTEND_DIR/node_modules"

if [ ! -d "$NODE_MODULES" ]; then
    echo "安裝前端依賴..."
    (cd "$FRONTEND_DIR" && npm install)
fi

echo "✅ 依賴檢查完成"
echo ""

# 啟動應用程式
echo "啟動 Stream Translator（開發模式）..."
echo ""

python main.py "$@"
