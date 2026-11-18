#!/bin/bash
# 控制单个服务：service_ctl.sh <front|admin|qdrant|ollama> <start|stop>
# Mac 版（与 service_ctl.bat 等价）：每服务独立日志；启动前检测端口防重复实例。
set -u
SVC="${1:-}"
ACTION="${2:-}"
ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV_PY="$ROOT/.venv/bin/python"
LOGDIR="$ROOT/storage"
LOG="$LOGDIR/svc_${SVC}_control.log"
QDRANT_EXE="${QDRANT_EXE:-$ROOT/tools/qdrant/qdrant}"

# 清掉代理变量：本机服务（Qdrant/Ollama）直连，不允许走宿主代理
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
export NO_PROXY=localhost,127.0.0.1
export no_proxy=localhost,127.0.0.1

if [ "$ACTION" != "start" ] && [ "$ACTION" != "stop" ]; then
    echo "用法：service_ctl.sh <front|admin|qdrant|ollama> <start|stop>"
    exit 1
fi
case "$SVC" in
    front)  PORT=8501 ;;
    admin)  PORT=8700 ;;
    qdrant) PORT=6333 ;;
    ollama) PORT=11434 ;;
    *) exit 1 ;;
esac

mkdir -p "$LOGDIR"
echo "=== $SVC $ACTION $(date '+%F %T') ===" >> "$LOG"

if [ "$ACTION" = "stop" ]; then
    # admin 自停前延迟：让 HTTP 响应先送达浏览器
    if [ "$SVC" = "admin" ]; then sleep 2; fi
    if [ "$SVC" = "ollama" ]; then
        pkill -f "ollama run" 2>/dev/null
        pkill -x ollama 2>/dev/null
    fi
    # 按监听端口精确清理（只杀 LISTEN 进程，不误伤普通连接）
    pids=$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null)
    [ -n "$pids" ] && kill $pids 2>/dev/null
    exit 0
fi

# start：端口已被监听则不重复实例化
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    exit 0
fi

case "$SVC" in
    front)
        cd "$ROOT"
        nohup "$VENV_PY" -m streamlit run app.py --server.headless true --server.port 8501 \
            >> "$LOGDIR/svc_front.log" 2>&1 &
        ;;
    admin)
        cd "$ROOT"
        nohup "$VENV_PY" -m uvicorn src.admin_app:app --host 0.0.0.0 --port 8700 \
            --proxy-headers --forwarded-allow-ips=127.0.0.1 \
            >> "$LOGDIR/svc_admin.log" 2>&1 &
        ;;
    qdrant)
        # 保持 Qdrant 原有工作目录（storage 相对路径依赖它）
        cd "$(dirname "$QDRANT_EXE")"
        QDRANT_BIND_HOST=127.0.0.1 \
        QDRANT__SERVICE__HOST=127.0.0.1 \
        QDRANT__STORAGE__STORAGE_PATH="$ROOT/storage/qdrant_data" \
        nohup "$QDRANT_EXE" >> "$LOGDIR/svc_qdrant.log" 2>&1 &
        ;;
    ollama)
        nohup ollama serve >> "$LOGDIR/svc_ollama.log" 2>&1 &
        ;;
esac
exit 0
