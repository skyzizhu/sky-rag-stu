#!/bin/bash
# 停止全部服务：Qdrant + Ollama + 管理后台 + 前台（按端口精确清理，Mac 版）
set -u
ROOT="$(cd "$(dirname "$0")" && pwd)"
LOGDIR="$ROOT/storage"
LOG="$LOGDIR/stop_services.log"

# 清掉代理变量
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY

mkdir -p "$LOGDIR"
echo "=== stop all $(date '+%F %T') ===" >> "$LOG"

# Ollama：进程名（ollama serve / ollama app）
pkill -f "ollama run" 2>/dev/null
pkill -x ollama 2>/dev/null
pkill -f "ollama app" 2>/dev/null

# Qdrant：进程名精确匹配
pkill -x qdrant 2>/dev/null

# 前台 + 管理后台：按监听端口精确清理（只杀 LISTEN 进程）
for port in 8760 8700 8501; do
    pids=$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null)
    if [ -n "$pids" ]; then
        echo "  port $port: killed pids $pids" >> "$LOG"
        kill $pids 2>/dev/null
    fi
done

echo "All services stopped."
