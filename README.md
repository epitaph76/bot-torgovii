# bot-torgovii

Autonomous Bybit Demo bot for the Kronos Tigrinya GRAM/ZEC/BTC/SOL strategy.

Default runtime in this repo is Demo-only:

- exchange: Bybit Demo only, `https://api-demo.bybit.com`;
- instruments: `GRAMUSDT`, `ZECUSDT`, `BTCUSDT`, and `SOLUSDT`, `category=linear`;
- leverage: `1x`;
- one trading account, no subaccount split/rebalance;
- position sizing uses the full Demo USDT equity by default (`max_notional_usdt: 0` disables the old fixed `$100` cap);
- secrets are read from local `.env` or environment variables;
- no mainnet endpoint is accepted by the runtime.

## Setup

```powershell
git clone https://github.com/epitaph76/bot-torgovii.git
cd bot-torgovii
.\start.ps1
```

`start.ps1` creates `.venv`, installs the package, downloads Kronos weights if they are missing, creates `.env` from `.env.example`, and opens the TUI. If PowerShell blocks script execution, run:

```cmd
start.cmd
```

On Linux/mini PC:

```bash
bash ./start.sh
```

You can paste keys inside the TUI Settings tab, or edit `.env` manually:

```dotenv
BYBIT_DEMO_API_KEY=...
BYBIT_DEMO_API_SECRET=...
```

PowerShell-only scripts still support `.env.ps1`, but the TUI and daemon use `.env`.

## TUI

Open the terminal UI:

```powershell
.\start.ps1
```

Or directly:

```powershell
.\.venv\Scripts\python.exe -m kronosobuchalka tui --config configs\bybit_demo_1x.example.json
```

The TUI can save API keys to `.env`, show account balance, open positions, local bot history, Bybit closed PnL/executions, logs, process status, and the next decision time. `Hide` starts the background bot if needed and exits the UI; reopen it later with the same `tui` command.

Background controls are also available from any terminal in the repo:

```powershell
.\.venv\Scripts\python.exe -m kronosobuchalka bybit-demo daemon start
.\.venv\Scripts\python.exe -m kronosobuchalka bybit-demo daemon status
.\.venv\Scripts\python.exe -m kronosobuchalka bybit-demo daemon logs --lines 120
.\.venv\Scripts\python.exe -m kronosobuchalka bybit-demo daemon stop
```

## Preflight

This checks config safety, Bybit Demo public instruments, wallet balance, and open positions. It does not submit orders.

```powershell
.\scripts\preflight_bybit_demo.ps1
```

## Dry Run

This downloads market data, runs Kronos, builds one decision, and submits no orders.

```powershell
.\scripts\dry_run_once.ps1
```

## Run

One real Demo decision cycle:

```powershell
.\.env.ps1
.\.venv\Scripts\python.exe -m kronosobuchalka bybit-demo run --config configs\bybit_demo_1x.example.json --once
```

Continuous Demo runtime:

```powershell
.\scripts\run_bybit_demo.ps1
```

While running, the bot prints one JSON heartbeat every `heartbeat_seconds` with the next hourly decision time. Every decision log includes ranking, costs/spread estimates, market snapshots, exchange flatness, rebalance state, and submitted order/stop responses. The daemon mode redirects this stream to `data/bybit_demo/bot_live.log`.

To cap position size again, set `split.sleeves[].max_notional_usdt` in `configs/bybit_demo_1x.example.json` to a positive USDT amount. `0` means no fixed cap: the bot sizes from the sleeve wallet equity and configured leverage.

## Strategy

The default config uses the latest fixed Tigrinya preset (`tigrinya_btc_optuna_1263`):

- rank GRAM/ZEC/BTC/SOL by recomputed dynamic `net_pred_move_bps`;
- trade only rank-1 when rank-1 passes its own symbol-specific entry filter;
- GRAM/ZEC/SOL can flip against Kronos on their own H/L-extreme thresholds; BTC never flips;
- active-hold keeps the current symbol/side while the active symbol has a next-hour row and passes its own hold thresholds;
- stop loss: `5%`, Bybit full-position trading stop;
- costs use `0.00068` commission per side plus dynamic spread/slippage.

Kronos history excludes the forming candle. The forming candle open is used as the live proxy for the backtest `actual_open`.

## Operational Notes

Bybit Demo may be region-blocked. If `preflight` returns a CloudFront `403`, run the bot from a VPS/VPN location where `https://api-demo.bybit.com` is available.

The API key needs `Read` and `Trade`. Withdrawal permission is not needed and should stay disabled.

Do not commit `.env.ps1`, `.env`, local config files, process files, SQLite state, logs, or downloaded Kronos weights.
