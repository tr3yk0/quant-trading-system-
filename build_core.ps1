# build_core.ps1 — compile ice_grid_core.pyd for Python 3.12 (Windows MSVC)
# Usage (PowerShell):  .\build_core.ps1
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

Write-Host "=== ICE + Grid Core build ===" -ForegroundColor Cyan
Write-Host "Root: $Root"

$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
  Write-Host "ERROR: .venv not found. Create venv first." -ForegroundColor Red
  exit 1
}

& $VenvPython -m pip install -q pybind11 numpy
$PybindDir = & $VenvPython -c "import pybind11; print(pybind11.get_cmake_dir())"
Write-Host "pybind11 cmake: $PybindDir"

$BuildDir = Join-Path $Root "cpp_core\build"
New-Item -ItemType Directory -Force -Path $BuildDir | Out-Null
Set-Location $BuildDir

# Configure
cmake .. -G "Visual Studio 17 2022" -A x64 -Dpybind11_DIR="$PybindDir" 2>$null
if ($LASTEXITCODE -ne 0) {
  Write-Host "VS2022 generator failed — trying default generator..." -ForegroundColor Yellow
  cmake .. -Dpybind11_DIR="$PybindDir"
  if ($LASTEXITCODE -ne 0) { throw "cmake configure failed" }
}

cmake --build . --config Release
if ($LASTEXITCODE -ne 0) { throw "cmake build failed" }

# Locate .pyd / .so
$Artifact = Get-ChildItem -Recurse -Include "ice_grid_core*.pyd","ice_grid_core*.so" | Select-Object -First 1
if (-not $Artifact) { throw "Built module not found" }

$DestDir = Join-Path $Root "src\trading_team\strategies\ice_grid"
New-Item -ItemType Directory -Force -Path $DestDir | Out-Null
Copy-Item $Artifact.FullName (Join-Path $DestDir $Artifact.Name) -Force
# also copy plain name for import
Copy-Item $Artifact.FullName (Join-Path $DestDir "ice_grid_core$($Artifact.Extension)") -Force

Write-Host "Installed: $($Artifact.Name) -> $DestDir" -ForegroundColor Green
& $VenvPython -c "import sys; sys.path.insert(0, r'$DestDir'); import ice_grid_core as m; print('OK', m.__doc__[:60] if m.__doc__ else m)"
Write-Host "=== Build complete ===" -ForegroundColor Cyan
Set-Location $Root
