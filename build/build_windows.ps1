# 构建 Windows onedir 应用包：dist\BetterMoney\BetterMoney.exe
# 用法（在项目根目录）：
#   powershell -ExecutionPolicy Bypass -File build\build_windows.ps1 [-InstallDependencies]
param(
    [switch]$InstallDependencies
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$SpecPath = Join-Path $PSScriptRoot "better-money.spec"
$DistPath = Join-Path $RepoRoot "dist"
$WorkPath = Join-Path $RepoRoot "build-output\pyinstaller"
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    # CI / 干净环境：没有 .venv 时退回 PATH 上的 Python
    $VenvPython = (Get-Command python -ErrorAction Stop).Source
}

# 只支持 64 位 Windows
if (-not [Environment]::Is64BitOperatingSystem) {
    throw "Better-money 1.0.0 只支持 64 位 Windows"
}
if ($env:OS -ne "Windows_NT") {
    throw "必须在 Windows 上构建 Windows 应用包"
}
if (-not (Test-Path $VenvPython)) {
    throw "未找到 .venv\Scripts\python.exe，请先运行 启动.bat 创建运行环境"
}

if ($InstallDependencies) {
    & $VenvPython -m pip install "pyinstaller>=6.0,<7"
    if ($LASTEXITCODE -ne 0) { throw "安装 PyInstaller 失败" }
}

# 只删除确认为仓库内解析后的构建目标，绝不动其他路径
$resolvedRoot = (Resolve-Path $RepoRoot).Path
$resolvedDist = (Resolve-Path $DistPath -ErrorAction SilentlyContinue)
$resolvedWork = (Resolve-Path $WorkPath -ErrorAction SilentlyContinue)
if ($resolvedDist -and $resolvedDist.Path.StartsWith($resolvedRoot)) {
    Remove-Item -Recurse -Force (Join-Path $DistPath "BetterMoney") -ErrorAction SilentlyContinue
} else {
    New-Item -ItemType Directory -Force -Path $DistPath | Out-Null
}
if ($resolvedWork -and $resolvedWork.Path.StartsWith($resolvedRoot)) {
    Remove-Item -Recurse -Force $WorkPath -ErrorAction SilentlyContinue
}

Push-Location $RepoRoot
try {
    & $VenvPython -m PyInstaller --noconfirm --clean --distpath $DistPath --workpath $WorkPath $SpecPath
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败" }
} finally {
    Pop-Location
}

$ExePath = Join-Path $DistPath "BetterMoney\BetterMoney.exe"
if (-not (Test-Path $ExePath)) { throw "构建完成但找不到 $ExePath" }
Write-Host ""
Write-Host "构建成功：$ExePath"
Write-Host "应用数据目录：%LOCALAPPDATA%\BetterMoney（不含 Python，双击即用）"
