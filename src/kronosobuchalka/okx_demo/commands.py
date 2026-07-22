from __future__ import annotations

import argparse
import json

from .config import load_config
from .runtime import OkxDemoRuntime


def add_okx_demo_parser(subparsers: argparse._SubParsersAction) -> None:
    okx = subparsers.add_parser("okx-demo", help="Run or inspect the OKX Demo GRAM/ZEC Kronos bot.")
    okx_sub = okx.add_subparsers(dest="okx_command")

    run = okx_sub.add_parser("run", help="Run the OKX Demo bot.")
    run.add_argument("--config", required=True)
    run.add_argument("--once", action="store_true", help="Run one decision cycle and exit.")
    run.add_argument("--dry-run", action="store_true", help="Build decisions without submitting private OKX actions.")
    run.add_argument("--rebalance-only", action="store_true", help="Only check/submit sleeve rebalance transfers.")
    run.set_defaults(func=_cmd_run)

    status = okx_sub.add_parser("status", help="Print persisted bot status.")
    status.add_argument("--config", required=True)
    status.set_defaults(func=_cmd_status)


def handle_okx_demo(args: argparse.Namespace) -> int:
    func = getattr(args, "func", None)
    if func is None:
        return 2
    return int(func(args))


def _cmd_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    runtime = OkxDemoRuntime(config=config)
    if bool(args.once) or bool(args.rebalance_only):
        result = runtime.run_once(dry_run=bool(args.dry_run), rebalance_only=bool(args.rebalance_only))
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    runtime.run_forever(dry_run=bool(args.dry_run))
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    runtime = OkxDemoRuntime(config=config)
    print(json.dumps(runtime.status(), ensure_ascii=False, indent=2, default=str))
    return 0
