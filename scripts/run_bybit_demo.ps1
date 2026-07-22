$ErrorActionPreference = "Stop"

if (Test-Path ".env.ps1") {
  . .\.env.ps1
}

if (Test-Path ".venv\Scripts\python.exe") {
  .\.venv\Scripts\python.exe -m kronosobuchalka bybit-demo run --config configs\bybit_demo_1x.example.json
} else {
  python -m kronosobuchalka bybit-demo run --config configs\bybit_demo_1x.example.json
}
