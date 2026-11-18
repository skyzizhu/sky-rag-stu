@echo off
setlocal
rem Control one service: service_ctl.bat <front|admin|qdrant|ollama> <start|stop>
rem Each server uses its own log; control output never shares a server handle.
set "SVC=%~1"
set "ACTION=%~2"
set "ROOT=%~dp0"
set "QDRANT_EXE=C:\Users\skyzi\sky-rag-setup\qdrant\bin\qdrant.exe"
set "OLLAMA_APP=%LOCALAPPDATA%\Programs\Ollama\ollama app.exe"
set "PATH=C:\Program Files\LibreOffice\program;%PATH%"
set HTTP_PROXY=
set HTTPS_PROXY=
set ALL_PROXY=
set "NO_PROXY=localhost,127.0.0.1"

if not "%ACTION%"=="start" if not "%ACTION%"=="stop" exit /b 1
set "PORT="
if "%SVC%"=="front" set "PORT=8501"
if "%SVC%"=="admin" set "PORT=8700"
if "%SVC%"=="qdrant" set "PORT=6333"
if "%SVC%"=="ollama" set "PORT=11434"
if not defined PORT exit /b 1

set "LOGDIR=%ROOT%storage"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set "LOG=%LOGDIR%\svc_%SVC%_control.log"
echo === %SVC% %ACTION% %date% %time% === >> "%LOG%"

if "%ACTION%"=="stop" goto stop
rem Do not create a duplicate instance when this port is already listening.
netstat -aon | findstr /R /C:":%PORT% .*LISTENING" >nul
if not errorlevel 1 exit /b 0

if "%SVC%"=="front" goto front
if "%SVC%"=="admin" goto admin
if "%SVC%"=="qdrant" goto qdrant
if "%SVC%"=="ollama" goto ollama
exit /b 1

:front
cd /d "%ROOT%"
start "" /B "%ROOT%.venv\Scripts\python.exe" -m streamlit run app.py --server.headless true --server.port 8501 >> "%LOGDIR%\svc_front.log" 2>&1
exit /b 0

:admin
cd /d "%ROOT%"
start "" /B "%ROOT%.venv\Scripts\python.exe" -m uvicorn src.admin_app:app --host 0.0.0.0 --port 8700 --proxy-headers --forwarded-allow-ips=127.0.0.1 >> "%LOGDIR%\svc_admin.log" 2>&1
exit /b 0

:qdrant
rem Preserve the existing Qdrant working directory and its storage folder.
for %%i in ("%QDRANT_EXE%") do cd /d "%%~dpi"
start "" /B "%QDRANT_EXE%" >> "%LOGDIR%\svc_qdrant.log" 2>&1
exit /b 0

:ollama
start "" /B "%OLLAMA_APP%" >> "%LOGDIR%\svc_ollama.log" 2>&1
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
