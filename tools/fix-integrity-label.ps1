#Requires -Version 5.1
<#
.SYNOPSIS
    检查并修复工作区目录被打了 Low 完整性标签导致的问题。

.DESCRIPTION
    目录若带有 Mandatory Label\Low，Windows 会以 Low 完整性级别启动该目录下的 EXE，
    这类进程无法写入 %APPDATA%，rclone_tray 会报“创建临时文件失败（系统错误 5）”，
    WinFsp 挂载返回 c0000022。修复方式是把标签改回 Medium（需管理员）。

.PARAMETER Root
    要修复的目录，默认是仓库根目录。

.PARAMETER Apply
    由提权后的子进程使用；直接执行修复而不再次提权。
#>
param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot),
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'

function Get-IntegrityLabel([string]$Path) {
    $line = (& icacls.exe $Path 2>&1 | Select-String -Pattern 'Mandatory Label')
    if (-not $line) { return 'none' }
    return (($line | ForEach-Object { $_.Line.Trim() }) -join '; ')
}

function Show-State([string]$Path) {
    "{0}`n    {1}" -f $Path, (Get-IntegrityLabel $Path)
}

$targets = @($Root, (Join-Path $Root 'dist\rclone_tray.exe')) | Where-Object { Test-Path -LiteralPath $_ }

Write-Host "工作区完整性标签："
foreach ($target in $targets) { Show-State $target }

$low = $targets | Where-Object { (Get-IntegrityLabel $_) -match 'Low Mandatory Level' }
if (-not $low) {
    Write-Host "`n未发现 Low 完整性标签；无需修复。"
    exit 0
}

if (-not $Apply) {
    Write-Host "`n发现 Low 完整性标签，正在请求管理员权限修复……"
    $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $PSCommandPath, '-Root', $Root, '-Apply')
    $child = Start-Process -FilePath (Get-Process -Id $PID).Path -Verb RunAs -ArgumentList $arguments -PassThru -Wait
    Write-Host "`n权限修复结果（退出码 $($child.ExitCode)）："
}
else {
    & icacls.exe $Root /setintegritylevel '(OI)(CI)M' /C /Q | Out-Null
}

$after = @($Root, (Join-Path $Root 'dist\rclone_tray.exe')) | Where-Object { Test-Path -LiteralPath $_ }
foreach ($target in $after) { Show-State $target }

$stillLow = $after | Where-Object { (Get-IntegrityLabel $_) -match 'Low Mandatory Level' }
if ($stillLow) {
    Write-Error "`n仍有 Low 完整性标签：$($stillLow -join ', ')"
    exit 1
}
Write-Host "`n完成：工作区下的程序将以 Medium 完整性级别运行。"
exit 0
