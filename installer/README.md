# Better-money Windows 安装器

本目录的 `BetterMoney.iss` 用 [Inno Setup 6](https://jrsoftware.org/isinfo.php) 把
`dist\BetterMoney`（PyInstaller onedir 包）打成 **双击安装、装完即用** 的
`BetterMoney-Setup-<版本>.exe`。

## 构建顺序

1. 确保 64 位 Windows，且仓库 `.venv` 已存在：
   `powershell -ExecutionPolicy Bypass -File build\build_windows.ps1`
   （首次需 `-InstallDependencies` 安装 PyInstaller）
2. 本机安装 Inno Setup 6（外部前置，未安装时构建脚本会明确报错并给出官网地址）
3. `powershell -ExecutionPolicy Bypass -File build\build_installer.ps1`
   - 自动从 `app\version.py` 读取版本号
   - 产物：`release\BetterMoney-Setup-<版本>.exe`，并打印 SHA-256

## 安装器行为

- 默认安装到 `Program Files\Better Money`，需要一次管理员授权（应用本体之后以
  普通用户运行，数据写在 `%LOCALAPPDATA%\BetterMoney`）
- 桌面 / 开始菜单快捷方式指向 `BetterMoney.exe`（启动器：健康检查通过才开浏览器、
  单实例、端口占用自动换端口）
- 安装/卸载前通过 `--request-shutdown` 先停止正在运行的实例
- **卸载默认保留个人数据**；卸载页的「同时删除我的账单、设置、图片和备份」
  复选框勾选后还需二次确认，才会删除 `%LOCALAPPDATA%\BetterMoney`

## 已知限制

- 未做代码签名：首次运行可能出现 SmartScreen「Windows 已保护你的电脑」，
  需点「更多信息 → 仍要运行」
- 不包含自动更新、托盘、云同步；升级用新版安装器覆盖安装即可（数据保留）
