@echo off
chcp 65001 >nul
REM ============================================================
REM   Desktop Pet - launcher (no PyCharm needed)
REM   Double-click to start; close the window to stop the pet
REM ============================================================

cd /d "%~dp0"

REM ============================================================
REM 1) Kill leftovers (use taskkill /im directly - more reliable than wmic)
REM ============================================================
echo [Cleanup] killing leftover python.exe (GPT-SoVITS + pet) ...
taskkill /im python.exe /f >nul 2>&1
taskkill /im "python 3.10.10.exe" /f >nul 2>&1
echo [Cleanup] killing leftover QtWebEngineProcess ...
taskkill /im QtWebEngineProcess.exe /f >nul 2>&1
REM Give the OS a moment to reap processes
ping -n 3 127.0.0.1 >nul

REM ============================================================
REM 2) Start GPT-SoVITS first (if package exists AND port 9880 free).
REM    Use setlocal+cd to scope the cwd change. Pet then reuses :9880.
REM ============================================================
set "GPT_PKG=%~dp0GPT-SoVITS-v2pro-20250604-nvidia50"
set "RUNTIME_PY=%GPT_PKG%\runtime\python.exe"

if exist "%RUNTIME_PY%" goto :start_gpt
goto :gpt_done

:start_gpt
REM One PowerShell call: check if :9880 is listening
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 9880 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>&1
if not errorlevel 1 (
    echo [TTS] GPT-SoVITS already on :9880, reusing
    goto :gpt_done
)

echo [TTS] Starting GPT-SoVITS in background (first model load: ^~30s)...
REM setlocal so cd doesn't leak to the python main.py launch below
setlocal
cd /d "%GPT_PKG%"
start "GPT-SoVITS" /B "%RUNTIME_PY%" api_v2.py -a 127.0.0.1 -p 9880 -c GPT_SoVITS\configs\tts_infer.yaml
REM Give it 3s head start so :9880 is up before pet starts
ping -n 4 127.0.0.1 >nul
endlocal

:gpt_done
echo.

REM ============================================================
REM 3) Pet environment
REM ============================================================
set PYTHONPATH=%~dp0.local-packages;%PYTHONPATH%
set OMP_NUM_THREADS=1
set KMP_DUPLICATE_LIB_OK=TRUE
set KMP_WARNINGS=0

REM ============================================================
REM 4) Launch
REM ============================================================
echo [Launch] starting desktop pet...
echo [Tip]    close this window to stop the pet (GPT-SoVITS stays running)
echo.
python main.py
if errorlevel 1 (
    echo.
    echo [Error] pet exited with code %errorlevel%
    echo [Log]    see crash.log or logs\pet.log
    pause
)
