from __future__ import annotations

import argparse
import json
from typing import Sequence

from .config import load_config
from .runtime import BybitDemoRuntime


def add_bybit_demo_parser(subparsers: argparse._SubParsersAction) -> None:
    bybit = subparsers.add_parser("bybit-demo", help="Run or inspect the Bybit Demo GRAM/ZEC Kronos bot.")
    bybit_sub = bybit.add_subparsers(dest="bybit_command")

    run = bybit_sub.add_parser("run", help="Run the Bybit Demo bot.")
    run.add_argument("--config", required=True)
    run.add_argument("--once", action="store_true", help="Run one decision cycle and exit.")
    run.add_argument("--dry-run", action="store_true", help="Build decisions without submitting private Bybit actions.")
    run.add_argument("--rebalance-only", action="store_true", help="Only check/submit sleeve rebalance transfers.")
    run.set_defaults(func=_cmd_run)

    status = bybit_sub.add_parser("status", help="Print persisted bot status.")
    status.add_argument("--config", required=True)
    status.set_defaults(func=_cmd_status)

    preflight = bybit_sub.add_parser("preflight", help="Check Demo API access, instruments, balances, and positions without orders.")
    preflight.add_argument("--config", required=True)
    preflight.set_defaults(func=_cmd_preflight)


def handle_bybit_demo(args: argparse.Namespace) -> int:
    func = getattr(args, "func", None)
    if func is None:
        return 2
    return int(func(args))


def _cmd_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    runtime = BybitDemoRuntime(config=config)
    if bool(args.once) or bool(args.rebalance_only):
        result = runtime.run_once(dry_run=bool(args.dry_run), rebalance_only=bool(args.rebalance_only))
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    runtime.run_forever(dry_run=bool(args.dry_run))
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    runtime = BybitDemoRuntime(config=config)
    print(json.dumps(runtime.status(), ensure_ascii=False, indent=2, default=str))
    return 0


def _cmd_preflight(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    runtime = BybitDemoRuntime(config=config)
    result = runtime.preflight()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if bool(result.get("ok")) else 1
