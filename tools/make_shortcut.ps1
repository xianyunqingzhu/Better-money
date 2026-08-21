# 为 Better-money 创建桌面 / 开始菜单快捷方式（.lnk）
# 用法：右键本文件 → "使用 PowerShell 运行"，或在 PowerShell 中：
#   powershell -ExecutionPolicy Bypass -File tools\make_shortcut.ps1
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$BatPath = Join-Path $RepoRoot "启动.bat"
$IconPath = Join-Path $RepoRoot "icon.ico"
if (-not (Test-Path $BatPath)) { throw "未找到启动.bat：$BatPath" }
if (-not (Test-Path $IconPath)) { $IconPath = "" }

$Shell = New-Object -ComObject WScript.Shell

# 桌面快捷方式
$Desktop = [Environment]::GetFolderPath("Desktop")
$DesktopLnk = $Shell.CreateShortcut((Join-Path $Desktop "Better-money.lnk"))
$DesktopLnk.TargetPath = $env:ComSpec
$DesktopLnk.Arguments = '/c ""' + $BatPath + '""'
$DesktopLnk.WorkingDirectory = $RepoRoot
$DesktopLnk.Description = "Better-money 个人记账与储蓄助手"
if ($IconPath) { $DesktopLnk.IconLocation = "$IconPath,0" }
$DesktopLnk.Save()

# 开始菜单快捷方式
$StartMenu = Join-Path ([Environment]::GetFolderPath("Programs")) "Better-money.lnk"
$MenuLnk = $Shell.CreateShortcut($StartMenu)
$MenuLnk.TargetPath = $env:ComSpec
$MenuLnk.Arguments = '/c ""' + $BatPath + '""'
$MenuLnk.WorkingDirectory = $RepoRoot
$MenuLnk.Description = "Better-money 个人记账与储蓄助手"
if ($IconPath) { $MenuLnk.IconLocation = "$IconPath,0" }
$MenuLnk.Save()

Write-Host "已创建快捷方式："
Write-Host "  桌面：$Desktop\Better-money.lnk"
Write-Host "  开始菜单：$StartMenu"
Write-Host "双击快捷方式即可启动；停止服务请点应用内「设置 → 退出服务」。"
