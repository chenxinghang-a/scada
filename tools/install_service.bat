@echo off
REM SCADA系统Windows服务注册脚本
REM 以管理员权限运行此脚本

echo ========================================
echo SCADA系统Windows服务注册
echo ========================================

REM 检查管理员权限
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo [错误] 请以管理员权限运行此脚本！
    echo 右键点击 -> 以管理员身份运行
    pause
    exit /b 1
)

REM 检查nssm是否存在
where nssm >nul 2>&1
if %errorLevel% neq 0 (
    echo [提示] nssm未找到，正在下载...
    powershell -Command "Invoke-WebRequest -Uri 'https://nssm.cc/release/nssm-2.24.zip' -OutFile 'nssm.zip'"
    powershell -Command "Expand-Archive -Path 'nssm.zip' -DestinationPath '.' -Force"
    copy nssm-2.24\win64\nssm.exe . >nul
    del nssm.zip
    rmdir /s /q nssm-2.24
)

REM 注册服务
echo [1/3] 注册SCADA服务...
nssm install SmartSCADA "%~dp0..\run.py"
nssm set SmartSCADA AppDirectory "%~dp0.."
nssm set SmartSCADA DisplayName "SmartSCADA工业数据采集系统"
nssm set SmartSCADA Description "工业SCADA数据采集、监控和报警系统"
nssm set SmartSCADA Start SERVICE_AUTO_START
nssm set SmartSCADA AppStdout "%~dp0..\logs\service_stdout.log"
nssm set SmartSCADA AppStderr "%~dp0..\logs\service_stderr.log"
nssm set SmartSCADA AppStdoutCreationDisposition 4
nssm set SmartSCADA AppStderrCreationDisposition 4
nssm set SmartSCADA AppRotateFiles 1
nssm set SmartSCADA AppRotateBytes 10485760

REM 设置崩溃自动重启
echo [2/3] 配置崩溃自动重启...
nssm set SmartSCADA AppExit Default Restart
nssm set SmartSCADA AppRestartDelay 5000

REM 启动服务
echo [3/3] 启动服务...
nssm start SmartSCADA

echo.
echo ========================================
echo 服务注册完成！
echo 服务名称: SmartSCADA
echo 启动类型: 自动
echo 崩溃恢复: 5秒后自动重启
echo.
echo 管理命令:
echo   启动: nssm start SmartSCADA
echo   停止: nssm stop SmartSCADA
echo   状态: nssm status SmartSCADA
echo   卸载: nssm remove SmartSCADA confirm
echo ========================================
pause
