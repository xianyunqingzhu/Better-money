# 构建安装器：release\BetterMoney-Setup-<版本>.exe
# 前置：先运行 build\build_windows.ps1 生成 dist\BetterMoney；本机安装 Inno Setup 6
# 用法：powershell -ExecutionPolicy Bypass -File build\build_installer.ps1 [-IsccPath "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"]
param(
    [string]$IsccPath = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$BundleExe = Join-Path $RepoRoot "dist\BetterMoney\BetterMoney.exe"
$IssPath = Join-Path $RepoRoot "installer\BetterMoney.iss"
$ReleaseDir = Join-Path $RepoRoot "release"

if (-not (Test-Path $BundleExe)) {
    throw "未找到 $BundleExe，请先运行 build\build_windows.ps1"
}

# 读取 app/version.py 的 APP_VERSION
$VersionPy = Join-Path $RepoRoot "app\version.py"
$VersionLine = (Get-Content $VersionPy | Where-Object { $_ -match '^APP_VERSION' } | Select-Object -First 1)
if ($VersionLine -match '"([^"]+)"') {
    $AppVersion = $Matches[1]
} else {
    throw "无法从 app\version.py 读取 APP_VERSION"
}
Write-Host "AppVersion = $AppVersion"

if (-not $IsccPath) {
    $candidates = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "${env:LOCALAPPDATA}\Programs\Inno Setup 6\ISCC.exe"
    )
    $IsccPath = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $IsccPath -or -not (Test-Path $IsccPath)) {
    throw "未找到 Inno Setup 6（ISCC.exe）。请安装 https://jrsoftware.org/isinfo.php 或通过 -IsccPath 指定路径。"
}

New-Item -ItemType Directory -Force -Path $ReleaseDir | Out-Null
& $IsccPath "/DAppVersion=$AppVersion" $IssPath
if ($LASTEXITCODE -ne 0) { throw "Inno Setup 编译失败" }

$Expected = Join-Path $ReleaseDir "BetterMoney-Setup-$AppVersion.exe"
if (-not (Test-Path $Expected)) {
    throw "编译完成但找不到预期文件 $Expected"
}
$Hash = (Get-FileHash $Expected -Algorithm SHA256).Hash.ToLower()
Write-Host ""
Write-Host "安装器：$Expected"
Write-Host "SHA-256: $Hash"
