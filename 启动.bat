@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.10 以上版本，并勾选 Add to PATH。
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [首次运行] 正在创建虚拟环境并安装依赖，请稍候...
    python -m venv .venv
    call ".venv\Scripts\activate.bat"
    python -m pip install -r requirements.txt
) else (
    call ".venv\Scripts\activate.bat"
)

echo.
echo  Better-money 已启动：http://127.0.0.1:8642
echo  关闭本窗口即可停止服务。
echo.
ping -n 3 127.0.0.1 >nul
start "" http://127.0.0.1:8642
python -m uvicorn app.main:app --host 127.0.0.1 --port 8642
pause
