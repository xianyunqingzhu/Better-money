param()
$ErrorActionPreference = "Stop"
$exe = "D:\Better-money\dist\BetterMoney\BetterMoney.exe"
$smokeHome = Join-Path $env:TEMP ("bm-smoke-" + (Get-Random))
$env:BETTER_MONEY_HOME = $smokeHome
$env:BETTER_MONEY_SESSION_TOKEN = "smoke-token-" + (Get-Random)
$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
$listener.Start()
$port = $listener.LocalEndpoint.Port
$listener.Stop()
$proc = Start-Process -FilePath $exe -ArgumentList "--server","--host","127.0.0.1","--port",$port -PassThru -WindowStyle Hidden
try {
    $healthy = $false
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Seconds 1
        if ($proc.HasExited) { throw "process exited early: $($proc.ExitCode)" }
        try {
            $health = Invoke-RestMethod "http://127.0.0.1:$port/api/health" -TimeoutSec 2
            $healthy = $true
            break
        } catch { }
    }
    if (-not $healthy) { throw "not healthy" }
    Write-Host "health version: $($health.version)"
    $share = Invoke-RestMethod "http://127.0.0.1:$port/api/share/status"
    Write-Host "share pending: $($share.pending_changes)"
    $settings = Invoke-RestMethod "http://127.0.0.1:$port/api/settings"
    Write-Host "device_id set: $([bool]$settings.device_id); name: $($settings.device_name)"
    $page = Invoke-WebRequest "http://127.0.0.1:$port/" -UseBasicParsing
    Write-Host "share UI present: $($page.Content.Contains('share-status'))"
    $body = '{"date":"2026-08-23","amount":12.34,"type":"\u652f\u51fa","category":"\u9910\u996e","merchant":"smoke","note":"","source":"\u624b\u52a8"}'
    $tx = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$port/api/transactions" -ContentType "application/json" -Body $body
    Write-Host "tx ok: $($tx.ok)"
    $statusAfter = Invoke-RestMethod "http://127.0.0.1:$port/api/share/status"
    Write-Host "pending after tx: $($statusAfter.pending_changes)"
    $zip = Invoke-WebRequest "http://127.0.0.1:$port/api/share/export" -UseBasicParsing
    Write-Host "export: $($zip.StatusCode), $($zip.RawContentLength) bytes"
    $statusFinal = Invoke-RestMethod "http://127.0.0.1:$port/api/share/status"
    Write-Host "pending after export: $($statusFinal.pending_changes)"
    Write-Host "SMOKE OK"
} finally {
    if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
}
