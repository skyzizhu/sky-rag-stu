@echo off
setlocal
rem Windows interactive launcher. Set RAG_ENABLE_MCP=1 or pass --with-mcp to include MCP.
set "ROOT=%~dp0"
if /i "%~1"=="--with-mcp" set "RAG_ENABLE_MCP=1"
call "%ROOT%scripts\windows_service_config.bat" || exit /b 1
if not exist "%ROOT%.venv\Scripts\python.exe" (
    echo ERROR: Missing .venv. Follow the Windows setup in DEPLOY.md. 1>&2
    exit /b 1
)

call :start_and_wait qdrant http://127.0.0.1:6333/collections || exit /b 1
call "%ROOT%service_ctl.bat" qdrant start || exit /b 1
call :start_and_wait ollama http://127.0.0.1:11434/api/tags || exit /b 1
call :start_and_wait admin http://127.0.0.1:8700/admin/login || exit /b 1
call :start_and_wait front http://127.0.0.1:8501/_stcore/health || exit /b 1
if "%RAG_ENABLE_MCP%"=="1" (
    call :start_and_wait mcp http://127.0.0.1:8760/mcp || exit /b 1
)
echo Frontend: http://127.0.0.1:8501
echo Admin: http://127.0.0.1:8700/admin/login
exit /b 0

:start_and_wait
call "%ROOT%service_ctl.bat" %~1 start || exit /b 1
for /l %%I in (1,1,20) do (
    call :probe "%~1" "%~2" && (
        echo READY: %~1
        exit /b 0
    )
    ping -n 2 127.0.0.1 >nul
)
echo ERROR: %~1 did not become ready. See "%ROOT%storage\svc_%~1.log". 1>&2
exit /b 1

:probe
if "%~1"=="mcp" goto probe_mcp
curl.exe --noproxy "*" --fail --silent --max-time 2 -o NUL "%~2"
exit /b %errorlevel%

:probe_mcp
rem A 401 from the PAT middleware is readiness, not an authenticated tool check.
for /f %%S in ('curl.exe --noproxy "*" --silent --max-time 2 -o NUL -w "%%{http_code}" "%~2"') do if "%%S"=="401" exit /b 0
exit /b 1
