#!/usr/bin/env bash
# 下载 Qdrant 向量数据库的 macOS 本体到项目 tools/ 目录（已 gitignore，不会进入开源仓库）
set -euo pipefail

QDRANT_VERSION="${QDRANT_VERSION:-v1.19.0}"
ARCH="$(uname -m)"
case "$ARCH" in
  arm64)  QDRANT_ARCH="aarch64" ;;
  x86_64) QDRANT_ARCH="x86_64" ;;
  *) echo "❌ 不支持的架构：$ARCH"; exit 1 ;;
esac

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOOLS_DIR="$ROOT/tools"
mkdir -p "$TOOLS_DIR"
cd "$TOOLS_DIR"

TARBALL="qdrant-${QDRANT_ARCH}-apple-darwin.tar.gz"
URL="https://github.com/qdrant/qdrant/releases/download/${QDRANT_VERSION}/${TARBALL}"

if [ -x "$TOOLS_DIR/qdrant/qdrant" ]; then
  echo "✅ Qdrant 已存在：$TOOLS_DIR/qdrant/qdrant"
  exit 0
fi

echo "⬇️  下载 $URL"
curl -fL --progress-bar "$URL" -o "$TARBALL"
mkdir -p qdrant
tar -xzf "$TARBALL" -C qdrant
rm -f "$TARBALL"
echo "✅ 下载完成：$TOOLS_DIR/qdrant/qdrant"
echo "   下一步启动：bash scripts/start_qdrant.sh"
