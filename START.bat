@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM Run directly in this console. Avoid nested CMD self-relaunch; explicit PAUSE prompts
REM below keep double-click startup errors visible instead of blinking closed.
cd /d "%~dp0"
if errorlevel 1 (
  echo [ERROR] Could not enter the project folder: "%~dp0"
  echo Check that the ZIP was fully extracted to a writable folder.
  pause
  exit /b 1
)
set "DEBUG_MODE=0"
if /i "%~1"=="--debug" set "DEBUG_MODE=1"
title AI Trading Team - START
set "DASHBOARD_URL=http://127.0.0.1:8105"
set "AI_TRADING_PROJECT_ROOT=%CD%"
set "START_LOG=%CD%\logs\start.log"
set "INSTALL_LOG=%CD%\logs\install.log"
set "GUARDIAN_LOG=%CD%\logs\guardian.log"
set "OLLAMA_LOG=%CD%\logs\ollama.log"

if not exist "pyproject.toml" (
  echo [ERROR] pyproject.toml was not found. Extract the project ZIP and run START.bat from the project root.
  goto failed
)
if not exist logs mkdir logs
if not exist data mkdir data
if not exist .runtime mkdir .runtime

echo ================================================================
echo AI TRADING TEAM v4.39.0 - INSTITUTIONAL QUANT TERMINAL
echo Root: %CD%
echo Time: %DATE% %TIME%
echo ================================================================
echo ==== START %DATE% %TIME% ==== > "%START_LOG%"
echo Root: %CD%>>"%START_LOG%"

REM Do not install/update dependencies underneath an already running server.
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r=Invoke-RestMethod -Uri 'http://127.0.0.1:8105/api/health' -TimeoutSec 2; if ($r.status -eq 'ok') { Write-Output $r.version; exit 0 }; exit 1 } catch { exit 1 }" > "%CD%\.runtime\health_existing.txt" 2>nul
if not errorlevel 1 (
  set "RUNNING_VERSION="
  set /p RUNNING_VERSION=<"%CD%\.runtime\health_existing.txt"
  echo [ERROR] A dashboard is already responding on port 8105. Detected version: !RUNNING_VERSION!
  echo Run STOP.bat from this project, wait for it to stop, then run START.bat again.
  echo Refusing to update Python packages while the application is running.
  del /q "%CD%\.runtime\health_existing.txt" >nul 2>nul
  goto failed
)
del /q "%CD%\.runtime\health_existing.txt" >nul 2>nul

REM A guardian can be starting before its HTTP endpoint is ready; detect its PID file.
if exist ".runtime\guardian.pid" (
  set "GUARDIAN_PID="
  for /f "usebackq delims=" %%P in (".runtime\guardian.pid") do set "GUARDIAN_PID=%%P"
  if defined GUARDIAN_PID (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=Get-CimInstance Win32_Process -Filter 'ProcessId = !GUARDIAN_PID!' -ErrorAction SilentlyContinue; if($p -and $p.CommandLine -match 'trading_team\.monitor\.guardian' -and $p.CommandLine.Contains($env:AI_TRADING_PROJECT_ROOT)){exit 0}else{exit 1}" >nul 2>nul
    if not errorlevel 1 (
      echo [ERROR] Guardian PID !GUARDIAN_PID! is already running.
      echo Run STOP.bat first; then run START.bat again.
      goto failed
    ) else (
      echo Removing a stale Guardian PID file left by a previous stopped/crashed run.
      del /q ".runtime\guardian.pid" >nul 2>nul
    )
  )
)

set "TRADING_DATA_DIR=%CD%\data"
set "TRADING_RUNTIME_DIR=%CD%\.runtime"
set "TRADING_LOG_DIR=%CD%\logs"
set "TRADING_DB=%CD%\data\trading_team.db"
set "DASHBOARD_HOST=127.0.0.1"
set "DASHBOARD_PORT=8105"
set "AUTO_OPEN_BROWSER=1"
set "AUTO_RECOVER=1"

