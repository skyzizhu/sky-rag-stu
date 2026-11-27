@echo off
setlocal
rem Used manually and by the detached admin service control process.
set "ROOT=%~dp0"
if /i "%~1"=="--with-mcp" set "RAG_ENABLE_MCP=1"
rem Keep MCP enabled on restart if it was already running, including manual starts.
netstat -aon | findstr /R /C:":8760 .*LISTENING" >nul && set "RAG_ENABLE_MCP=1"
if not exist "%ROOT%.venv\Scripts\python.exe" (
    echo ERROR: Missing .venv. Follow the Windows setup in DEPLOY.md. 1>&2
    exit /b 1
)
call "%ROOT%stop_services.bat" || exit /b 1
ping -n 4 127.0.0.1 >nul
call "%ROOT%start.bat"
exit /b %errorlevel%
