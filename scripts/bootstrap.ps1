$ErrorActionPreference = "Stop"

if (-not (Test-Path ".venv")) {
  python -m venv .venv
}

.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe scripts\download_kronos_assets.py

Write-Host "Bootstrap complete."
Write-Host "Next: copy .env.example.ps1 to .env.ps1, fill Bybit Demo keys, then run scripts\\preflight_bybit_demo.ps1"
