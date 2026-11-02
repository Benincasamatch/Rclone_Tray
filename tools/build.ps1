param([switch]$Test)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$toolRoot = 'E:\tools\rust'
$cargo = Join-Path $toolRoot 'cargo\bin\cargo.exe'
if (-not (Test-Path -LiteralPath $cargo)) {
    $found = Get-Command cargo.exe -ErrorAction SilentlyContinue
    if (-not $found) { throw 'Rust 未安装。请先安装 Rust，或使用 E:\tools\rust\activate.cmd 配置当前终端。' }
    $cargo = $found.Source
}
$previous = @{
    CARGO_HOME = $env:CARGO_HOME; RUSTUP_HOME = $env:RUSTUP_HOME; PATH = $env:PATH
    LINKER = $env:CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER
    RUSTFLAGS = $env:CARGO_TARGET_X86_64_PC_WINDOWS_GNU_RUSTFLAGS
    TEMP = $env:TEMP; TMP = $env:TMP
}
try {
    if ($cargo -eq (Join-Path $toolRoot 'cargo\bin\cargo.exe')) {
        $env:CARGO_HOME = Join-Path $toolRoot 'cargo'
        $env:RUSTUP_HOME = Join-Path $toolRoot 'rustup'
        $env:PATH = (Join-Path $env:CARGO_HOME 'bin') + ';' + $env:PATH
    }
    Push-Location $root
    try {
        $buildTemp = Join-Path $root 'target\.build-tmp'
        New-Item -ItemType Directory -Path $buildTemp -Force | Out-Null
        $env:TEMP = $buildTemp
        $env:TMP = $buildTemp
        $rustc = Join-Path (Split-Path -Parent $cargo) 'rustc.exe'
        $sysroot = (& $rustc --print sysroot | Out-String).Trim()
        if ($LASTEXITCODE -ne 0) { throw '无法读取 Rust 工具链目录。' }
        $gnuTools = Join-Path $sysroot 'lib\rustlib\x86_64-pc-windows-gnu\bin\self-contained'
        if (-not (Test-Path -LiteralPath (Join-Path $gnuTools 'dlltool.exe'))) { throw '缺少 GNU DLL 链接工具；请安装完整的 Rust Windows GNU 工具链。' }
        $gnuSupport = Join-Path $toolRoot 'w64devkit\bin'
        if (-not (Test-Path -LiteralPath (Join-Path $gnuSupport 'as.exe'))) { throw '缺少 GNU 汇编器；请将 w64devkit 安装到 E:\tools\rust\w64devkit。' }
        $env:PATH = $gnuSupport + ';' + $gnuTools + ';' + $env:PATH
        if (-not $env:CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER) {
            $env:CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER = Join-Path $gnuTools 'x86_64-w64-mingw32-gcc.exe'
        }
        if (-not $env:CARGO_TARGET_X86_64_PC_WINDOWS_GNU_RUSTFLAGS) {
            $env:CARGO_TARGET_X86_64_PC_WINDOWS_GNU_RUSTFLAGS = '-C link-self-contained=yes'
        }
        if ($Test) {
            & $cargo test --locked
            if ($LASTEXITCODE -ne 0) { throw 'Rust 回归测试失败；未更新发布产物。' }
        }
        & $cargo build --release --locked
        if ($LASTEXITCODE -ne 0) { throw 'Rust 构建失败；未更新发布产物。' }
        $rclone = Join-Path $root 'rclone\rclone.exe'
        if (-not (Test-Path -LiteralPath $rclone)) { throw '缺少 rclone\rclone.exe；请放置官方 Windows rclone 二进制后打包。' }
        $cargoToml = Get-Content -LiteralPath (Join-Path $root 'Cargo.toml') -Raw
        $versionMatch = [regex]::Match($cargoToml, '(?m)^version\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"')
        if (-not $versionMatch.Success) { throw 'Cargo.toml 缺少 x.y.z 版本号；无法生成安装程序。' }
        $version = $versionMatch.Groups[1].Value
        $isccCandidates = @(@(
            $env:INNO_SETUP_ISCC,
            (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
            (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
            (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe')
        ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) })
        if (-not $isccCandidates) {
            throw '缺少 Inno Setup 6；请运行 winget install JRSoftware.InnoSetup，或设置 INNO_SETUP_ISCC。'
        }
        $iscc = $isccCandidates[0]
        $dist = Join-Path $root 'dist'
        New-Item -ItemType Directory -Path (Join-Path $dist 'rclone') -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $root 'target\release\rclone_tray.exe') -Destination (Join-Path $dist 'rclone_tray.exe') -Force
        Copy-Item -LiteralPath $rclone -Destination (Join-Path $dist 'rclone\rclone.exe') -Force
        Copy-Item -LiteralPath (Join-Path $root 'LICENSE') -Destination (Join-Path $dist 'LICENSE') -Force
        $exe = Get-Item -LiteralPath (Join-Path $dist 'rclone_tray.exe')
        $hash = (Get-FileHash -LiteralPath $exe.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        $utf8 = [System.Text.UTF8Encoding]::new($false)
        [System.IO.File]::WriteAllText((Join-Path $dist 'rclone_tray.exe.sha256'), "$hash  rclone_tray.exe`n", $utf8)
        & $iscc "/DAppVersion=$version" (Join-Path $root 'tools\installer.iss')
        if ($LASTEXITCODE -ne 0) { throw '安装程序构建失败；未更新发布清单。' }
        $setup = Get-Item -LiteralPath (Join-Path $dist 'rclone_tray_setup.exe')
        $setupHash = (Get-FileHash -LiteralPath $setup.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        [System.IO.File]::WriteAllText((Join-Path $dist 'rclone_tray_setup.exe.sha256'), "$setupHash  rclone_tray_setup.exe`n", $utf8)
        $commit = (& git -C $root rev-parse --short=8 HEAD 2>$null | Out-String).Trim()
        $commitLabel = if ($commit) { $commit } else { 'unknown' }
        $manifest = [ordered]@{
            application = 'rclone_tray'; version = $version; implementation = 'Rust + Win32'
            built_at = [DateTimeOffset]::Now.ToString('o'); commit = $commitLabel
            executable = 'rclone_tray.exe'
            executable_bytes = $exe.Length; sha256 = $hash; target = 'x86_64-pc-windows-gnu'
            installer = 'rclone_tray_setup.exe'; installer_bytes = $setup.Length; installer_sha256 = $setupHash
            cargo = (& $cargo --version | Out-String).Trim(); frontend_close = 'DestroyWindow + drop'
            rclone = 'rclone/rclone.exe'; python_required = $false; webview_required = $false
        }
        [System.IO.File]::WriteAllText((Join-Path $dist 'build-manifest.json'), ($manifest | ConvertTo-Json -Depth 4) + "`n", $utf8)
        Write-Output "发布目录：$dist"
        Write-Output "应用 EXE：$($exe.Length) bytes；SHA-256：$hash"
        Write-Output "安装程序：$($setup.Length) bytes；SHA-256：$setupHash"
    } finally { Pop-Location }
} finally {
    $env:CARGO_HOME = $previous.CARGO_HOME
    $env:RUSTUP_HOME = $previous.RUSTUP_HOME
    $env:PATH = $previous.PATH
    $env:CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER = $previous.LINKER
    $env:CARGO_TARGET_X86_64_PC_WINDOWS_GNU_RUSTFLAGS = $previous.RUSTFLAGS
    $env:TEMP = $previous.TEMP
    $env:TMP = $previous.TMP
}
