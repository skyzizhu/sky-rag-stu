@echo off
rem Common paths for the Windows service launchers. The caller supplies ROOT.
if not defined QDRANT_EXE if exist "%ROOT%tools\qdrant\qdrant.exe" set "QDRANT_EXE=%ROOT%tools\qdrant\qdrant.exe"
rem Keep existing Windows installs working without moving their database.
if not defined QDRANT_EXE if exist "%USERPROFILE%\sky-rag-setup\qdrant\bin\qdrant.exe" set "QDRANT_EXE=%USERPROFILE%\sky-rag-setup\qdrant\bin\qdrant.exe"
if not defined QDRANT_EXE set "QDRANT_EXE=%ROOT%tools\qdrant\qdrant.exe"
for %%I in ("%QDRANT_EXE%") do (
    set "QDRANT_EXE=%%~fI"
    set "QDRANT_DIR=%%~dpI"
)
if not defined QDRANT_STORAGE_PATH if defined QDRANT__STORAGE__STORAGE_PATH set "QDRANT_STORAGE_PATH=%QDRANT__STORAGE__STORAGE_PATH%"
if not defined QDRANT_STORAGE_PATH if exist "%QDRANT_DIR%storage\" set "QDRANT_STORAGE_PATH=%QDRANT_DIR%storage"
if not defined QDRANT_STORAGE_PATH set "QDRANT_STORAGE_PATH=%ROOT%storage\qdrant_data"

if not defined OLLAMA_EXE if exist "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" set "OLLAMA_EXE=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"
if not defined OLLAMA_EXE for /f "delims=" %%I in ('where.exe ollama.exe 2^>nul') do if not defined OLLAMA_EXE set "OLLAMA_EXE=%%I"
if not defined OLLAMA_EXE set "OLLAMA_EXE=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"
if not defined LIBREOFFICE_DIR set "LIBREOFFICE_DIR=%ProgramFiles%\LibreOffice\program"
if exist "%LIBREOFFICE_DIR%\soffice.exe" set "PATH=%LIBREOFFICE_DIR%;%PATH%"

set "HTTP_PROXY="
set "HTTPS_PROXY="
set "ALL_PROXY="
set "NO_PROXY=localhost,127.0.0.1"
exit /b 0
