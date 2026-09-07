#!/usr/bin/env bash
# ============================================================
# Sky Personal RAG —— 一键启动全部服务
# 用法：bash start.sh
# ============================================================
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

# 清理残留进程
pkill -9 qdrant 2>/dev/null || true
pkill -f "streamlit run" 2>/dev/null || true
sleep 2

# 激活虚拟环境
if [ ! -d "$ROOT/.venv" ]; then
    echo "❌ .venv 不存在，请先运行：python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi
source "$ROOT/.venv/bin/activate"

echo ""
echo "🧠 Sky Personal RAG · 启动中"
echo "────────────────────────────────"

# ---------- 启动 Qdrant ----------
echo "  [1/3] 启动 Qdrant 向量数据库…"
QDRANT__STORAGE__STORAGE_PATH="$ROOT/storage/qdrant_data"   nohup "$ROOT/tools/qdrant/qdrant" > "$ROOT/storage/qdrant.log" 2>&1 &

# 等待 Qdrant 就绪（最多 15 秒）
for i in $(seq 1 15); do
    if curl -s --max-time 2 http://localhost:6333/collections >/dev/null 2>&1; then
        echo "  ✅ Qdrant 就绪 (${i}s)"
        break
    fi
    if [ "$i" = "15" ]; then
        echo "  ❌ Qdrant 启动超时，请检查 storage/qdrant.log"
        exit 1
    fi
    sleep 1
done

# ---------- 检查 Ollama ----------
echo "  [2/3] 检查 Ollama…"
if curl -s --max-time 3 http://localhost:11434/api/tags >/dev/null 2>&1; then
    echo "  ✅ Ollama 运行中"
else
    echo "  ⚠️  Ollama 未运行，尝试启动…"
    nohup ollama serve > /dev/null 2>&1 &
    sleep 3
    if curl -s --max-time 3 http://localhost:11434/api/tags >/dev/null 2>&1; then
        echo "  ✅ Ollama 已启动"
    else
        echo "  ❌ Ollama 启动失败，请手动运行：ollama serve"
    fi
fi

# ---------- 启动 Streamlit ----------
echo "  [3/3] 启动 Web 界面…"
echo ""
echo "  📍 地址：http://localhost:8501"
echo "  🛑 停止：Ctrl+C（Qdrant 继续后台运行）"
echo ""

exec streamlit run app.py --server.headless false --server.port 8501