if not exist ".env" if exist ".env.example" (
  copy /y ".env.example" ".env" >nul
  if not errorlevel 1 echo Created .env from .env.example; existing .env files are never overwritten.
)

REM Reuse a compatible 64-bit Python virtual environment; rebuild only if it is broken/unsupported.
set "VPY=%CD%\.venv\Scripts\python.exe"
set "REBUILD_VENV=0"
if not exist "%VPY%" set "REBUILD_VENV=1"
if exist "%VPY%" (
  "%VPY%" -c "import sys,struct; assert sys.version_info >= (3,12) and struct.calcsize('P') == 8" >nul 2>nul
  if errorlevel 1 set "REBUILD_VENV=1"
)
if "!REBUILD_VENV!"=="1" (
  echo.
  echo [1/6] Finding compatible 64-bit Python 3.12+...
  call :find_python
  if not defined PY (
    where winget >nul 2>nul
    if not errorlevel 1 (
      echo Python 3.12 was not found. Attempting to install it through Windows Package Manager...
      winget install --id Python.Python.3.12 -e --silent --accept-package-agreements --accept-source-agreements
      call :find_python
    )
  )
  if not defined PY (
    echo [ERROR] No compatible 64-bit Python 3.12+ was found and automatic installation was unavailable.
    echo Install Python 3.12 x64 from https://www.python.org/downloads/windows/ and enable the Python launcher, then retry START.bat.
    goto failed
  )
  echo Using host interpreter: !PY!
  if exist ".venv" rmdir /s /q ".venv"
  !PY! -m venv .venv
  if errorlevel 1 (
    echo [ERROR] Could not create .venv.
    goto failed
  )
  set "VPY=%CD%\.venv\Scripts\python.exe"
)
if not exist "%VPY%" (
  echo [ERROR] Virtual-environment Python not found: %VPY%
  goto failed
)
"%VPY%" -c "import sys,struct; assert sys.version_info >= (3,12) and struct.calcsize('P') == 8; print('Python',sys.version.split()[0], '- 64-bit')"
if errorlevel 1 (
  echo [ERROR] The virtual environment requires 64-bit Python 3.12 or newer.
  goto failed
)
"%VPY%" -m ensurepip --upgrade >> "%INSTALL_LOG%" 2>&1
"%VPY%" -m pip --version >nul 2>nul
if errorlevel 1 (
  echo [ERROR] pip is unavailable inside .venv. See logs\install.log.
  goto failed
)

REM Fingerprint pyproject.toml using a temp file. FOR /F command substitution around
REM a quoted executable fails when the project root contains spaces (e.g. AI Trading Team).
set "DEPS_HASH="
set "OLD_HASH="
if exist ".runtime\dependencies_hash.tmp" del /q ".runtime\dependencies_hash.tmp" >nul 2>nul
"%VPY%" -c "import hashlib,pathlib; print(hashlib.sha256(pathlib.Path('pyproject.toml').read_bytes()).hexdigest())" > ".runtime\dependencies_hash.tmp" 2>> "%START_LOG%"
if errorlevel 1 (
  echo [ERROR] Could not calculate dependency fingerprint.
  goto failed
)
if exist ".runtime\dependencies_hash.tmp" set /p "DEPS_HASH=" < ".runtime\dependencies_hash.tmp"
del /q ".runtime\dependencies_hash.tmp" >nul 2>nul
if not defined DEPS_HASH (
  echo [ERROR] Could not calculate dependency fingerprint.
  goto failed
)
if exist ".runtime\dependencies.sha256" for /f "usebackq delims=" %%H in (".runtime\dependencies.sha256") do set "OLD_HASH=%%H"
set "NEED_INSTALL=0"
if /i not "!OLD_HASH!"=="!DEPS_HASH!" set "NEED_INSTALL=1"
"%VPY%" -c "import dotenv,MetaTrader5,numpy,scipy,sklearn,pytest,importlib.util,pathlib; spec=importlib.util.find_spec('trading_team.main'); root=pathlib.Path.cwd().resolve(); assert spec is not None and spec.origin and pathlib.Path(spec.origin).resolve().is_relative_to(root / 'src')" >nul 2>nul
if errorlevel 1 set "NEED_INSTALL=1"

