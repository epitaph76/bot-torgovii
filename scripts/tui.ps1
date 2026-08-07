$ErrorActionPreference = "Stop"

if (Test-Path ".venv\Scripts\python.exe") {
  .\.venv\Scripts\python.exe -m kronosobuchalka tui --config configs\bybit_demo_1x.example.json
} else {
  python -m kronosobuchalka tui --config configs\bybit_demo_1x.example.json
}
