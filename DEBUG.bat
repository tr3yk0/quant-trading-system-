@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title AI Trading Team - DEBUG

echo ================================================================
echo AI TRADING TEAM - DEBUG / FOREGROUND STARTUP
echo ================================================================
echo This runs setup and starts Guardian in the foreground so errors stay visible.
echo The console is kept open after startup or failure.
echo.
set "AI_TRADING_DEBUG_WRAPPER=1"
call "%~dp0START.bat" --debug
set "DEBUG_RC=%ERRORLEVEL%"
set "AI_TRADING_DEBUG_WRAPPER="
echo.
echo Debug session returned exit code %DEBUG_RC%.
echo Logs are in: %CD%\logs
if exist "%CD%\logs\start.log" (
  echo.
  echo ---- Recent startup log ----
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Content -LiteralPath '%CD%\logs\start.log' -Tail 18" 2>nul
)
if exist "%CD%\logs\guardian.log" (
  echo.
  echo ---- Recent Guardian log ----
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Content -LiteralPath '%CD%\logs\guardian.log' -Tail 30" 2>nul
)
echo ================================================================
echo Press any key to close this debug window.
pause >nul
exit /b %DEBUG_RC%
