#!/bin/bash
# Better-money 启动器（macOS）：双击运行，启动本地服务并用默认浏览器打开。
# 关闭本窗口即停止服务（与 Windows 版体验一致）。
cd "$(dirname "$0")"

# 1) 已在运行就直接开浏览器
if lsof -nP -iTCP:8642 -sTCP:LISTEN >/dev/null 2>&1; then
    open "http://127.0.0.1:8642"
    exit 0
fi

# 2) 找一个 Python 3.10+（mac 自带 python3 可能过老）
PY=""
for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then
        v=$("$c" -c 'import sys; print(sys.version_info[0]*100+sys.version_info[1])' 2>/dev/null)
        if [ -n "$v" ] && [ "$v" -ge 310 ]; then
            PY="$c"
            break
        fi
    fi
done
if [ -z "$PY" ]; then
    echo "未找到 Python 3.10 或更高版本。"
    echo "请先安装（任选其一）："
    echo "  1) 终端运行：xcode-select --install"
    echo "  2) 安装 Homebrew（brew.sh）后运行：brew install python"
    read -p "按回车关闭窗口" _
    exit 1
fi

# 3) 首次运行：创建虚拟环境并安装依赖（清华镜像加速）
if [ ! -x ".venv/bin/python" ]; then
    echo "[首次运行] 正在创建虚拟环境并安装依赖，请稍候..."
    "$PY" -m venv .venv || {
        echo "虚拟环境创建失败，请检查 Python 安装。"
        read -p "按回车关闭窗口" _
        exit 1
    }
    .venv/bin/python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple || {
        echo "依赖安装失败，请检查网络后重试。"
        read -p "按回车关闭窗口" _
        exit 1
    }
fi

# 4) 启动服务（前台运行），稍后自动打开浏览器
echo "Better-money 已启动：http://127.0.0.1:8642"
echo "关闭本窗口即可停止服务。"
( sleep 3; open "http://127.0.0.1:8642" ) &
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8642

read -p "服务已停止，按回车关闭窗口" _
