@echo off
chcp 65001 >nul
title Sky Personal RAG 启动器

REM ============================================================
REM  Windows 一键启动：Qdrant + Ollama 检查 + 管理后台 + 知识库前台
REM  （start.sh 是 Mac/Linux 版；本文件是其 Windows 等价实现）
REM  用法：双击本文件。停止服务：关闭弹出的三个服务窗口即可。
REM ============================================================

set ROOT=%~dp0
set QDRANT_DIR=C:\Users\skyzi\sky-rag-setup\qdrant\bin
set OLLAMA_APP=C:\Users\skyzi\AppData\Local\Programs\Ollama\ollama app.exe
REM LibreOffice：老格式（doc/xls/ppt/wps/et/dps）解析依赖 soffice.exe
set "PATH=C:\Program Files\LibreOffice\program;%PATH%"

REM 摘除代理变量：本机服务必须直连，防止代理软件劫持 localhost 请求
set http_proxy=
set https_proxy=
set all_proxy=
set HTTP_PROXY=
set HTTPS_PROXY=
set ALL_PROXY=
set NO_PROXY=localhost,127.0.0.1
set no_proxy=localhost,127.0.0.1

echo [0/4] 清理残留服务（占用 6333/8700/8501 端口的旧进程）...
for %%p in (8760 6333 8700 8501) do (
    for /f "tokens=5" %%a in ('netstat -aon ^| findstr /R /C:":%%p .*LISTENING" 2^>nul') do taskkill /F /T /PID %%a >nul 2>&1
)
taskkill /F /IM qdrant.exe >nul 2>&1

echo [1/4] 启动 Qdrant 向量数据库...
cd /d "%QDRANT_DIR%"
start "Qdrant (6333)" qdrant.exe

echo [2/4] 检查 Ollama（未运行则启动）...
curl.exe -s --max-time 3 http://localhost:11434/api/tags >nul 2>&1
if errorlevel 1 (
    start "Ollama" "%OLLAMA_APP%"
    timeout /t 4 /nobreak >nul
)

echo [3/4] 启动管理后台（认证服务）...
cd /d "%ROOT%"
start "管理后台 (8700)" ".venv\Scripts\python.exe" -m uvicorn src.admin_app:app --host 0.0.0.0 --port 8700 --proxy-headers --forwarded-allow-ips=127.0.0.1

echo [4/5] 启动知识库前台...
start "知识库前台 (8501)" ".venv\Scripts\streamlit.exe" run app.py

echo [5/5] 启动 MCP 知识服务（PAT 鉴权）...
start "MCP (8760)" cmd /c "set MCP_TRANSPORT=streamable-http && .venv\Scripts\python.exe src\mcp_server.py"

echo 等待服务就绪...
set /a waited=0
:waitloop
timeout /t 2 /nobreak >nul
curl.exe -s --max-time 2 http://localhost:8501 >nul 2>&1
if errorlevel 1 (
    set /a waited+=1
    if %waited% lss 20 goto waitloop
)

echo.
echo ============================================================
echo   知识库前台：  http://localhost:8501
echo   管理后台：    http://localhost:8700/admin/login
echo   停止服务：    关闭弹出的服务窗口（Qdrant/8700/8501/8760）
echo ============================================================
echo.
start "" http://localhost:8501
pause
