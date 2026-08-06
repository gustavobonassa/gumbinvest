# Builds the Windows desktop app: the Python server (PyInstaller) wrapped by
# the Electron shell (electron-builder). Run from anywhere:
#
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1
#
# Output: desktop-shell\out\GumbInvest Setup <version>.exe

$ErrorActionPreference = "Stop"
$Packaging = $PSScriptRoot
$Root = Split-Path $Packaging -Parent
$Backend = Join-Path $Root "backend"
$Frontend = Join-Path $Root "frontend"
$Shell = Join-Path $Root "desktop-shell"
$BuildVenv = Join-Path $Packaging ".venv-build"

Write-Host "== 1/5 Frontend build =="
Push-Location $Frontend
try {
    if (-not (Test-Path "node_modules")) { npm ci; if ($LASTEXITCODE -ne 0) { throw "npm ci failed" } }
    npm run build
    if ($LASTEXITCODE -ne 0) { throw "npm run build failed" }
} finally { Pop-Location }

Write-Host "== 2/5 Build venv + icons =="
if (-not (Test-Path $BuildVenv)) { python -m venv $BuildVenv }
$Py = Join-Path $BuildVenv "Scripts\python.exe"
& $Py -m pip install --quiet --upgrade pip
& $Py -m pip install --quiet -r (Join-Path $Backend "requirements-build.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
& $Py (Join-Path $Packaging "make_icon.py")
if ($LASTEXITCODE -ne 0) { throw "icon generation failed" }

Write-Host "== 3/5 PyInstaller (server) =="
& $Py -m PyInstaller `
    --noconfirm `
    --distpath (Join-Path $Packaging "dist") `
    --workpath (Join-Path $Packaging "build") `
    (Join-Path $Packaging "GumbInvest.spec")
if ($LASTEXITCODE -ne 0) { throw "pyinstaller failed" }

Write-Host "== 4/5 Electron shell dependencies =="
Push-Location $Shell
try {
    if (-not (Test-Path "node_modules")) { npm install; if ($LASTEXITCODE -ne 0) { throw "npm install failed" } }

    Write-Host "== 5/5 electron-builder =="
    npx electron-builder --win
    if ($LASTEXITCODE -ne 0) { throw "electron-builder failed" }
} finally { Pop-Location }

Write-Host "Installer at $Shell\out\"
