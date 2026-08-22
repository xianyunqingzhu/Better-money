Add-Type -AssemblyName System.Drawing
$src = [System.Drawing.Image]::FromFile("D:\Better-money\图标.png")
$sizes = @{ "mdpi" = 48; "hdpi" = 72; "xhdpi" = 96; "xxhdpi" = 144; "xxxhdpi" = 192 }
$side = [System.Math]::Min($src.Width, $src.Height)
$srcX = [int](($src.Width - $side) / 2)
$srcY = [int](($src.Height - $side) / 2)
foreach ($d in $sizes.Keys) {
    $s = $sizes[$d]
    $dir = "D:\Better-money\mobile\android\app\src\main\res\mipmap-$d"
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $bmp = New-Object System.Drawing.Bitmap($s, $s)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
    $g.Clear([System.Drawing.Color]::Transparent)
    $srcRect = New-Object System.Drawing.Rectangle($srcX, $srcY, $side, $side)
    $destRect = New-Object System.Drawing.Rectangle(0, 0, $s, $s)
    $g.DrawImage($src, $destRect, $srcRect, [System.Drawing.GraphicsUnit]::Pixel)
    $g.Dispose()
    $bmp.Save("$dir\ic_launcher.png", [System.Drawing.Imaging.ImageFormat]::Png)
    $bmp.Save("$dir\ic_launcher_round.png", [System.Drawing.Imaging.ImageFormat]::Png)
    $bmp.Dispose()
    Write-Host "$d done ($s x $s), file size: $((Get-Item "$dir\ic_launcher.png").Length)"
}
$src.Dispose()
Write-Host "icons generated"