if "!NEED_INSTALL!"=="1" (
  echo.
  echo [2/6] Installing/updating ALL Python project dependencies...
  echo Includes project, MetaTrader5, NumPy, SciPy, scikit-learn, pytest and editable package registration.
  echo ==== PIP INSTALL %DATE% %TIME% ====>>"%INSTALL_LOG%"
  "%VPY%" -m pip install --upgrade pip setuptools wheel --disable-pip-version-check --retries 2 --timeout 15 >> "%INSTALL_LOG%" 2>&1
  if errorlevel 1 echo [WARN] pip/setuptools upgrade failed; trying the project install with the current installer.
  "%VPY%" -m pip install -e ".[dev,ml,quant,finance]" --disable-pip-version-check --retries 2 --timeout 15 >> "%INSTALL_LOG%" 2>&1
  if errorlevel 1 (
    echo [ERROR] Core dependency installation failed. Read logs\install.log for the detailed pip error.
    powershell -NoProfile -Command "Get-Content -LiteralPath '%INSTALL_LOG%' -Tail 35" 2>nul
    goto failed
  )
  "%VPY%" -c "import dotenv,MetaTrader5,numpy,scipy,sklearn,pytest,importlib.util,pathlib; spec=importlib.util.find_spec('trading_team.main'); root=pathlib.Path.cwd().resolve(); assert spec is not None and spec.origin and pathlib.Path(spec.origin).resolve().is_relative_to(root / 'src')"
  if errorlevel 1 (
    echo [ERROR] A required project dependency failed its import check. See logs\install.log.
    goto failed
  )
  >".runtime\dependencies.sha256" echo !DEPS_HASH!
  echo Python project dependencies installed successfully.
) else (
  echo.
  echo [2/6] Python dependencies already match pyproject.toml; skipping reinstall.
)

REM QuantLib is an optional native Python wheel; install it automatically, without a separate installer batch.
"%VPY%" -c "import QuantLib as ql; assert str(getattr(ql,'__version__',getattr(ql,'QL_VERSION',''))).startswith('1.44')" >nul 2>nul
if errorlevel 1 (
  echo.
  echo [3/6] Installing QuantLib 1.44 Python binding...
  "%VPY%" -m pip install --upgrade "QuantLib==1.44" --disable-pip-version-check --retries 1 --timeout 8 >> "%INSTALL_LOG%" 2>&1
  if errorlevel 1 (
    echo [WARN] QuantLib 1.44 wheel could not be installed. The dashboard will run; QuantLib pricing will report dependency unavailable.
    echo [WARN] QuantLib install failed at %DATE% %TIME%>>"%INSTALL_LOG%"
  ) else (
    "%VPY%" -c "import QuantLib as ql; print('QuantLib',getattr(ql,'__version__',getattr(ql,'QL_VERSION','installed')))"
  )
) else (
  echo [3/6] QuantLib 1.44 is already installed.
)

