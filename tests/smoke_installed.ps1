# 已安装应用冒烟：验证单实例、健康身份与受控退出
# 用法：powershell -ExecutionPolicy Bypass -File tests\smoke_installed.ps1 `
#         -ExecutablePath "C:\Program Files\Better Money\BetterMoney.exe" `
#         -ApplicationHome "$env:LOCALAPPDATA\BetterMoney"
# 不删除应用数据；测试清理由验收人员另行处理。
param(
    [Parameter(Mandatory = $true)][string]$ExecutablePath,
    [Parameter(Mandatory = $true)][string]$ApplicationHome
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path $ExecutablePath)) { throw "未找到 $ExecutablePath" }
$Env:BETTER_MONEY_HOME = $ApplicationHome

function Wait-Record {
    $deadline = (Get-Date).AddSeconds(30)
    $record = Join-Path $ApplicationHome "runtime\instance.json"
    while (-not (Test-Path $record)) {
        if ((Get-Date) -gt $deadline) { throw "等待 instance.json 超时" }
        Start-Sleep -Milliseconds 200
    }
    return (Get-Content $record -Raw | ConvertFrom-Json)
}

function Invoke-Health {
    param([int]$Port)
    $resp = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 2
    if (-not $resp.ok -or $resp.app_id -ne "better-money" -or $resp.version -ne "1.0.0" -or $resp.protocol -ne 1) {
        throw "健康身份校验失败：$($resp | ConvertTo-Json -Compress)"
    }
    return $resp
}

# 1) 第一次点击：等待记录并验证身份
Start-Process -FilePath $ExecutablePath | Out-Null
$first = Wait-Record
$health = Invoke-Health -Port $first.port
Write-Host "第一次点击 OK：PID $($first.pid) 端口 $($first.port) 版本 $($health.version)"

# 2) 第二次点击：必须复用同一实例（PID 不变）
Start-Process -FilePath $ExecutablePath | Out-Null
Start-Sleep -Seconds 2
$second = Get-Content (Join-Path $ApplicationHome "runtime\instance.json") -Raw | ConvertFrom-Json
if ($second.pid -ne $first.pid) { throw "第二次点击启动了第二个实例：$($first.pid) -> $($second.pid)" }
Write-Host "单实例 OK：两次点击 PID 均为 $($first.pid)"

# 3) 受控退出
$headers = @{ "X-Better-Money-Token" = $first.token }
$resp = Invoke-RestMethod -Uri "http://127.0.0.1:$($first.port)/api/control/shutdown" -Method Post -Headers $headers -TimeoutSec 5
if (-not $resp.ok) { throw "退出请求失败" }
Start-Sleep -Seconds 2
try {
    Invoke-Health -Port $first.port | Out-Null
    throw "退出后服务仍在运行"
} catch {
    if ($_.Exception.Message -like "*服务仍在运行*") { throw }
}
Write-Host "已安装应用冒烟全部通过"
