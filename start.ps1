$NoTui = $false
if ($args -contains "-NoTui") {
  $NoTui = $true
}

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RepoRoot

$Python = "python"
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$ModelWeights = Join-Path $RepoRoot "kronos\weights\NeoQuasar__Kronos-base\model.safetensors"
$TokenizerWeights = Join-Path $RepoRoot "kronos\weights\NeoQuasar__Kronos-Tokenizer-base\model.safetensors"

if (-not (Test-Path $VenvPython)) {
  Write-Host "Создаю .venv..."
  & $Python -m venv .venv
}

Write-Host "Устанавливаю/обновляю локальный пакет..."
& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install --index-url https://download.pytorch.org/whl/cpu "torch>=2.0"
& $VenvPython -m pip install -e .

if ((-not (Test-Path $ModelWeights)) -or (-not (Test-Path $TokenizerWeights))) {
  Write-Host "Скачиваю веса Kronos..."
  & $VenvPython scripts\download_kronos_assets.py
}

if (-not (Test-Path ".env")) {
  Copy-Item ".env.example" ".env"
  Write-Host "Создан .env из .env.example. API-ключи можно вставить во вкладке Настройки."
}

if (-not $NoTui) {
  Write-Host "Открываю TUI бота..."
  & $VenvPython -m kronosobuchalka tui --config configs\bybit_demo_1x.example.json
} else {
  Write-Host "Установка готова. TUI не открыт, потому что передан -NoTui."
}