REM Optional native ICE accelerator: build automatically if CMake exists. Python fallback remains supported.
if not exist "src\trading_team\strategies\ice_grid\ice_grid_core*.pyd" (
  where cmake >nul 2>nul
  if not errorlevel 1 (
    set "NATIVE_HASH="
    set "OLD_NATIVE_HASH="
    if exist ".runtime\native_hash.tmp" del /q ".runtime\native_hash.tmp" >nul 2>nul
    "%VPY%" -c "import hashlib,pathlib; print(hashlib.sha256(pathlib.Path('pyproject.toml').read_bytes()+pathlib.Path('build_core.ps1').read_bytes()).hexdigest())" > ".runtime\native_hash.tmp" 2>> "%START_LOG%"
    if not errorlevel 1 if exist ".runtime\native_hash.tmp" set /p "NATIVE_HASH=" < ".runtime\native_hash.tmp"
    del /q ".runtime\native_hash.tmp" >nul 2>nul
    if exist ".runtime\native_build_attempted.sha256" for /f "usebackq delims=" %%H in (".runtime\native_build_attempted.sha256") do set "OLD_NATIVE_HASH=%%H"
    if defined NATIVE_HASH if /i not "!NATIVE_HASH!"=="!OLD_NATIVE_HASH!" (
      echo.
      echo [4/6] Checking/building optional native ICE/Grid accelerator...
      powershell -NoProfile -ExecutionPolicy Bypass -File "%CD%\build_core.ps1" >> "%CD%\logs\native_build.log" 2>&1
      set "NATIVE_RC=!ERRORLEVEL!"
      >".runtime\native_build_attempted.sha256" echo !NATIVE_HASH!
      if not "!NATIVE_RC!"=="0" (
        echo [WARN] Native ICE/Grid build unavailable; the tested Python fallback remains active. See logs\native_build.log.
      ) else (
        echo Native ICE/Grid accelerator built successfully.
      )
    ) else (
      echo [4/6] Native build already attempted for unchanged sources; using the Python backend.
    )
  ) else (
    echo [4/6] CMake not installed; using the supported Python ICE/Grid backend.
  )
) else (
  echo [4/6] Native ICE/Grid accelerator already present.
)

REM Preserve the optional Ollama workflow: start it if installed, but do not auto-download large models.
echo.
echo [5/6] Checking optional Ollama local-model service...
set "OLLAMA="
where ollama >nul 2>nul
if not errorlevel 1 set "OLLAMA=ollama"
if not defined OLLAMA if exist "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" set "OLLAMA=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"
if defined OLLAMA (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Invoke-WebRequest -Uri 'http://127.0.0.1:11434/api/tags' -UseBasicParsing -TimeoutSec 2 | Out-Null; exit 0 } catch { exit 1 }" >nul 2>nul
  if errorlevel 1 (
    echo Ollama found; starting its service in the background.
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$c=Get-Command -Name 'ollama.exe' -ErrorAction SilentlyContinue; $exe=$null; if($c){$exe=$c.Source}; if(-not $exe){$candidate=Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'; if(Test-Path $candidate){$exe=$candidate}}; if(-not $exe){exit 2}; $p=Start-Process -FilePath $exe -ArgumentList 'serve' -PassThru -WindowStyle Hidden -RedirectStandardOutput (Join-Path $env:TRADING_LOG_DIR 'ollama.log') -RedirectStandardError (Join-Path $env:TRADING_LOG_DIR 'ollama_error.log'); Set-Content -LiteralPath (Join-Path $env:TRADING_RUNTIME_DIR 'ollama.pid') -Value $p.Id; exit 0" >nul 2>nul
    if errorlevel 1 (
      echo [WARN] Could not start Ollama automatically. See logs and start Ollama manually only if Local AI is enabled.
    ) else (
      timeout /t 3 /nobreak >nul
    )
  ) else (
    echo Ollama service is already responding; STOP.bat will leave this externally started service running.
  )
  "%OLLAMA%" list >> "%CD%\logs\ollama_models.log" 2>&1
  echo Ollama models are not downloaded automatically. Local AI remains optional to avoid unexpected multi-GB downloads and CPU/RAM use.
) else (
  echo Ollama is not installed; core dashboard/quant features continue without it.
  echo Local AI requires the Ollama app and configured model files. It is not a Python project dependency.
)

REM Remove stale stop signal only after checking for an already-running instance.
if exist ".runtime\STOP" del /q ".runtime\STOP" >nul 2>nul
"%VPY%" -c "import trading_team; print('AI Trading Team package import OK')"
if errorlevel 1 (
  echo [ERROR] trading_team is not importable after setup.
  goto failed
)

if "!DEBUG_MODE!"=="1" (
  echo.
  echo [6/6] DEBUG MODE: starting Guardian in the foreground.
  echo Server log output will remain visible in this console.
  echo Dashboard URL: %DASHBOARD_URL%
  echo Press Ctrl+C to stop the debug session; STOP.bat is also available.
  echo.
  "%VPY%" -u -m trading_team.monitor.guardian
  set "APP_RC=!ERRORLEVEL!"
  echo.
  echo Guardian process ended with code !APP_RC!.
  echo Additional logs: logs\start.log, logs\install.log, logs\guardian.log
  exit /b !APP_RC!
)

