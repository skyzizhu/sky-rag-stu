#!/usr/bin/env bash
# 启动 Qdrant 向量数据库（数据保存在项目 storage/qdrant_data/，已 gitignore）
# 停止：在这个终端按 Ctrl+C
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN="$ROOT/tools/qdrant/qdrant"
DATA_DIR="$ROOT/storage/qdrant_data"

if [ ! -x "$BIN" ]; then
  echo "❌ 还没安装 Qdrant，先运行：bash scripts/install_qdrant.sh"
  exit 1
fi

mkdir -p "$DATA_DIR"

echo "🚀 启动 Qdrant（数据目录：${DATA_DIR}）"
echo "   控制台地址：http://localhost:6333/dashboard"
echo "   停止：按 Ctrl+C"
echo

QDRANT__STORAGE__STORAGE_PATH="$DATA_DIR" exec "$BIN"
