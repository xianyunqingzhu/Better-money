@echo off
chcp 65001 >nul
echo 正在停止 Better-money 服务...
set FOUND=0
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8642 ^| findstr LISTENING') do (
    set FOUND=1
    echo 停止进程 PID %%a
    taskkill /f /pid %%a >nul 2>nul
)
if "%FOUND%"=="0" (
    echo 服务本来就没有在运行。
) else (
    echo 已停止。
)
pause
