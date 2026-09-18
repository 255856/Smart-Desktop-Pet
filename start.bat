@echo off
REM ============================================================
REM   桌面宠物 - 启动脚本
REM   双击即可启动；按 Ctrl+C 终止
REM ============================================================

REM 切到脚本所在目录
cd /d "%~dp0"

REM 设置 PYTHONPATH，让 Python 找到 .local-packages 里的包
set PYTHONPATH=%~dp0.local-packages;%PYTHONPATH%

REM 启动桌宠
echo [启动] 启动桌宠...
python main.py
if errorlevel 1 (
    echo.
    echo [错误] 桌宠退出，错误码 %errorlevel%
    pause
)