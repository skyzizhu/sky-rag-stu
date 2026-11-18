@echo off
chcp 65001 >nul
title Sky Personal RAG - 重启服务

REM 一键重启全部服务：先停止（复用 stop_services.bat），再按 start.bat 的方式拉起。
REM 由前端"服务状态与控制"页触发，以分离进程运行——本窗口关闭不影响服务。
set ROOT=%~dp0
set QDRANT_EXE=C:\Users\skyzi\sky-rag-setup\qdrant\bin\qdrant.exe
set OLLAMA_APP=C:\Users\skyzi\AppData\Local\Programs\Ollama\ollama app.exe
set "PATH=C:\Program Files\LibreOffice\program;%PATH%"
set http_proxy=
set https_proxy=
set all_proxy=
set HTTP_PROXY=
set HTTPS_PROXY=
set ALL_PROXY=
set NO_PROXY=localhost,127.0.0.1
set no_proxy=localhost,127.0.0.1

echo 重启中：先停止全部服务…
call "%~dp0stop_services.bat" >nul 2>&1
ping -n 4 127.0.0.1 >nul

start "Qdrant (6333)" "%QDRANT_EXE%"
start "Ollama" "%OLLAMA_APP%"
ping -n 5 127.0.0.1 >nul

cd /d "%ROOT%"
start "管理后台 (8700)" ".venv\Scripts\python.exe" -m uvicorn src.admin_app:app --host 0.0.0.0 --port 8700 --proxy-headers --forwarded-allow-ips=127.0.0.1
start "知识库前台 (8501)" ".venv\Scripts\streamlit.exe" run app.py

echo 全部服务已重启。
