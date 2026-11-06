#!/usr/bin/env bash
# ============================================================
# Sky Personal RAG —— 一键启动全部服务
# 用法：bash start.sh
# ============================================================
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

# 本机服务直连：摘除代理软件注入的代理变量。
# 否则 curl 健康检查与 Streamlit/Qdrant 的本机请求会被送进代理而失败。
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
export NO_PROXY=localhost,127.0.0.1
export no_proxy=localhost,127.0.0.1

# 清理残留进程
pkill -9 qdrant 2>/dev/null || true
pkill -f "streamlit run" 2>/dev/null || true
pkill -f "uvicorn src.admin_app" 2>/dev/null || true
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
echo "  [1/4] 启动 Qdrant 向量数据库…"
# 部署安全：向量库只绑本机（同机部署时局域网无法直连 6333）；需要独立部署时改此变量
QDRANT_BIND_HOST="${QDRANT_BIND_HOST:-127.0.0.1}"
QDRANT__SERVICE__HOST="$QDRANT_BIND_HOST" QDRANT__STORAGE__STORAGE_PATH="$ROOT/storage/qdrant_data"   nohup "$ROOT/tools/qdrant/qdrant" > "$ROOT/storage/qdrant.log" 2>&1 &

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
echo "  [2/4] 检查 Ollama…"
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

# ---------- 启动管理后台（FastAPI，8700）----------
echo "  [3/4] 启动管理后台…"
nohup "$ROOT/.venv/bin/python" -m uvicorn src.admin_app:app \
      --host 0.0.0.0 --port 8700 --proxy-headers --forwarded-allow-ips="127.0.0.1" > "$ROOT/storage/admin.log" 2>&1 &
for i in $(seq 1 10); do
    if curl -s --max-time 2 http://localhost:8700/admin/login >/dev/null 2>&1; then
        echo "  ✅ 管理后台就绪 (http://localhost:8700/admin/login)"
        break
    fi
    if [ "$i" = "10" ]; then
        echo "  ❌ 管理后台启动超时，请检查 storage/admin.log"
        exit 1
    fi
    sleep 1
done

# ---------- 可选：MCP HTTP 传输（团队共享，PAT 鉴权，端口 8760） ----------
# 默认不启动（stdio 个人模式）。需要时：export MCP_TRANSPORT=streamable-http 后再跑 start.sh
if [ "$MCP_TRANSPORT" = "streamable-http" ]; then
    echo "  [extra] 启动 MCP HTTP 服务（PAT 鉴权，8760）…"
    nohup env MCP_TRANSPORT=streamable-http \
          "$ROOT/.venv/bin/python" -m src.mcp_server > "$ROOT/storage/mcp_http.log" 2>&1 &
    sleep 2
    echo "  ✅ MCP HTTP: http://localhost:8760/mcp"
fi

# ---------- 启动 Streamlit ----------
echo "  [4/4] 启动 Web 界面…"
echo ""
echo "  📍 知识库前台：http://localhost:8501"
echo "  📍 管理后台：http://localhost:8700/admin/login"
echo "  🛑 停止：Ctrl+C（Qdrant/管理后台继续后台运行）"
echo ""

exec streamlit run app.py --server.headless false --server.port 8501
