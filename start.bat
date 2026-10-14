@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM ============================================================
REM Desktop Pet launcher
REM - Kills leftover processes
REM - Starts GPT-SoVITS (if package exists and :9880 free)
REM - Waits for :9880 to listen (up to 90s)
REM - Launches the pet (reuses :9880 if already up)
REM ============================================================

echo.
echo === Cleanup leftover processes ===
taskkill /im python.exe /f >nul 2>&1
taskkill /im "python 3.10.10.exe" /f >nul 2>&1
taskkill /im QtWebEngineProcess.exe /f >nul 2>&1
ping -n 3 127.0.0.1 >nul

echo.
echo === Check :9880 ===
netstat -ano | findstr ":9880" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 goto :gpt_ready

set GPT_PKG=%~dp0GPT-SoVITS-v2pro-20250604-nvidia50
set RUNTIME_PY=%GPT_PKG%\runtime\python.exe

if not exist "%RUNTIME_PY%" goto :gpt_ready

echo.
echo === Starting GPT-SoVITS (first model load: ~30s) ===
setlocal
cd /d "%GPT_PKG%"
start "GPT-SoVITS" /B "%RUNTIME_PY%" api_v2.py -a 127.0.0.1 -p 9880 -c GPT_SoVITS\configs\tts_infer.yaml
endlocal

echo.
echo === Waiting for :9880 (no timeout - GPT-SoVITS model load can take ^>60s) ===
:wait_port
timeout /t 3 /nobreak >nul
netstat -ano | findstr ":9880" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 goto :gpt_ready
goto :wait_port

:gpt_ready
echo GPT-SoVITS ready on :9880

:gpt_done

echo.
echo === Pet environment ===
set PYTHONPATH=%~dp0.local-packages;%PYTHONPATH%
set OMP_NUM_THREADS=1
set KMP_DUPLICATE_LIB_OK=TRUE
set KMP_WARNINGS=0

echo.
echo === Launch desktop pet ===
echo Tip: close this window to stop the pet (GPT-SoVITS stays running)
echo.
python main.py
if errorlevel 1 goto :pet_failed
goto :pet_end

:pet_failed
echo.
echo Error: pet exited with code %errorlevel%
echo Log:    see crash.log or logs\pet.log
pause

:pet_end
