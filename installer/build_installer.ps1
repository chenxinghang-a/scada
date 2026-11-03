<#
.SYNOPSIS
    构建 SCADA Windows 安装包（PyInstaller 产物 + Inno Setup 封装）。

.DESCRIPTION
    两步：
      1. build.py  →  dist-scada-<VERSION>\SCADA.exe（onedir/onefile 产物）
      2. ISCC.exe  →  dist-installer\SCADA-Setup-<VERSION>.exe

    没有装 Inno Setup 时**不会静默跳过**：脚本会打印安装命令并以退出码 2 结束，
    避免 CI 里出现「构建成功但没有安装包」这种假绿。

.PARAMETER SkipPyInstaller
    跳过第 1 步，直接复用已有的 dist-scada-<VERSION>（改 .iss 时快速迭代用）。

.PARAMETER Clean
    先删掉旧产物（dist-installer 下同版本的文件）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File installer\build_installer.ps1
    powershell -ExecutionPolicy Bypass -File installer\build_installer.ps1 -SkipPyInstaller
#>
[CmdletBinding()]
param(
    [switch]$SkipPyInstaller,
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$InstallerDir = $PSScriptRoot
$OutDir = Join-Path $RepoRoot 'dist-installer'

function Write-Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Warn2($msg) { Write-Host "  [!!] $msg" -ForegroundColor Yellow }

# ---------- 1. 版本 ----------
$VersionFile = Join-Path $RepoRoot 'VERSION'
if (-not (Test-Path $VersionFile)) { throw "找不到 VERSION 文件: $VersionFile" }
$Version = (Get-Content $VersionFile -Raw).Trim()
if (-not $Version) { throw "VERSION 文件为空" }
Write-Step "版本 $Version"

$SourceDir = Join-Path $RepoRoot "dist-scada-$Version"
$SetupExe = Join-Path $OutDir "SCADA-Setup-$Version.exe"

# ---------- 2. PyInstaller ----------
if (-not $SkipPyInstaller) {
    Write-Step "步骤 1/2：PyInstaller 打包"
    $Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path $Python)) { $Python = 'python' }
    Push-Location $RepoRoot
    try {
        & $Python build.py
        if ($LASTEXITCODE -ne 0) { throw "build.py 失败（退出码 $LASTEXITCODE）" }
    } finally {
        Pop-Location
    }
    Write-Ok "PyInstaller 完成"
} else {
    Write-Step "步骤 1/2：跳过 PyInstaller（-SkipPyInstaller）"
}

if (-not (Test-Path $SourceDir)) {
    throw "找不到打包产物目录: $SourceDir`n请先跑 build.py，或去掉 -SkipPyInstaller。"
}
$exeCount = (Get-ChildItem -Path $SourceDir -Filter '*.exe' -Recurse -ErrorAction SilentlyContinue).Count
Write-Ok "产物目录 $SourceDir（$exeCount 个 exe）"

# ---------- 3. 找 Inno Setup ----------
Write-Step "步骤 2/2：Inno Setup 封装"

$candidates = @(
    "$env:ProgramFiles(x86)\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "C:\Program Files (x86)\Inno Setup 5\ISCC.exe"
)
$Iscc = $null
foreach ($c in $candidates) {
    if ($c -and (Test-Path $c)) { $Iscc = $c; break }
}
if (-not $Iscc) {
    $cmd = Get-Command iscc.exe -ErrorAction SilentlyContinue
    if ($cmd) { $Iscc = $cmd.Source }
}

if (-not $Iscc) {
    Write-Warn2 "没有找到 Inno Setup（ISCC.exe）"
    Write-Host @"

安装方式（任选一种，装完重跑本脚本即可）：

  winget install -e --id JRSoftware.InnoSetup
  或从官网下载：https://jrsoftware.org/isdl.php

已生成的东西：
  $SourceDir       —— 可直接解压运行的程序目录（绿色版）
  安装包需要 Inno 才能产出。

"@ -ForegroundColor Yellow
    exit 2
}
Write-Ok "ISCC: $Iscc"

# ---------- 4. 构建 ----------
if ($Clean -and (Test-Path $OutDir)) {
    Get-ChildItem -Path $OutDir -Filter "SCADA-Setup-$Version*.exe" | Remove-Item -Force
    Write-Ok "已清理旧安装包"
}
if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }

$IssFile = Join-Path $InstallerDir 'scada.iss'
if (-not (Test-Path $IssFile)) { throw "找不到 $IssFile" }

& $Iscc "/DAppVersion=$Version" "/DSourceDir=$SourceDir" $IssFile
if ($LASTEXITCODE -ne 0) { throw "ISCC 编译失败（退出码 $LASTEXITCODE）" }

if (-not (Test-Path $SetupExe)) { throw "ISCC 报告成功但找不到 $SetupExe" }

# ---------- 5. 结果 ----------
$info = Get-Item $SetupExe
$hash = (Get-FileHash $SetupExe -Algorithm SHA256).Hash
Write-Step "完成"
Write-Host "  安装包: $($info.FullName)"
Write-Host "  大小  : $([math]::Round($info.Length / 1MB, 2)) MB"
Write-Host "  版本  : $Version"
Write-Host "  SHA256: $hash"

# 发布清单（给现场核对下载是否完整）
$manifest = [ordered]@{
    version    = $Version
    built_at   = (Get-Date).ToString('s')
    installer  = $info.Name
    size_bytes = $info.Length
    sha256     = $hash
}
$manifestPath = Join-Path $OutDir "SCADA-Setup-$Version.manifest.json"
$manifest | ConvertTo-Json | Set-Content -Path $manifestPath -Encoding UTF8
Write-Host "  清单  : $manifestPath"

Write-Host "`n下一步：用 installer\verify_install.ps1 验证安装/卸载（见 docs\打包与安装.md）" -ForegroundColor Cyan
exit 0
