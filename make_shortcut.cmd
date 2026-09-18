@echo off
REM Create desktop shortcut for Desktop Pet
setlocal

set "DESK=%USERPROFILE%\Desktop"
echo Desktop: %DESK%

if not exist "%DESK%" (
    echo [ERROR] Desktop folder does not exist: %DESK%
    exit /b 1
)

set "LNK=%DESK%\DesktopPet.lnk"
set "TGT=E:\study\desktop-pet\start_silent.vbs"
set "WD=E:\study\desktop-pet"
set "ICO=E:\study\desktop-pet\assets\sprites\body_front.png"

if exist "%LNK%" del /f /q "%LNK%"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ws = New-Object -ComObject WScript.Shell;" ^
  "$s = $ws.CreateShortcut('%LNK%');" ^
  "$s.TargetPath = '%TGT%';" ^
  "$s.WorkingDirectory = '%WD%';" ^
  "$s.WindowStyle = 7;" ^
  "$s.Description = 'Desktop Pet - xiaoshen';" ^
  "$s.IconLocation = '%ICO%,0';" ^
  "$s.Save();" ^
  "Write-Host 'Saved:';" ^
  "Get-Item '%LNK%' | Select-Object -ExpandProperty FullName"

echo.
echo ============================================
echo   Desktop shortcut created:
echo   %LNK%
echo ============================================
endlocal