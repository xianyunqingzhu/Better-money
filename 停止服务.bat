@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Better-money 停止服务

if not exist ".venv\Scripts\python.exe" (
    echo 未找到运行环境，请先运行一次"启动.bat"。
    pause
    exit /b 1
)

".venv\Scripts\python.exe" windows_entry.py --request-shutdown
echo.
pause
