# 一键把本项目发布到 GitHub（默认私有库；确认没问题后再转公开）
#
# 用法（在本项目目录）：
#   .\pub.ps1                  # 交互式：问你 GitHub 用户名 / 邮箱
#   .\pub.ps1 -User 你的用户名  # 也可以直接带上
#   .\pub.ps1 -DryRun          # 只演练，不真的推送
#
# 它会做四件事：
#   1) 把本仓库的作者署名改成你（只改本仓库，不动全局 git 配置）
#   2) 设置远端 origin -> https://github.com/<你>/<仓库名>.git
#   3) git push（会弹浏览器让你登录 GitHub，点同意即可，不用填 token）
#   4) 打印后续步骤（挂 Release 附件、转公开）
param(
    [string]$User = "",
    [string]$Email = "",
    [string]$Repo = "MediaOrganizer",
    [switch]$Public,
    [switch]$DryRun
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path (Join-Path $PSScriptRoot ".git"))) {
    throw "当前目录不是 git 仓库：$PSScriptRoot"
}

if (-not $User) { $User = Read-Host "请输入你的 GitHub 用户名" }
if (-not $User) { throw "用户名不能为空" }
if (-not $Email) {
    $Email = Read-Host "请输入你的 GitHub 邮箱（直接回车 = $User@users.noreply.github.com）"
}
if (-not $Email) { $Email = "$User@users.noreply.github.com" }

$vis = if ($Public) { "Public" } else { "Private" }
$url = "https://github.com/$User/$Repo.git"

Write-Host "[1/4] 设置本仓库作者署名（不影响全局 git 配置）" -ForegroundColor Cyan
Write-Host "      作者: $User <$Email>"
Write-Host "[2/4] 远端 origin: $url"
Write-Host "[3/4] 推送分支: main  可见性: $vis"

if ($DryRun) {
    Write-Host "[4/4] DryRun：以上只是演练，没有做任何修改。" -ForegroundColor Yellow
    exit 0
}

$dirty = git status --porcelain
if ($dirty) {
    Write-Host "      提示：还有未提交的改动（不影响推送，只是这些改动不会进仓库）" -ForegroundColor Yellow
}

git config user.name $User
git config user.email $Email
git commit --amend --reset-author --no-edit | Out-Null
Write-Host ("      已改好： " + (git log -1 --pretty=format:'%an <%ae>')) -ForegroundColor Green

git remote remove origin 2>$null
git remote add origin $url

Write-Host "[4/4] 正在推送……如果弹出浏览器窗口，请登录 GitHub 并点 Authorize" -ForegroundColor Cyan
git push -u origin main
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "推送失败。最常见的原因：GitHub 上还没有这个仓库。" -ForegroundColor Yellow
    Write-Host "请做这两步，然后重跑本脚本：" -ForegroundColor Yellow
    Write-Host "  1. 打开 https://github.com/new"
    Write-Host "  2. 仓库名填 $Repo ，可见性选 $vis ，"
    Write-Host "     **不要**勾选 Add a README / .gitignore / license（本地已经都有）"
    Write-Host "     → 点 Create repository"
    Write-Host "  3. 回到这里重新运行： .\pub.ps1 -User $User"
    exit 1
}

Write-Host ""
Write-Host "发布成功！仓库地址： https://github.com/$User/$Repo" -ForegroundColor Green
Write-Host ""
Write-Host "接下来（都在网页上点，不用命令）：" -ForegroundColor Cyan
Write-Host "  A. 挂便携版下载：仓库页 → Releases → Draft a new release"
Write-Host "     → Tag 填 v1.0.0 → 上传 release\MediaOrganizer_v1.0.zip → Publish release"
Write-Host "  B. 确认没问题后转公开：Settings → 页面最下方 Danger Zone"
Write-Host "     → Change repository visibility → Make public"
Write-Host "  C. 以后改了代码要更新： git add -A ; git commit -m ""说明"" ; git push"
