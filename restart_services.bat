@echo off
rem Restart all Great Wall Internet (长城互联网) services: stop everything, then relaunch.
rem Triggered by the admin console "Services" tab (detached process) or manually.
rem Each service logs to its own file under storage\ (shared files would lock
rem each other out — long-running servers hold their redirect handle open).
set ROOT=%~dp0
set QDRANT_EXE=C:\Users\skyzi\sky-rag-setup\qdrant\bin\qdrant.exe
set OLLAMA_APP=C:\Users\skyzi\AppData\Local\Programs\Ollama\ollama app.exe
set LOGDIR=%ROOT%storage
set "PATH=C:\Program Files\LibreOffice\program;%PATH%"
set http_proxy=
set https_proxy=
set all_proxy=
set HTTP_PROXY=
set HTTPS_PROXY=
set ALL_PROXY=
set NO_PROXY=localhost,127.0.0.1
set no_proxy=localhost,127.0.0.1

if not exist "%LOGDIR%" mkdir "%LOGDIR%"
echo === restart begin %date% %time% === >> "%LOGDIR%\restart.log"

echo Stopping all services...
call "%~dp0stop_services.bat" >nul 2>&1
ping -n 4 127.0.0.1 >nul

rem /B = no new window: this script may run in a console-less detached process
cd /d "%QDRANT_EXE%\.."
start "" /B "%QDRANT_EXE%" >> "%LOGDIR%\restart_qdrant.log" 2>&1

start "" /B "%OLLAMA_APP%" >> "%LOGDIR%\restart_ollama.log" 2>&1
ping -n 5 127.0.0.1 >nul

cd /d "%ROOT%"
start "" /B "%ROOT%.venv\Scripts\python.exe" -m uvicorn src.admin_app:app --host 0.0.0.0 --port 8700 --proxy-headers --forwarded-allow-ips=127.0.0.1 >> "%LOGDIR%\restart_admin.log" 2>&1
start "" /B "%ROOT%.venv\Scripts\streamlit.exe" run app.py --server.headless true >> "%LOGDIR%\restart_front.log" 2>&1
set "MCP_TRANSPORT=streamable-http"
start "" /B "%ROOT%.venv\Scripts\python.exe" "%ROOT%src\mcp_server.py" >> "%LOGDIR%\restart_mcp.log" 2>&1

echo === restart done %date% %time% === >> "%LOGDIR%\restart.log"
