<#
.SYNOPSIS
    验证 SCADA 安装包：静默安装 → 逐项检查 → 静默卸载 → 检查残留。

.DESCRIPTION
    「卸载干净」不能靠肉眼看。这个脚本把「干净」拆成可断言的项：

    安装后必须存在：
      · 程序主文件、卸载程序
      · 开始菜单快捷方式、（可选）桌面快捷方式
      · 注册表 HKLM\Software\SCADA（InstallPath / Version）
      · 卸载项出现在「应用和功能」里
      · 安装日志文件

    卸载后必须消失：
      · 安装目录下所有程序文件（exe / dll / _internal）
      · 开始菜单快捷方式
      · 注册表 HKLM\Software\SCADA
      · 卸载项
      · （加了 -DeleteData 时）data / logs / exports

    默认**装在临时目录**，不污染真实 Program Files。
    每一步都写日志，失败时打印实际状态。

.PARAMETER SetupExe
    安装包路径。默认自动找 dist-installer 下最新版本。

.PARAMETER InstallDir
    测试安装目录。默认 %TEMP%\SCADA-verify-<时间戳>。

.PARAMETER DeleteData
    卸载时同时删运行数据（验证「完全干净」）。不加则验证「保留数据」路径。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File installer\verify_install.ps1
    powershell -ExecutionPolicy Bypass -File installer\verify_install.ps1 -DeleteData
#>
[CmdletBinding()]
param(
    [string]$SetupExe,
    [string]$InstallDir,
    [switch]$DeleteData,
    # 安装语言：en / chs。用来验证双语安装包两条路径都能正常装/卸。
    [string]$Lang = 'chs'
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Failures = New-Object System.Collections.Generic.List[string]
$Checks = 0

function Check($name, $ok, $detail = '') {
    $script:Checks++
    if ($ok) {
        Write-Host "  [PASS] $name" -ForegroundColor Green
    } else {
        Write-Host "  [FAIL] $name$(if ($detail) { "  — $detail" })" -ForegroundColor Red
        $script:Failures.Add("$name$(if ($detail) { ": $detail" })")
    }
    return $ok
}

function Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }

# ---------- 0. 定位安装包 ----------
if (-not $SetupExe) {
    $cand = Get-ChildItem -Path (Join-Path $RepoRoot 'dist-installer') -Filter 'SCADA-Setup-*.exe' -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $cand) { throw "找不到安装包。先跑 installer\build_installer.ps1" }
    $SetupExe = $cand.FullName
}
if (-not (Test-Path $SetupExe)) { throw "安装包不存在: $SetupExe" }

$Version = (Get-Content (Join-Path $RepoRoot 'VERSION') -Raw).Trim()
# 卸载项显示名 = AppVerName（scada.iss 里 AppName + 版本）。
# 不能用 `*SCADA*` 通配：机器上可能有 SmartSCADA 之类的同名前缀软件，
# 会把别人的条目算到自己头上（实测踩到：SmartSCADA 1.3.1079）。
$ExpectedDisplayName = "SCADA 工业监控系统 $Version"
if (-not $InstallDir) {
    $InstallDir = Join-Path $env:TEMP ("SCADA-verify-" + (Get-Date -Format 'yyyyMMdd-HHmmss'))
}
$InstallLog = Join-Path $env:TEMP ("scada-install-" + (Get-Date -Format 'yyyyMMdd-HHmmss') + ".log")
$UninstallLog = Join-Path $env:TEMP ("scada-uninstall-" + (Get-Date -Format 'yyyyMMdd-HHmmss') + ".log")

Write-Host "安装包 : $SetupExe"
Write-Host "版本   : $Version"
Write-Host "测试目录: $InstallDir"
Write-Host "删数据 : $(if ($DeleteData) { '是' } else { '否（验证保留数据路径）' })"
Write-Host "语言   : $Lang"

# 清理可能存在的旧测试目录
if (Test-Path $InstallDir) { Remove-Item -Path $InstallDir -Recurse -Force -ErrorAction SilentlyContinue }

