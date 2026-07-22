# bot-torgovii

Autonomous Bybit Demo bot for the Kronos GRAM/ZEC strategy.

Default runtime in this repo is intentionally conservative:

- exchange: Bybit Demo only, `https://api-demo.bybit.com`;
- instruments: `GRAMUSDT` and `ZECUSDT`, `category=linear`;
- leverage: `1x`;
- one trading account, no subaccount split/rebalance;
- secrets are read only from environment variables;
- no mainnet endpoint is accepted by the runtime.

## Setup

```powershell
git clone https://github.com/epitaph76/bot-torgovii.git
cd bot-torgovii
.\scripts\bootstrap.ps1
copy .env.example.ps1 .env.ps1
```

Edit `.env.ps1`:

```powershell
$env:BYBIT_DEMO_API_KEY = "..."
$env:BYBIT_DEMO_API_SECRET = "..."
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

While running, the bot prints one JSON heartbeat every `heartbeat_seconds` with the next hourly decision time. Every decision log includes ranking, costs/spread estimates, market snapshots, exchange flatness, rebalance state, and submitted order/stop responses.

## Strategy

The strategy is the latest handoff baseline:

- rank by recomputed dynamic `net_pred_move_bps`;
- entry: `vol_ratio_168 >= 1.75` and `net_pred_move_bps >= 150`;
- flip: `hl_relevant_wick_ratio_168 >= 5.0` and `hl_range_ratio_168 >= 2.25`;
- hold decay: same symbol/side, `net >= 50`, `vol >= 0.75`;
- stop loss: `5%`, Bybit full-position trading stop.

Kronos history excludes the forming candle. The forming candle open is used as the live proxy for the backtest `actual_open`.

## Operational Notes

Bybit Demo may be region-blocked. If `preflight` returns a CloudFront `403`, run the bot from a VPS/VPN location where `https://api-demo.bybit.com` is available.

The API key needs `Read` and `Trade`. Withdrawal permission is not needed and should stay disabled.

Do not commit `.env.ps1`, `.env`, local config files, SQLite state, or downloaded Kronos weights.
