@echo off
chcp 65001 >nul
title Sky Personal RAG - 停止服务

REM 关闭全部服务：Qdrant + Ollama + 管理后台 + 前台（按端口精确清理残留）
taskkill /F /IM qdrant.exe >nul 2>&1
taskkill /F /IM "ollama app.exe" >nul 2>&1
taskkill /F /IM ollama.exe >nul 2>&1
for %%p in (8700 8501) do (
    for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":%%p .*LISTENING" 2^>nul') do taskkill /F /PID %%a >nul 2>&1
)
echo 所有服务已停止。
