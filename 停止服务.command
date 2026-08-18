#!/bin/bash
# Better-money 停止服务（macOS）
cd "$(dirname "$0")"
PIDS=$(lsof -nP -tiTCP:8642 -sTCP:LISTEN 2>/dev/null)
if [ -n "$PIDS" ]; then
    kill $PIDS
    echo "已停止 Better-money 服务。"
else
    echo "服务本来就没有在运行。"
fi
read -p "按回车关闭窗口" _
