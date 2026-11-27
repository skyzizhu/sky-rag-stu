@echo off
setlocal
rem Control one service: service_ctl.bat <front|admin|qdrant|ollama|mcp> <start|stop>
rem Each server uses its own log; control output never shares a server handle.
set "SVC=%~1"
set "ACTION=%~2"
set "ROOT=%~dp0"
call "%ROOT%scripts\windows_service_config.bat" || exit /b 1

if not "%ACTION%"=="start" if not "%ACTION%"=="stop" exit /b 1
set "PORT="
if "%SVC%"=="front" set "PORT=8501"
if "%SVC%"=="admin" set "PORT=8700"
if "%SVC%"=="qdrant" set "PORT=6333"
if "%SVC%"=="ollama" set "PORT=11434"
if "%SVC%"=="mcp" set "PORT=8760"
if not defined PORT exit /b 1

set "LOGDIR=%ROOT%storage"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set "LOG=%LOGDIR%\svc_%SVC%_control.log"
echo === %SVC% %ACTION% %date% %time% === >> "%LOG%"

if "%ACTION%"=="stop" goto stop
rem Do not replace an existing listener. Qdrant must not already be exposed on the network.
if "%SVC%"=="qdrant" call :check_qdrant_listeners || exit /b 1
netstat -aon | findstr /R /C:":%PORT% .*LISTENING" >nul
if not errorlevel 1 (
    echo Service port %PORT% is already listening; no duplicate process started. >> "%LOG%"
    exit /b 0
)

set "SERVICE_EXE=%ROOT%.venv\Scripts\python.exe"
if "%SVC%"=="qdrant" set "SERVICE_EXE=%QDRANT_EXE%"
if "%SVC%"=="ollama" set "SERVICE_EXE=%OLLAMA_EXE%"
if not exist "%SERVICE_EXE%" (
    echo ERROR: service executable not found: %SERVICE_EXE% >> "%LOG%"
    echo ERROR: service executable not found: %SERVICE_EXE% 1>&2
    exit /b 1
)
if "%SVC%"=="mcp" if not exist "%ROOT%src\mcp_server.py" (
    echo ERROR: MCP entry script not found. >> "%LOG%"
    exit /b 1
)
set "SERVICE_LOG=%LOGDIR%\svc_%SVC%.log"
rem Check redirection before START; failures are also captured by the API wrapper log.
type nul >> "%SERVICE_LOG%" 2>nul
if errorlevel 1 (
    echo ERROR: service log is not writable: %SERVICE_LOG% >> "%LOG%"
    echo ERROR: service log is not writable: %SERVICE_LOG% 1>&2
    exit /b 1
)

if "%SVC%"=="front" goto front
if "%SVC%"=="admin" goto admin
if "%SVC%"=="qdrant" goto qdrant
if "%SVC%"=="ollama" goto ollama
if "%SVC%"=="mcp" goto mcp
exit /b 1

:front
cd /d "%ROOT%"
set "PYTHONIOENCODING=utf-8"
start "" /B "%ROOT%.venv\Scripts\python.exe" -m streamlit run app.py --server.headless true --server.port 8501 >> "%LOGDIR%\svc_front.log" 2>&1
if errorlevel 1 goto launch_failed
echo START accepted: front; readiness is checked by the admin page. >> "%LOG%"
exit /b 0

:admin
cd /d "%ROOT%"
start "" /B "%ROOT%.venv\Scripts\python.exe" -m uvicorn src.admin_app:app --host 0.0.0.0 --port 8700 --proxy-headers --forwarded-allow-ips=127.0.0.1 >> "%LOGDIR%\svc_admin.log" 2>&1
if errorlevel 1 goto launch_failed
echo START accepted: admin; readiness is checked by the admin page. >> "%LOG%"
exit /b 0

:qdrant
rem Keep the executable's working directory and point explicitly at the selected database.
pushd "%QDRANT_DIR%" || goto launch_failed
set "QDRANT__SERVICE__HOST=127.0.0.1"
set "QDRANT__STORAGE__STORAGE_PATH=%QDRANT_STORAGE_PATH%"
echo Qdrant data: "%QDRANT_STORAGE_PATH%" >> "%LOG%"
start "" /B "%QDRANT_EXE%" >> "%LOGDIR%\svc_qdrant.log" 2>&1
set "QDRANT_START_RC=%errorlevel%"
popd
if not "%QDRANT_START_RC%"=="0" goto launch_failed
echo START accepted: qdrant; readiness is checked by the admin page. >> "%LOG%"
exit /b 0

:ollama
rem A desktop instance may already be running; this path starts a local server when needed.
set "OLLAMA_HOST=127.0.0.1:11434"
start "" /B "%OLLAMA_EXE%" serve >> "%LOGDIR%\svc_ollama.log" 2>&1
if errorlevel 1 goto launch_failed
echo START accepted: ollama; readiness is checked by the admin page. >> "%LOG%"
exit /b 0

:mcp
rem MCP 知识服务（streamable-http + PAT 鉴权）；MCP_MODE 由 src\mcp_server.py 从 .env 读取
cd /d "%ROOT%"
set "MCP_TRANSPORT=streamable-http"
set "PYTHONIOENCODING=utf-8"
start "" /B "%ROOT%.venv\Scripts\python.exe" "%ROOT%src\mcp_server.py" >> "%LOGDIR%\svc_mcp.log" 2>&1
if errorlevel 1 goto launch_failed
echo START accepted: mcp; readiness is checked by the admin page. >> "%LOG%"
exit /b 0

:launch_failed
echo ERROR: START failed for service %SVC%. See %SERVICE_LOG%. >> "%LOG%"
echo ERROR: START failed for service %SVC%. See %SERVICE_LOG%. 1>&2
exit /b 1

:check_qdrant_listeners
for /f "tokens=2" %%A in ('netstat -aon ^| findstr /R /C:":6333 .*LISTENING" /C:":6334 .*LISTENING"') do (
    if not "%%A"=="127.0.0.1:6333" if not "%%A"=="127.0.0.1:6334" if not "%%A"=="[::1]:6333" if not "%%A"=="[::1]:6334" (
        echo ERROR: %%A is not loopback. Review the existing listener; no process was stopped. >> "%LOG%"
        echo ERROR: %%A is not loopback. See "%LOG%". 1>&2
        exit /b 1
    )
)
exit /b 0

:stop
rem Allow the HTTP response to reach the browser before stopping the admin API.
if "%SVC%"=="admin" ping -n 3 127.0.0.1 >nul
if "%SVC%"=="ollama" taskkill /F /T /IM "ollama app.exe" >> "%LOG%" 2>&1
rem Match LISTENING rows as one regex, not client connections or every python.exe.
for /f "tokens=5" %%a in ('netstat -aon ^| findstr /R /C:":%PORT% .*LISTENING"') do (
    if not "%%a"=="0" taskkill /F /T /PID %%a >> "%LOG%" 2>&1
)
exit /b 0
