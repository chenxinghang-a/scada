# 打包industrial_scada项目到桌面
$ErrorActionPreference = "Stop"

# 项目目录
$projectDir = "c:\Users\cxx\WorkBuddy\Claw\industrial_scada"
$projectName = "industrial_scada"

# 桌面路径
$desktopDir = [Environment]::GetFolderPath("Desktop")
if (-not (Test-Path $desktopDir)) {
    Write-Error "桌面目录不存在: $desktopDir"
    exit 1
}

# 生成时间戳
$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$zipFilename = "${projectName}_${timestamp}.zip"
$zipPath = Join-Path $desktopDir $zipFilename

# 临时目录
$tempDir = Join-Path $env:TEMP "scada_package_$timestamp"

Write-Host "开始打包项目: $projectDir"
Write-Host "目标文件: $zipPath"

try {
    # 创建临时目录
    New-Item -ItemType Directory -Path $tempDir -Force | Out-Null
    
    # 排除的目录和文件
    $excludeDirs = @(
        "__pycache__",
        "logs",
        "data",
        "exports",
        "backup",
        ".git",
        "venv",
        ".venv"
    )
    
    $excludeFiles = @(
        "*.pyc",
        "*.pyo",
        "*.db",
        "*.sqlite3",
        "*.log",
        "*.bak",
        ".env",
        "stderr*.txt",
        "stdout*.txt",
        "package_to_desktop.py",
        "package_to_desktop.ps1"
    )
    
    # 复制文件到临时目录，排除不需要的文件
    Write-Host "复制文件..."
    
    # 使用robocopy进行复制，排除目录和文件
    $robocopyArgs = @(
        $projectDir,
        $tempDir,
        "/E",  # 包含子目录
        "/XD"  # 排除目录
    )
    $robocopyArgs += $excludeDirs
    $robocopyArgs += "/XF"  # 排除文件
    $robocopyArgs += $excludeFiles
    
    & robocopy @robocopyArgs | Out-Null
    
    # 压缩临时目录
    Write-Host "压缩文件..."
    Compress-Archive -Path "$tempDir\*" -DestinationPath $zipPath -Force
    
    # 显示结果
    $zipSize = (Get-Item $zipPath).Length
    $zipSizeMB = [math]::Round($zipSize / 1MB, 2)
    
    Write-Host "`n打包完成!" -ForegroundColor Green
    Write-Host "文件: $zipPath"
    Write-Host "大小: $zipSizeMB MB"
    
} catch {
    Write-Error "打包过程中发生错误: $_"
    exit 1
} finally {
    # 清理临时目录
    if (Test-Path $tempDir) {
        Remove-Item -Path $tempDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Write-Host "`n✅ 项目已成功打包到桌面!" -ForegroundColor Green