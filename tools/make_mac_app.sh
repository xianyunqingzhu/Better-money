#!/bin/bash
# 生成 Better-money.app（macOS 专用，放在项目文件夹内）
# 用法：在 mac 上项目目录里运行  bash tools/make_mac_app.sh
# 生成后：右键 .app → 制作替身，把替身拖到桌面/Dock（.app 本体不要移出项目文件夹）
set -e
cd "$(dirname "$0")/.."
APP="Better-money.app"

mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

if [ -f icon.icns ]; then
    cp icon.icns "$APP/Contents/Resources/AppIcon.icns"
else
    echo "缺少 icon.icns：请先在项目目录运行 .venv/bin/python tools/make_icon.py"
    exit 1
fi

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Better-money</string>
  <key>CFBundleDisplayName</key><string>Better-money 记账</string>
  <key>CFBundleIdentifier</key><string>local.bettermoney.launcher</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleExecutable</key><string>launcher</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSMinimumSystemVersion</key><string>10.13</string>
</dict>
</plist>
PLIST

cat > "$APP/Contents/MacOS/launcher" <<'SCRIPT'
#!/bin/bash
# 项目目录 = .app 所在文件夹的上一级（.app 必须放在项目文件夹里）
PROJ="$(cd "$(dirname "$0")/../../.." && pwd)"
# 无终端环境运行时必须重定向 stdout（uvicorn 无 stdout 会崩溃）
exec "$PROJ/启动.command" < /dev/null > "$PROJ/data/server.log" 2>&1
SCRIPT

chmod +x "$APP/Contents/MacOS/launcher"
echo "已生成 $APP（保持它放在项目文件夹内；右键→制作替身，把替身拖到桌面或 Dock）"
