# 一键打包成 Windows 便携版
#   产物: release\MediaOrganizer\        (整个文件夹拷走即用)
#         release\MediaOrganizer_v1.0.zip (分享用)
# 用法: 在项目根目录右键"使用 PowerShell 运行", 或  .\build.ps1
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location $root

Write-Host "[1/4] 关掉正在运行的实例(否则 exe 被占用无法覆盖)"
Get-Process MediaOrganizer -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 800

Write-Host "[2/4] PyInstaller 打包 -> release\MediaOrganizer"
$rel = Join-Path $root "release\MediaOrganizer"
if (Test-Path $rel) { Remove-Item $rel -Recurse -Force }
py -m PyInstaller --noconfirm --distpath (Join-Path $root "release") `
    --workpath (Join-Path $env:TEMP "MO_build") (Join-Path $root "MediaOrganizer.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败" }

Write-Host "[3/4] 放入使用说明"
Copy-Item (Join-Path $root "使用说明.txt") $rel -Force

Write-Host "[4/4] 打 zip"
$zip = Join-Path $root "release\MediaOrganizer_v1.0.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path $rel -DestinationPath $zip

$sz = (Get-ChildItem $rel -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
Write-Host ("完成: {0}  ({1:N1} MB)  以及 {2}" -f $rel, $sz, $zip)
