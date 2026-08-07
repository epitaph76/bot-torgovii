from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from .account import BybitAccountService
from .config import load_config
from .env_store import EnvStore
from .process_manager import BotProcessManager
from .runtime import BybitDemoRuntime


DEFAULT_CONFIG_PATH = "configs/bybit_demo_1x.example.json"


def add_bybit_demo_parser(subparsers: argparse._SubParsersAction) -> None:
    bybit = subparsers.add_parser("bybit-demo", help="Запуск и проверка Bybit Demo бота Kronos Tigrinya.")
    bybit_sub = bybit.add_subparsers(dest="bybit_command")

    run = bybit_sub.add_parser("run", help="Запустить Bybit Demo бота.")
    run.add_argument("--config", required=True)
    run.add_argument("--once", action="store_true", help="Выполнить один цикл решения и выйти.")
    run.add_argument("--dry-run", action="store_true", help="Считать решения без отправки приватных действий в Bybit.")
    run.add_argument("--rebalance-only", action="store_true", help="Только проверить/отправить rebalance transfers.")
    run.add_argument("--stop-file", default="", help="Файл мягкой остановки, который проверяет постоянный runtime.")
    run.set_defaults(func=_cmd_run)

    status = bybit_sub.add_parser("status", help="Показать сохраненный статус бота.")
    status.add_argument("--config", required=True)
    status.set_defaults(func=_cmd_status)

    preflight = bybit_sub.add_parser("preflight", help="Проверить Demo API, инструменты, баланс и позиции без ордеров.")
    preflight.add_argument("--config", required=True)
    preflight.set_defaults(func=_cmd_preflight)

    account = bybit_sub.add_parser("account", help="Показать read-only снимок аккаунта Bybit.")
    account.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    account.add_argument("--json", action="store_true", help="Печатать JSON.")
    account.add_argument("--history-limit", type=int, default=50)
    account.set_defaults(func=_cmd_account)

    daemon = bybit_sub.add_parser("daemon", help="Управлять фоновым процессом Bybit Demo бота.")
    daemon.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    daemon_sub = daemon.add_subparsers(dest="daemon_command", required=True)

    daemon_start = daemon_sub.add_parser("start", help="Запустить бота как скрытый фоновый процесс.")
    daemon_start.add_argument("--dry-run", action="store_true")
    daemon_start.set_defaults(func=_cmd_daemon_start)

    daemon_stop = daemon_sub.add_parser("stop", help="Попросить фоновый процесс мягко остановиться.")
    daemon_stop.add_argument("--timeout", type=float, default=15.0)
    daemon_stop.set_defaults(func=_cmd_daemon_stop)

    daemon_status = daemon_sub.add_parser("status", help="Показать статус фонового процесса.")
    daemon_status.set_defaults(func=_cmd_daemon_status)

    daemon_logs = daemon_sub.add_parser("logs", help="Показать хвост лога фонового процесса.")
    daemon_logs.add_argument("--lines", type=int, default=120)
    daemon_logs.set_defaults(func=_cmd_daemon_logs)


def handle_bybit_demo(args: argparse.Namespace) -> int:
    func = getattr(args, "func", None)
    if func is None:
        return 2
    return int(func(args))


def _cmd_run(args: argparse.Namespace) -> int:
    EnvStore().apply_to_environ()
    config = load_config(args.config)
    runtime = BybitDemoRuntime(config=config)
    if bool(args.once) or bool(args.rebalance_only):
        result = runtime.run_once(dry_run=bool(args.dry_run), rebalance_only=bool(args.rebalance_only))
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    runtime.run_forever(dry_run=bool(args.dry_run), stop_file=args.stop_file or None)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    runtime = BybitDemoRuntime(config=config)
    print(json.dumps(runtime.status(), ensure_ascii=False, indent=2, default=str))
    return 0


def _cmd_preflight(args: argparse.Namespace) -> int:
    EnvStore().apply_to_environ()
    config = load_config(args.config)
    runtime = BybitDemoRuntime(config=config)
    result = runtime.preflight()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if bool(result.get("ok")) else 1


def _cmd_account(args: argparse.Namespace) -> int:
    try:
        service = BybitAccountService(config_path=args.config)
        payload = service.snapshot(history_limit=int(args.history_limit))
    except Exception as exc:
        payload = {"ok": False, "error_type": type(exc).__name__, "error": str(exc)}
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 1
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def _cmd_daemon_start(args: argparse.Namespace) -> int:
    manager = BotProcessManager(config_path=args.config)
    print(json.dumps(manager.start(dry_run=bool(args.dry_run)), ensure_ascii=False, indent=2, default=str))
    return 0


def _cmd_daemon_stop(args: argparse.Namespace) -> int:
    manager = BotProcessManager(config_path=args.config)
    print(json.dumps(manager.stop(timeout_seconds=float(args.timeout)), ensure_ascii=False, indent=2, default=str))
    return 0


def _cmd_daemon_status(args: argparse.Namespace) -> int:
    manager = BotProcessManager(config_path=args.config)
    print(json.dumps(manager.status().as_dict(), ensure_ascii=False, indent=2, default=str))
    return 0


def _cmd_daemon_logs(args: argparse.Namespace) -> int:
    manager = BotProcessManager(config_path=args.config)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    sys.stdout.write("".join(manager.tail_logs(lines=int(args.lines))))
    return 0
