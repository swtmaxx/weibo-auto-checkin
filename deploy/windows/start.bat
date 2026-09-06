@echo off
chcp 65001 >nul
setlocal
rem 微博超话签到 - Windows 启动脚本
rem 双击运行或由任务计划程序调用;首次运行会自动创建虚拟环境并安装依赖。
rem 如需修改监听地址/端口,在运行前设置 APP_HOST / APP_PORT 环境变量。

set "APP_DIR=%~dp0..\.."
cd /d "%APP_DIR%" || goto :error

if "%APP_HOST%"=="" set "APP_HOST=127.0.0.1"
if "%APP_PORT%"=="" set "APP_PORT=8000"

if not exist ".venv\Scripts\python.exe" (
    echo [首次运行] 创建虚拟环境...
    python -m venv .venv || goto :error
    echo [首次运行] 安装依赖,可能需要几分钟...
    ".venv\Scripts\python.exe" -m pip install -e . || goto :error
)

if not exist ".venv\Scripts\uvicorn.exe" (
    echo [错误] 依赖不完整,请删除 .venv 目录后重新运行本脚本。
    goto :error
)

echo.
echo 微博超话签到启动中: http://%APP_HOST%:%APP_PORT%
echo 停止服务: 在本窗口按 Ctrl+C,或直接关闭窗口。
echo.
".venv\Scripts\uvicorn.exe" --factory app.main:create_app --host %APP_HOST% --port %APP_PORT%
goto :eof

:error
echo.
echo 启动失败,请确认已安装 Python 3.11+ 并勾选 "Add python.exe to PATH"。
pause
exit /b 1
