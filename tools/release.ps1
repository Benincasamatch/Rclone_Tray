param(
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$Message = '',
    [switch]$SkipPush,
    [switch]$SkipBuild
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    throw '版本号必须为 x.y.z，例如 2.1.0。'
}
$tag = "v$Version"
Push-Location $root
try {
    $dirty = (& git status --porcelain | Out-String).Trim()
    if ($dirty) { throw "工作区不干净；请先提交或还原：`n$dirty" }
    $toml = Get-Content -LiteralPath (Join-Path $root 'Cargo.toml') -Raw
    $current = [regex]::Match($toml, '(?m)^version\s*=\s*"([^"]+)"').Groups[1].Value
    if ($current -ne $Version) {
        throw "Cargo.toml 版本为 $current；请先改为 $Version 并提交，再运行发布脚本。"
    }
    if ((& git tag --list $tag)) { throw "标签 $tag 已存在。" }
    if (-not $SkipBuild) {
        & (Join-Path $PSScriptRoot 'build.ps1') -Test
        if ($LASTEXITCODE -ne 0) { throw '构建或回归测试失败；未创建标签。' }
    }
    if (-not $Message) { $Message = "release: rclone_tray $Version" }
    & git tag -a $tag -m $Message
    if ($LASTEXITCODE -ne 0) { throw "创建标签 $tag 失败。" }
    if (-not $SkipPush) {
        & git push origin HEAD
        if ($LASTEXITCODE -ne 0) { throw '推送分支失败；标签未推送。' }
        & git push origin $tag
        if ($LASTEXITCODE -ne 0) { throw "推送标签 $tag 失败。" }
    }
    Write-Output "已创建 $tag。"
    Write-Output "发布资产：dist\rclone_tray_setup.exe、dist\rclone_tray_setup.exe.sha256、dist\build-manifest.json"
    Write-Output '在 GitHub Release 中上传上述文件；更新检查读取 Releases 列表，草稿和预发布会被忽略。'
} finally { Pop-Location }