# ---------- 1. 静默安装 ----------
Step "1. 静默安装"
$installArgs = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-',
                 '/CURRENTUSER', "/LANG=$Lang", "/DIR=$InstallDir", "/LOG=$InstallLog", '/TASKS=""')
$p = Start-Process -FilePath $SetupExe -ArgumentList $installArgs -Wait -PassThru
Check "安装程序退出码为 0" ($p.ExitCode -eq 0) "实际 $($p.ExitCode)"

if (-not (Test-Path $InstallDir)) {
    Write-Host "`n安装目录不存在，后续检查无法进行。" -ForegroundColor Red
    exit 1
}

# ---------- 2. 安装后检查 ----------
Step "2. 安装结果检查"

$mainExe = Join-Path $InstallDir 'SCADA.exe'
Check "主程序存在" (Test-Path $mainExe) $mainExe

$uninst = Get-ChildItem -Path $InstallDir -Filter 'unins*.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
Check "卸载程序存在" ($null -ne $uninst) "在 $InstallDir 下找 unins*.exe"

Check "安装日志已生成" (Test-Path $InstallLog) $InstallLog

# 开始菜单
# `/CURRENTUSER` 装到用户级开始菜单（%APPDATA%），管理员装到全局（%ProgramData%）。
# 两个都要查 —— 只查 ProgramData 会在 currentuser 模式下误报「快捷方式缺失」。
$startMenuCandidates = @(
    (Join-Path $env:ProgramData ('Microsoft\Windows\Start Menu\Programs\SCADA')),
    (Join-Path $env:APPDATA     ('Microsoft\Windows\Start Menu\Programs\SCADA'))
)
$startMenu = $startMenuCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $startMenu) { $startMenu = $startMenuCandidates[0] }
Check "开始菜单文件夹存在" (Test-Path $startMenu) $startMenu
$lnk = Get-ChildItem -Path $startMenu -Filter '*.lnk' -ErrorAction SilentlyContinue
Check "开始菜单有快捷方式" ($lnk.Count -ge 1) "找到 $($lnk.Count) 个"

# 注册表
# currentuser 模式写 HKCU，管理员模式写 HKLM —— 两个都查
$regPath = if (Test-Path 'HKCU:\Software\SCADA') { 'HKCU:\Software\SCADA' } else { 'HKLM:\Software\SCADA' }
$regOk = Test-Path $regPath
Check "注册表 Software\SCADA 存在" $regOk $regPath
if ($regOk) {
    $installedPath = (Get-ItemProperty -Path $regPath -Name 'InstallPath' -ErrorAction SilentlyContinue).InstallPath
    Check "注册表 InstallPath 正确" ($installedPath -eq $InstallDir) "期望 $InstallDir，实际 $installedPath"
    $regVer = (Get-ItemProperty -Path $regPath -Name 'Version' -ErrorAction SilentlyContinue).Version
    Check "注册表 Version 正确" ($regVer -eq $Version) "期望 $Version，实际 $regVer"
}

# 卸载项（应用和功能）
$uninstKeys = @(
    'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*',
    'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*',
    'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*'
)
$uninstEntry = Get-ItemProperty -Path $uninstKeys -ErrorAction SilentlyContinue |
               Where-Object { $_.DisplayName -eq $ExpectedDisplayName } | Select-Object -First 1
Check "出现在「应用和功能」" ($null -ne $uninstEntry) "Uninstall 注册表项"
if ($uninstEntry) {
    Check "卸载项显示名含版本" ($uninstEntry.DisplayName -like "*$Version*") $uninstEntry.DisplayName
}

# ---------- 3. 制造运行数据（验证卸载会处理它）----------
Step "3. 造一点运行数据"
foreach ($d in @('data', 'logs', 'exports')) {
    $p2 = Join-Path $InstallDir $d
    if (-not (Test-Path $p2)) { New-Item -ItemType Directory -Path $p2 -Force | Out-Null }
    Set-Content -Path (Join-Path $p2 'verify-marker.txt') -Value "created by verify_install.ps1" -Encoding UTF8
}
Check "已创建 data/logs/exports 标记文件" $true

