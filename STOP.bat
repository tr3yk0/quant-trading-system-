@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
set "AI_TRADING_PROJECT_ROOT=%CD%"
title AI Trading Team - STOP

echo ================================================================
echo AI TRADING TEAM - STOP
echo ================================================================
if not exist .runtime mkdir .runtime
>".runtime\STOP" echo stop

echo Stop signal written. Waiting for the project Guardian to shut down cleanly...
set "GUARDIAN_PID="
if exist ".runtime\guardian.pid" for /f "usebackq delims=" %%P in (".runtime\guardian.pid") do set "GUARDIAN_PID=%%P"

if defined GUARDIAN_PID (
  for /l %%S in (1,1,20) do (
    tasklist /FI "PID eq !GUARDIAN_PID!" /FO CSV /NH 2>nul | findstr /C:"!GUARDIAN_PID!" >nul
    if errorlevel 1 goto guardian_stopped
    timeout /t 1 /nobreak >nul
  )
  tasklist /FI "PID eq !GUARDIAN_PID!" /FO CSV /NH 2>nul | findstr /C:"!GUARDIAN_PID!" >nul
  if not errorlevel 1 (
    echo Guardian has not exited after 20 seconds. Checking process identity before force-stopping...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=Get-CimInstance Win32_Process -Filter 'ProcessId = !GUARDIAN_PID!' -ErrorAction SilentlyContinue; if($p -and $p.CommandLine -match 'trading_team\.monitor\.guardian' -and $p.CommandLine.Contains($env:AI_TRADING_PROJECT_ROOT)){exit 0}else{exit 1}" >nul 2>nul
    if not errorlevel 1 (
      echo Force-stopping project Guardian process !GUARDIAN_PID! and its child processes...
      taskkill /PID !GUARDIAN_PID! /T /F >nul 2>nul
    ) else (
      echo PID !GUARDIAN_PID! no longer identifies this project's Guardian; leaving it untouched.
    )
  )
) else (
  echo No Guardian PID file found. Checking dashboard health...
)

goto check_dashboard

:guardian_stopped
echo Guardian has stopped cleanly.

:check_dashboard
timeout /t 2 /nobreak >nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r=Invoke-RestMethod -Uri 'http://127.0.0.1:8105/api/health' -TimeoutSec 2; if($r.status -eq 'ok'){exit 0}; exit 1 } catch { exit 1 }" >nul 2>nul
if not errorlevel 1 (
  echo [WARN] Something is still responding on port 8105.
  echo It was not force-killed because it may belong to another application. Check logs\guardian.log and the owning process.
) else (
  echo Dashboard is no longer responding on port 8105.
)
REM Stop Ollama only if this project START.bat created a PID marker for its service.
if exist ".runtime\ollama.pid" (
  set "OLLAMA_PID="
  for /f "usebackq delims=" %%P in (".runtime\ollama.pid") do set "OLLAMA_PID=%%P"
  if defined OLLAMA_PID (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=Get-CimInstance Win32_Process -Filter 'ProcessId = !OLLAMA_PID!' -ErrorAction SilentlyContinue; if($p -and $p.CommandLine -match 'ollama(\.exe)?"?\s+serve'){exit 0}else{exit 1}" >nul 2>nul
    if not errorlevel 1 (
      echo Stopping the Ollama service started by this project (PID !OLLAMA_PID!)...
      taskkill /PID !OLLAMA_PID! /T /F >nul 2>nul
    ) else (
      echo Ollama PID marker is stale or belongs to another process; leaving that process untouched.
    )
  )
  del /q ".runtime\ollama.pid" >nul 2>nul
)
echo Stop request completed. Your data\trading_team.db and .env were not deleted.
echo.
pause
