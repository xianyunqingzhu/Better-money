# 跨实现互通验证：电脑(Python) 导出 → 手机(TS) 导入 → 手机导出 → 电脑导入
$ErrorActionPreference = "Stop"
$root = "D:\Better-money"
$py = "$root\.venv\Scripts\python.exe"
$tmp = Join-Path $env:TEMP ("bm-cross-" + (Get-Random))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null

$pyHomeA = "$tmp\py-a"
$pyHomeB = "$tmp\py-b"
$pkgPy = "$tmp\from-python.zip"
$pkgTs = "$tmp\from-ts.zip"

Write-Host "== 1. Python 种子并导出 =="
$out = & $py "$root\tools\_cross_py.py" seed $pyHomeA $pkgPy
Write-Host $out

Write-Host "== 2. TS 导入 Python 包 =="
Set-Location "$root\mobile"
$out = & npx tsx "tests\_cross_ts.ts" import $pkgPy
Write-Host $out

Write-Host "== 3. TS 导入后导出（roundtrip） =="
$out = & npx tsx "tests\_cross_ts.ts" roundtrip $pkgPy $pkgTs
Write-Host $out

Write-Host "== 4. Python 导入 TS 包（新设备） =="
$out = & $py "$root\tools\_cross_py.py" import $pyHomeB $pkgTs
Write-Host $out

Write-Host "CROSS-CHECK DONE"