# ---------- 4. 静默卸载 ----------
Step "4. 静默卸载"
if (-not $uninst) { throw "没有卸载程序，无法继续" }
$uninstArgs = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/LOG=$UninstallLog")
if ($DeleteData) { $uninstArgs += '/DELETE-DATA' }

$up = Start-Process -FilePath $uninst.FullName -ArgumentList $uninstArgs -Wait -PassThru
Check "卸载程序退出码为 0" ($up.ExitCode -eq 0) "实际 $($up.ExitCode)"
Check "卸载日志已生成" (Test-Path $UninstallLog) $UninstallLog

# Inno 的卸载程序会把自己复制到 temp 再跑，原进程结束后文件可能还占着一两秒
Start-Sleep -Seconds 2

# ---------- 5. 卸载后检查 ----------
Step "5. 残留检查"

$leftExe = Get-ChildItem -Path $InstallDir -Filter '*.exe' -Recurse -ErrorAction SilentlyContinue
Check "程序文件已删除" ($leftExe.Count -eq 0) "残留 $($leftExe.Count) 个 exe: $($leftExe.Name -join ', ')"

$menuLeft = $startMenuCandidates | Where-Object { Test-Path $_ }
Check "开始菜单已清理" ($null -eq $menuLeft) ($menuLeft -join '; ')
Check "注册表已清理" (-not (Test-Path $regPath)) $regPath

$uninstLeft = Get-ItemProperty -Path $uninstKeys -ErrorAction SilentlyContinue |
              Where-Object { $_.DisplayName -eq $ExpectedDisplayName } | Select-Object -First 1
Check "卸载项已移除" ($null -eq $uninstLeft) "仍能找到: $($uninstLeft.DisplayName)"

if ($DeleteData) {
    $leftDirs = @()
    foreach ($d in @('data', 'logs', 'exports')) {
        if (Test-Path (Join-Path $InstallDir $d)) { $leftDirs += $d }
    }
    Check "运行数据已删除（-DeleteData）" ($leftDirs.Count -eq 0) "残留: $($leftDirs -join ', ')"
} else {
    $kept = @()
    foreach ($d in @('data', 'logs', 'exports')) {
        if (Test-Path (Join-Path $InstallDir $d)) { $kept += $d }
    }
    Check "运行数据已保留（未加 -DeleteData）" ($kept.Count -eq 3) "实际保留: $($kept -join ', ')"
    Write-Host "  提示：验证「完全干净」请加 -DeleteData 再跑一遍" -ForegroundColor Yellow
}

# 防火墙规则
$fw = & netsh advfirewall firewall show rule name="SCADA Web" 2>&1
$fwExists = ($LASTEXITCODE -eq 0) -and ($fw -notmatch 'No rules match')
Check "防火墙规则已删除" (-not $fwExists) "netsh 仍能找到 'SCADA Web'"

# ---------- 6. 汇总 ----------
Step "汇总"
Write-Host "  检查项: $Checks，失败: $($Failures.Count)"
if ($Failures.Count -gt 0) {
    Write-Host "`n失败项：" -ForegroundColor Red
    foreach ($f in $Failures) { Write-Host "  - $f" -ForegroundColor Red }
    Write-Host "`n日志：" -ForegroundColor Yellow
    Write-Host "  安装  $InstallLog"
    Write-Host "  卸载  $UninstallLog"
    exit 1
}

Write-Host "`n全部通过。" -ForegroundColor Green
Write-Host "安装日志: $InstallLog"
Write-Host "卸载日志: $UninstallLog"
if (Test-Path $InstallDir) {
    Write-Host "测试目录仍存在（保留数据的正常现象）: $InstallDir" -ForegroundColor Yellow
    Write-Host "确认后可手动删除。" -ForegroundColor Yellow
}
exit 0