echo.
echo [6/6] Starting supervised Guardian and dashboard...
start "AI Trading Team Guardian" /min cmd.exe /d /c ""%VPY%" -u -m trading_team.monitor.guardian >> "%GUARDIAN_LOG%" 2>&1"

echo Waiting for dashboard health check (up to 60 seconds)...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$until=(Get-Date).AddSeconds(60); while((Get-Date) -lt $until){ try { $r=Invoke-RestMethod -Uri 'http://127.0.0.1:8105/api/health' -TimeoutSec 2; if($r.status -eq 'ok'){ Write-Output ('Health OK; version '+$r.version); exit 0 } } catch {}; Start-Sleep -Seconds 1 }; exit 1"
if errorlevel 1 (
  echo [ERROR] Dashboard did not become healthy within 60 seconds.
  echo Read logs\guardian.log and logs\install.log, then run DEBUG.bat to view startup output.
  goto failed
)
echo.
echo ================================================================
echo AI TRADING TEAM IS READY
echo Dashboard: %DASHBOARD_URL%
echo Logs:      %CD%\logs
echo Stop with: STOP.bat
echo Debug with: DEBUG.bat
echo ================================================================
echo OK %DATE% %TIME%>>"%START_LOG%"
start "" "%DASHBOARD_URL%"
echo.
echo The supervised application continues running in the background.
if not defined AI_TRADING_DEBUG_WRAPPER (
  echo.
  echo Press any key to close this launcher window. The dashboard will keep running.
  pause >nul
)
exit /b 0

:find_python
set "PY="
where py >nul 2>nul
if not errorlevel 1 (
  py -3.12 -c "import sys,struct; assert sys.version_info >= (3,12) and struct.calcsize('P')==8" >nul 2>nul
  if not errorlevel 1 set "PY=py -3.12"
)
if not defined PY (
  where py >nul 2>nul
  if not errorlevel 1 (
    py -3.13 -c "import sys,struct; assert sys.version_info >= (3,12) and struct.calcsize('P')==8" >nul 2>nul
    if not errorlevel 1 set "PY=py -3.13"
  )
)
if not defined PY (
  where py >nul 2>nul
  if not errorlevel 1 (
    py -3 -c "import sys,struct; assert sys.version_info >= (3,12) and struct.calcsize('P')==8" >nul 2>nul
    if not errorlevel 1 set "PY=py -3"
  )
)
if not defined PY (
  where python >nul 2>nul
  if not errorlevel 1 (
    python -c "import sys,struct; assert sys.version_info >= (3,12) and struct.calcsize('P')==8" >nul 2>nul
    if not errorlevel 1 set "PY=python"
  )
)
if not defined PY (
  if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" -c "import sys,struct; assert sys.version_info >= (3,12) and struct.calcsize('P')==8" >nul 2>nul
    if not errorlevel 1 set "PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe""
  )
)
if not defined PY (
  if exist "%ProgramFiles%\Python312\python.exe" (
    "%ProgramFiles%\Python312\python.exe" -c "import sys,struct; assert sys.version_info >= (3,12) and struct.calcsize('P')==8" >nul 2>nul
    if not errorlevel 1 set "PY="%ProgramFiles%\Python312\python.exe""
  )
)
exit /b 0

:failed
echo.
echo ================================================================
echo START FAILED - read the error above and the logs in %CD%\logs
echo ================================================================
if not exist logs mkdir logs >nul 2>nul
echo FAILED %DATE% %TIME%>>"%START_LOG%" 2>nul
if exist "%INSTALL_LOG%" (
  echo.
  echo ---- Recent dependency-install log ----
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Content -LiteralPath '%INSTALL_LOG%' -Tail 22" 2>nul
)
if not defined AI_TRADING_DEBUG_WRAPPER (
  echo.
  echo Press any key to close this error window.
  pause >nul
)
exit /b 1
