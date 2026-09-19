@echo off
REM ============================================================
REM   启动 GPT-SoVITS 本地 TTS 服务（桌宠语音依赖此服务）
REM   端口 9880；关掉本窗口即停止服务
REM ============================================================
cd /d "%~dp0GPT-SoVITS-v2pro-20250604-nvidia50"
runtime\python.exe api_v2.py -a 127.0.0.1 -p 9880 -c GPT_SoVITS\configs\tts_infer.yaml
pause
