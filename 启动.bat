@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Better-money 启动器

where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo  [错误] 未找到 Python。
    echo  请先到 https://www.python.org/downloads/ 下载安装 Python 3.10 以上版本，
    echo  安装时务必勾选 "Add Python to PATH"，装完再双击本文件。
    echo.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo  [首次运行] 正在创建运行环境并安装依赖（需要联网，约 1~3 分钟），请稍候...
    echo.
    python -m venv .venv
    if errorlevel 1 (
        echo  [错误] 创建运行环境失败。
        pause
        exit /b 1
    )
    call ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo  [错误] 依赖安装失败，请检查网络后重新双击本文件。
        pause
        exit /b 1
    )
)

echo  正在启动 Better-money，服务就绪后会自动打开浏览器...
".venv\Scripts\python.exe" windows_entry.py
if errorlevel 1 (
    echo.
    echo  启动失败，详情见 data\logs\startup.log（或 logs\startup.log）。
    echo  如果反复失败，可先双击"停止服务.bat"再重试。
    echo.
    pause
)
