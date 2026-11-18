#!/bin/bash
# 重启全部服务：先全部停止，再按序拉起（Qdrant → Ollama → 管理后台 → 前台）。Mac 版。
set -u
ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV_PY="$ROOT/.venv/bin/python"
LOGDIR="$ROOT/storage"
QDRANT_EXE="${QDRANT_EXE:-$ROOT/tools/qdrant/qdrant}"

# 清掉代理变量：本机服务直连，不允许走宿主代理
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
export NO_PROXY=localhost,127.0.0.1
export no_proxy=localhost,127.0.0.1

mkdir -p "$LOGDIR"
echo "=== restart begin $(date '+%F %T') ===" >> "$LOGDIR/restart.log"

echo "Stopping all services..."
bash "$ROOT/stop_services.sh" >> "$LOGDIR/restart.log" 2>&1
sleep 3

# ① Qdrant（保持工作目录与 storage 位置）
cd "$(dirname "$QDRANT_EXE")"
QDRANT_BIND_HOST=127.0.0.1 \
QDRANT__SERVICE__HOST=127.0.0.1 \
QDRANT__STORAGE__STORAGE_PATH="$ROOT/storage/qdrant_data" \
nohup "$QDRANT_EXE" >> "$LOGDIR/restart_qdrant.log" 2>&1 &

# ② Ollama
nohup ollama serve >> "$LOGDIR/restart_ollama.log" 2>&1 &
sleep 4

# ③ 管理后台
cd "$ROOT"
nohup "$VENV_PY" -m uvicorn src.admin_app:app --host 0.0.0.0 --port 8700 \
    --proxy-headers --forwarded-allow-ips=127.0.0.1 \
    >> "$LOGDIR/restart_admin.log" 2>&1 &

# ④ 前台
nohup "$VENV_PY" -m streamlit run app.py --server.headless true --server.port 8501 \
    >> "$LOGDIR/restart_front.log" 2>&1 &

echo "=== restart done $(date '+%F %T') ===" >> "$LOGDIR/restart.log"
