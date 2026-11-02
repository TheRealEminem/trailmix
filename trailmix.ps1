# Trailmix from a copy of this repository on Windows: the transcription worker for a server install, the same as
# ./trailmix worker on a Mac or Linux. (The Trailmix app for Windows is the installer on the Releases page.)
#
#   .\trailmix.ps1 worker     run the transcription worker on port 8770 with whisper.cpp (Ctrl+C stops it)
#
# Needs Python 3.11 or newer (python.org, or: winget install Python.Python.3.12). The first run sets up its Python
# environment in backend\.venv, with ffmpeg (from the imageio-ffmpeg package). If PowerShell won't run scripts:
#   powershell -ExecutionPolicy Bypass -File .\trailmix.ps1 worker
# TRAILMIX_WORKER_TOKEN, TRAILMIX_WORKER_PORT and TRAILMIX_WORKER_HOST work as they do on a Mac.
param([string]$Command = "")
# Python's own exit codes are checked below. (Not "Stop": Windows PowerShell would stop at any line Python
# writes to stderr.)
$Backend = Join-Path $PSScriptRoot "backend"
$Venv = Join-Path $Backend ".venv"
$Python = Join-Path $Venv "Scripts\python.exe"

function Say($Text) { Write-Host "> $Text" -ForegroundColor Green }
function Die($Text) { Write-Host "x $Text" -ForegroundColor Red; exit 1 }

# A Python 3.11 or newer: the py launcher's 3.12 first (the release Trailmix is tested with), then any.
function Find-Python {
  $candidates = @(@("py", "-3.12"), @("py", "-3"), @("python"))
  foreach ($c in $candidates) {
    if (-not (Get-Command $c[0] -ErrorAction SilentlyContinue)) { continue }
    $exe = $c[0]
    $pre = @($c | Select-Object -Skip 1)
    & $exe @pre -c "import sys; sys.exit(sys.version_info < (3, 11))" 2>$null
    if ($LASTEXITCODE -eq 0) { return , $c }
  }
  Die "Trailmix needs Python 3.11 or newer (winget install Python.Python.3.12)."
}

function Ensure-Worker {
  if (Test-Path $Python) {
    & $Python -c "import pywhispercpp, uvicorn, imageio_ffmpeg" 2>$null
    if ($LASTEXITCODE -eq 0) { return }
  }
  Say "Setting up the Python environment (first run only)..."
  $py = Find-Python
  $exe = $py[0]
  $pre = @($py | Select-Object -Skip 1)
  & $exe @pre -m venv $Venv
  if ($LASTEXITCODE -ne 0) { Die "Couldn't make the Python environment in $Venv" }
  & $Python -m pip install -q -r (Join-Path $Backend "requirements-whispercpp.txt") imageio-ffmpeg
  if ($LASTEXITCODE -ne 0) { Die "Couldn't install the worker's Python packages (above)." }
}

switch ($Command) {
  "worker" {
    Ensure-Worker
    Push-Location $Backend
    try { & $Python -m transcription_worker } finally { Pop-Location }
    exit $LASTEXITCODE
  }
  default {
    Write-Host "Usage: .\trailmix.ps1 worker    (the transcription worker for a server install; see docs/self-hosting.md)"
    if ($Command) { exit 1 }
  }
}
