@echo off
setlocal EnableExtensions
title AV1 Queue Studio — Setup
cd /d "%~dp0"

echo.
echo ============================================================
echo    AV1 QUEUE STUDIO — SETUP
echo ============================================================
echo.
echo  This will download missing binaries, create the Python environment,
echo  and install VapourSynth plugins. Internet connection required.
echo  Re-run anytime; existing binaries/plugins are left alone.
echo  ^(Delete a binary under bin\ to force that piece to re-download.^)
echo.
echo  Working directory: %CD%
echo.

:: Prefer Windows "py" launcher when available (handles 3.12+ cleanly),
:: otherwise fall through to whatever `python` is on PATH.
where py >nul 2>&1
if %ERRORLEVEL%==0 (
  set "PY_HINT=py"
) else (
  set "PY_HINT=python"
)

where %PY_HINT% >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python 3.12+ was not found on PATH.
  echo.
  echo  Install Python from https://www.python.org/downloads/
  echo  During setup, enable "Add python.exe to PATH".
  echo  Then close this window and run setup.bat again.
  echo.
  goto :end_fail
)

echo [*] Starting setup via PowerShell...
echo.

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup.ps1"
set "SETUP_EXIT=%ERRORLEVEL%"

echo.
if not "%SETUP_EXIT%"=="0" (
  echo ============================================================
  echo  Setup FAILED  ^(exit code %SETUP_EXIT%^)
  echo ============================================================
  echo  Check the messages above. Fix the issue, then run setup.bat again.
  echo.
  goto :end_fail
)

echo ============================================================
echo  Setup finished successfully
echo ============================================================
echo.
echo  Next step: double-click  runGUI.bat
echo  Studio opens at http://localhost:8765  ^(tray icon in the system tray^)
echo.
pause
endlocal
exit /b 0

:end_fail
pause
endlocal
exit /b 1
