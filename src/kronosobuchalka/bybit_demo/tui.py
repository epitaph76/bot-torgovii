from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .account import BybitAccountService
from .config import load_config
from .env_store import BYBIT_DEMO_API_KEY_ENV, BYBIT_DEMO_API_SECRET_ENV, EnvStore
from .process_manager import BotProcessManager, hide_current_console_window
from .runtime import BybitDemoRuntime, next_decision_datetime
from .state import BotStateStore

try:
    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal
    from textual.widgets import Button, Checkbox, Footer, Header, Input, Static, TabPane, TabbedContent
except Exception as exc:  # pragma: no cover - exercised only when dependency is absent
    _TEXTUAL_IMPORT_ERROR: Exception | None = exc
else:
    _TEXTUAL_IMPORT_ERROR = None


if _TEXTUAL_IMPORT_ERROR is None:

    class BotTuiApp(App[None]):
        CSS = """
        Screen {
            layout: vertical;
        }

        TabPane {
            padding: 1 2;
        }

        .toolbar {
            height: auto;
            margin-bottom: 1;
        }

        Button {
            margin-right: 1;
        }

        Button.process-selected {
            background: $surface;
            text-style: bold;
        }

        Button#start.process-selected {
            border: heavy $success;
            color: $success;
        }

        Button#stop.process-selected {
            border: heavy $error;
            color: $error;
        }

        Static {
            margin-bottom: 1;
        }

        Input {
            margin-bottom: 1;
        }

        #logs_text {
            height: 1fr;
            overflow-y: auto;
        }

        #settings_status {
            color: $text-muted;
        }
        """

        BINDINGS = [
            ("r", "refresh", "Обновить"),
            ("q", "quit", "Выход"),
        ]

        def __init__(self, *, config_path: str | Path):
            super().__init__()
            self.config_path = Path(config_path)
            self.manager = BotProcessManager(config_path=self.config_path)
            os.chdir(self.manager.root)
            self.config_path = self.manager.config_path
            self.env_store = EnvStore(self.manager.root / ".env")
            self.config = load_config(self.config_path)
            state_path = Path(self.config.state_path)
            self.state = BotStateStore(state_path if state_path.is_absolute() else self.manager.root / state_path)
            self.last_account: dict[str, Any] | None = None
            self.last_message = ""

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            with TabbedContent(initial="dashboard"):
                with TabPane("Обзор", id="dashboard"):
                    with Horizontal(classes="toolbar"):
                        yield Button("Запустить", id="start", variant="success")
                        yield Button("Остановить", id="stop", variant="error")
                        yield Button("Перезапустить", id="restart")
                        yield Button("Скрыть", id="hide")
                    yield Static("", id="dashboard_text")
                with TabPane("Баланс", id="account"):
                    with Horizontal(classes="toolbar"):
                        yield Button("Обновить баланс", id="refresh_account")
                    yield Static("", id="account_text")
                with TabPane("Позиции", id="positions"):
                    with Horizontal(classes="toolbar"):
                        yield Button("Обновить позиции", id="refresh_positions")
                    yield Static("", id="positions_text")
                with TabPane("История", id="history"):
                    with Horizontal(classes="toolbar"):
                        yield Button("Обновить историю", id="refresh_history")
                    yield Static("", id="history_text")
                with TabPane("Логи", id="logs"):
                    with Horizontal(classes="toolbar"):
                        yield Button("Обновить логи", id="refresh_logs")
                    yield Static("", id="logs_text")
                with TabPane("Настройки", id="settings"):
                    yield Static("", id="settings_status")
                    yield Input(placeholder="Новый Bybit Demo API key; пусто = оставить текущий", id="api_key")
                    yield Input(placeholder="Новый Bybit Demo API secret; пусто = оставить текущий", id="api_secret", password=True)
                    yield Checkbox("Запускать бота в dry-run режиме", id="dry_run_start")
                    with Horizontal(classes="toolbar"):
                        yield Button("Сохранить ключи", id="save_keys", variant="primary")
                        yield Button("Проверить доступ", id="preflight")
            yield Footer()

        def on_mount(self) -> None:
            self.refresh_local()
            if self.env_store.read().credentials_present:
                self.refresh_account()
            else:
                self.last_account = {"error": "Ключи Bybit Demo не заданы. Добавь их во вкладке Настройки или в файле .env.", "as_of_utc": utcnow_iso()}
                self.update_account_views()
            self.set_interval(5.0, self.refresh_local)

        def action_refresh(self) -> None:
            self.refresh_local()
            self.refresh_account()

        def on_button_pressed(self, event: Button.Pressed) -> None:
            button_id = event.button.id or ""
            try:
                if button_id == "start":
                    dry_run = bool(self.query_one("#dry_run_start", Checkbox).value)
                    self.last_message = json.dumps(self.manager.start(dry_run=dry_run), ensure_ascii=False)
                elif button_id == "stop":
                    self.last_message = json.dumps(self.manager.stop(), ensure_ascii=False)
                elif button_id == "restart":
                    self.manager.stop()
                    dry_run = bool(self.query_one("#dry_run_start", Checkbox).value)
                    self.last_message = json.dumps(self.manager.start(dry_run=dry_run), ensure_ascii=False)
                elif button_id == "hide":
                    if not self.manager.status().running:
                        dry_run = bool(self.query_one("#dry_run_start", Checkbox).value)
                        self.manager.start(dry_run=dry_run)
                    hide_current_console_window()
                    self.exit()
                    return
                elif button_id == "save_keys":
                    self.save_keys()
                elif button_id == "preflight":
                    self.run_preflight()
                elif button_id in {"refresh_account", "refresh_positions", "refresh_history"}:
                    self.refresh_account()
                elif button_id == "refresh_logs":
                    self.refresh_logs()
            except Exception as exc:
                self.last_message = f"{type(exc).__name__}: {exc}"
            self.refresh_local()

        def save_keys(self) -> None:
            api_key = self.query_one("#api_key", Input).value.strip()
            api_secret = self.query_one("#api_secret", Input).value.strip()
            updates = {}
            if api_key:
                updates[BYBIT_DEMO_API_KEY_ENV] = api_key
            if api_secret:
                updates[BYBIT_DEMO_API_SECRET_ENV] = api_secret
            if updates:
                self.env_store.update_values(updates)
                self.query_one("#api_key", Input).value = ""
                self.query_one("#api_secret", Input).value = ""
                self.last_message = "Ключи сохранены в .env"
            else:
                self.last_message = "Поля пустые: изменения ключей не сохранены"

        def run_preflight(self) -> None:
            self.env_store.apply_to_environ()
            runtime = BybitDemoRuntime(config=load_config(self.config_path))
            result = runtime.preflight()
            ok = "OK" if bool(result.get("ok")) else "ОШИБКА"
            self.last_message = json.dumps(preflight_summary(result, label=ok), ensure_ascii=False, default=str)

        def refresh_local(self) -> None:
            self.update_dashboard()
            self.update_settings()
            self.refresh_logs()
            if self.last_account is not None:
                self.update_account_views()

        def refresh_account(self) -> None:
            if not self.env_store.read().credentials_present:
                self.last_account = {"error": "Ключи Bybit Demo не заданы. Добавь их во вкладке Настройки или в файле .env.", "as_of_utc": utcnow_iso()}
                self.update_account_views()
                return
            try:
                service = BybitAccountService(config_path=self.config_path, env_path=self.env_store.path)
                self.last_account = service.snapshot(history_limit=50)
                self.last_message = "Данные аккаунта обновлены"
            except Exception as exc:
                self.last_account = {"error": f"{type(exc).__name__}: {exc}", "as_of_utc": utcnow_iso()}
            self.update_account_views()

        def update_dashboard(self) -> None:
            status = self.manager.status().as_dict()
            self.update_process_buttons(running=bool(status.get("running")))
            state = self.state.status()
            active = state.get("active_episode") or {}
            latest = state.get("latest_decision") or {}
            latest_payload = latest.get("payload") if isinstance(latest, Mapping) else {}
            target_dt = next_decision_datetime(delay_seconds=int(self.config.decision_delay_seconds))
            self.query_one("#dashboard_text", Static).update(
                dashboard_renderable(
                    status=status,
                    state=state,
                    active=active,
                    latest_payload=latest_payload if isinstance(latest_payload, Mapping) else {},
                    next_utc=target_dt,
                    environment=self.config.environment,
                    sizing=sizing_summary(self.config),
                    config_path=self.config_path,
                    log_path=status.get("log_path"),
                    last_message=self.last_message,
                )
            )

        def update_settings(self) -> None:
            snapshot = self.env_store.read()
            self.query_one("#settings_status", Static).update(settings_renderable(snapshot))

        def update_account_views(self) -> None:
            payload = self.last_account or {}
            if "error" in payload:
                panel = error_renderable(str(payload["error"]), as_of=payload.get("as_of_utc"))
                self.query_one("#account_text", Static).update(panel)
                self.query_one("#positions_text", Static).update(panel)
                self.query_one("#history_text", Static).update(panel)
                return
            wallet = payload.get("wallet", {}).get("coins", []) if isinstance(payload.get("wallet"), Mapping) else []
            positions = payload.get("positions", {}).get("rows", []) if isinstance(payload.get("positions"), Mapping) else []
            history = payload.get("local_history", {}) if isinstance(payload.get("local_history"), Mapping) else {}
            errors = account_section_errors(payload)
            closed_pnl = payload.get("closed_pnl", {}).get("rows", []) if isinstance(payload.get("closed_pnl"), Mapping) else []
            executions = payload.get("executions", {}).get("rows", []) if isinstance(payload.get("executions"), Mapping) else []
            self.query_one("#account_text", Static).update(wallet_renderable(wallet, as_of=payload.get("as_of_utc")))
            self.query_one("#positions_text", Static).update(positions_renderable(positions, as_of=payload.get("as_of_utc")))
            self.query_one("#history_text", Static).update(
                history_renderable(
                    decisions=history.get("decisions", []),
                    order_events=history.get("order_events", []),
                    closed_pnl=closed_pnl,
                    executions=executions,
                    errors=errors,
                )
            )

        def refresh_logs(self) -> None:
            lines = self.manager.tail_logs(lines=160)
            self.query_one("#logs_text", Static).update(logs_renderable(lines))

        def update_process_buttons(self, *, running: bool) -> None:
            for button_id, selected in process_button_selected_map(running=running).items():
                self.query_one(f"#{button_id}", Button).set_class(selected, "process-selected")


def run_tui(*, config_path: str | Path) -> int:
    if _TEXTUAL_IMPORT_ERROR is not None:
        print(f"Textual не установлен или не импортируется: {_TEXTUAL_IMPORT_ERROR}")
        print("Запусти установку заново: .\\start.ps1")
        return 1
    BotTuiApp(config_path=config_path).run()
    return 0


def dashboard_renderable(
    *,
    status: Mapping[str, Any],
    state: Mapping[str, Any],
    active: Mapping[str, Any],
    latest_payload: Mapping[str, Any],
    next_utc: datetime,
    environment: str,
    sizing: str,
    config_path: str | Path,
    log_path: Any,
    last_message: str = "",
) -> Any:
    running = bool(status.get("running"))
    table = key_value_table()
    table.add_row("Режим", Text(environment, style="cyan"))
    table.add_row("Бот", status_text(running))
    table.add_row("PID", Text(str(status.get("pid") or "-"), style="bold" if running else "dim"))
    table.add_row("Запущен", Text(format_empty(status.get("started_at"))))
    table.add_row("Dry-run", bool_text(status.get("dry_run")))
    table.add_row("Следующая операция UTC", Text(next_utc.isoformat(timespec="seconds"), style="bold cyan"))
    table.add_row("Следующая операция локально", Text(next_utc.replace(tzinfo=timezone.utc).astimezone().isoformat(timespec="seconds"), style="cyan"))
    table.add_row("Активная позиция", compact_active_position(active))
    table.add_row("Последнее действие", action_reason_text(latest_payload.get("action"), latest_payload.get("reason")))
    table.add_row("Сверка состояния", reconciliation_text(state.get("reconciliation")))
    table.add_row("Размер позиции", Text(sizing, style="bold yellow" if "весь" in sizing else "cyan"))
    table.add_row("Конфиг", Text(str(config_path), style="dim"))
    table.add_row("Лог", Text(format_empty(log_path), style="dim"))
    parts: list[Any] = [Panel(table, title="Обзор", border_style="green" if running else "red")]
    if last_message:
        parts.append(Panel(human_message_renderable(last_message), title="Последнее сообщение", border_style="yellow"))
    return Group(*parts)


def settings_renderable(snapshot: Any) -> Any:
    table = key_value_table()
    table.add_row(".env", Text(str(snapshot.path), style="dim"))
    table.add_row(BYBIT_DEMO_API_KEY_ENV, Text(snapshot.api_key_masked or "-", style="bold green" if snapshot.api_key_masked else "yellow"))
    table.add_row(BYBIT_DEMO_API_SECRET_ENV, Text(snapshot.api_secret_masked or "-", style="bold green" if snapshot.api_secret_masked else "yellow"))
    table.add_row("Ключи заданы", bool_text(snapshot.credentials_present))
    return Panel(table, title="Настройки доступа", border_style="cyan")


def sizing_summary(config: Any) -> str:
    sleeves = [sleeve for sleeve in getattr(getattr(config, "split", None), "sleeves", ()) if not bool(getattr(sleeve, "is_cash", False))]
    if not sleeves:
        return "нет торговых sleeves"
    parts = []
    for sleeve in sleeves:
        leverage = format_number(getattr(sleeve, "leverage", 0))
        cap = to_float(getattr(sleeve, "max_notional_usdt", 0))
        if cap and cap > 0:
            parts.append(f"{sleeve.sleeve_id}: до {format_number(cap)} USDT, плечо {leverage}x")
        else:
            parts.append(f"{sleeve.sleeve_id}: весь equity, плечо {leverage}x")
    return "; ".join(parts)


def process_button_selected_map(*, running: bool) -> dict[str, bool]:
    return {"start": bool(running), "stop": not bool(running)}


def wallet_renderable(rows: Any, *, as_of: Any = None) -> Any:
    rows = rows if isinstance(rows, list) else []
    table = Table(title="Баланс", expand=True)
    table.add_column("Монета", style="bold")
    table.add_column("Equity", justify="right", style="green")
    table.add_column("Wallet", justify="right")
    table.add_column("USD", justify="right")
    table.add_column("Доступно", justify="right")
    if not rows:
        table.add_row("-", "-", "-", "-", "-")
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        table.add_row(
            format_empty(row.get("coin")),
            format_number(row.get("equity")),
            format_number(row.get("walletBalance")),
            format_number(row.get("usdValue")),
            format_number(row.get("availableToWithdraw") or row.get("availableToBorrow")),
        )
    return with_refresh_note(table, as_of=as_of)


def positions_renderable(rows: Any, *, as_of: Any = None) -> Any:
    rows = rows if isinstance(rows, list) else []
    table = Table(title="Текущие позиции", expand=True)
    table.add_column("Символ", style="bold")
    table.add_column("Сторона")
    table.add_column("Размер", justify="right")
    table.add_column("Вход", justify="right")
    table.add_column("Mark", justify="right")
    table.add_column("PnL", justify="right")
    table.add_column("Плечо", justify="right")
    if not rows:
        table.add_row("-", Text("нет", style="dim"), "-", "-", "-", "-", "-")
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        size = to_float(row.get("size"))
        side = row.get("side") if size and abs(size) > 0 else ""
        pnl = row.get("unrealisedPnl") or row.get("unrealizedPnl")
        table.add_row(
            format_empty(row.get("symbol")),
            side_text(side),
            format_number(row.get("size")),
            format_number(row.get("avgPrice")),
            format_number(row.get("markPrice")),
            signed_number_text(pnl),
            format_leverage(row.get("leverage")),
        )
    return with_refresh_note(table, as_of=as_of)


def history_renderable(
    *,
    decisions: Any,
    order_events: Any,
    closed_pnl: Any,
    executions: Any,
    errors: list[str],
) -> Any:
    parts: list[Any] = []
    if errors:
        parts.append(Panel("\n".join(errors), title="Ошибки обновления", border_style="red"))
    parts.extend(
        [
            decisions_table(decisions),
            order_events_table(order_events),
            closed_pnl_table(closed_pnl),
            executions_table(executions),
        ]
    )
    return Group(*parts)


def decisions_table(rows: Any) -> Any:
    rows = rows if isinstance(rows, list) else []
    table = Table(title="Последние решения бота", expand=True)
    table.add_column("Создано", style="dim")
    table.add_column("Свеча")
    table.add_column("Действие")
    table.add_column("Причина")
    table.add_column("Выбор")
    table.add_column("Rank #1", overflow="fold")
    if not rows:
        table.add_row("-", "-", Text("нет данных", style="dim"), "-", "-", "-")
        return table
    for row in rows[:20]:
        if not isinstance(row, Mapping):
            continue
        payload = row.get("payload", {}) if isinstance(row.get("payload"), Mapping) else {}
        decision = decision_payload(payload)
        selected = decision.get("selected") if isinstance(decision.get("selected"), Mapping) else {}
        top_rank = first_rank(payload)
        table.add_row(
            format_empty(row.get("created_at")),
            format_empty(row.get("as_of")),
            action_text(payload.get("action") or decision.get("status")),
            reason_text(payload.get("reason") or decision.get("reason")),
            selected_text(selected),
            rank_text(top_rank),
        )
    return table


def order_events_table(rows: Any) -> Any:
    rows = rows if isinstance(rows, list) else []
    table = Table(title="События ордеров", expand=True)
    table.add_column("Время", style="dim")
    table.add_column("Действие")
    table.add_column("Символ")
    table.add_column("Сторона")
    table.add_column("Кол-во", justify="right")
    table.add_column("Sleeve", style="dim")
    if not rows:
        table.add_row("-", Text("нет событий", style="dim"), "-", "-", "-", "-")
        return table
    for row in rows[:30]:
        if not isinstance(row, Mapping):
            continue
        payload = row.get("payload", {}) if isinstance(row.get("payload"), Mapping) else {}
        table.add_row(
            format_empty(row.get("created_at")),
            action_text(row.get("action")),
            format_empty(payload.get("symbol")),
            side_text(payload.get("side") or payload.get("final_side")),
            format_number(payload.get("qty") or payload.get("size")),
            format_empty(row.get("sleeve_id")),
        )
    return table


def closed_pnl_table(rows: Any) -> Any:
    rows = rows if isinstance(rows, list) else []
    table = Table(title="Закрытые сделки Bybit", expand=True)
    table.add_column("Время", style="dim")
    table.add_column("Символ", style="bold")
    table.add_column("Сторона")
    table.add_column("Кол-во", justify="right")
    table.add_column("Вход", justify="right")
    table.add_column("Выход", justify="right")
    table.add_column("PnL", justify="right")
    if not rows:
        table.add_row("-", "-", Text("нет закрытых сделок", style="dim"), "-", "-", "-", "-")
        return table
    for row in rows[:20]:
        if not isinstance(row, Mapping):
            continue
        table.add_row(
            format_time_field(row.get("updatedTime") or row.get("createdTime")),
            format_empty(row.get("symbol")),
            side_text(row.get("side")),
            format_number(row.get("qty")),
            format_number(row.get("avgEntryPrice")),
            format_number(row.get("avgExitPrice")),
            signed_number_text(row.get("closedPnl")),
        )
    return table


def executions_table(rows: Any) -> Any:
    rows = rows if isinstance(rows, list) else []
    table = Table(title="Исполнения Bybit", expand=True)
    table.add_column("Время", style="dim")
    table.add_column("Символ", style="bold")
    table.add_column("Сторона")
    table.add_column("Тип")
    table.add_column("Кол-во", justify="right")
    table.add_column("Цена", justify="right")
    table.add_column("Комиссия", justify="right")
    if not rows:
        table.add_row("-", "-", Text("нет исполнений", style="dim"), "-", "-", "-", "-")
        return table
    for row in rows[:20]:
        if not isinstance(row, Mapping):
            continue
        table.add_row(
            format_time_field(row.get("execTime") or row.get("createdTime")),
            format_empty(row.get("symbol")),
            side_text(row.get("side")),
            format_empty(row.get("orderType") or row.get("execType")),
            format_number(row.get("execQty")),
            format_number(row.get("execPrice")),
            format_number(row.get("execFee")),
        )
    return table


def logs_renderable(lines: Any) -> Any:
    lines = lines if isinstance(lines, list) else []
    if not lines:
        return Panel(Text("Лог-файл пока пустой или не создан", style="dim"), title="Логи", border_style="cyan")
    table = Table(title="Логи", expand=True)
    table.add_column("Время", style="dim", no_wrap=True)
    table.add_column("Событие", no_wrap=True)
    table.add_column("Детали", overflow="fold")
    for raw_line in lines[-160:]:
        parsed = parse_log_line(str(raw_line).strip())
        table.add_row(parsed["time"], parsed["event"], parsed["details"])
    return table


def parse_log_line(line: str) -> dict[str, Any]:
    if not line:
        return {"time": "-", "event": Text("пусто", style="dim"), "details": Text("")}
    try:
        payload = json.loads(line)
    except Exception:
        return {"time": "-", "event": Text("текст", style="dim"), "details": Text(line)}
    if not isinstance(payload, Mapping):
        return {"time": "-", "event": Text("текст", style="dim"), "details": Text(str(payload))}
    event = str(payload.get("event") or "событие")
    if event == "heartbeat":
        details = " | ".join(
            item
            for item in [
                f"следующая: {format_empty(payload.get('next_decision_utc'))}",
                f"позиция: {format_empty(payload.get('active_symbol'))} {ru_side(payload.get('active_side'))}".strip(),
                f"dry-run: {ru_bool(payload.get('dry_run'))}",
            ]
            if item
        )
        return {"time": format_empty(payload.get("now_utc")), "event": Text("сигнал жизни", style="green"), "details": Text(details)}
    if event == "decision":
        details = action_reason_text(payload.get("action"), payload.get("reason"))
        return {"time": format_empty(payload.get("as_of") or payload.get("created_at")), "event": Text("решение", style="cyan"), "details": details}
    if "error" in event.lower() or "exception" in event.lower():
        return {"time": format_empty(payload.get("time") or payload.get("now_utc")), "event": Text(event, style="bold red"), "details": Text(readable_pairs(payload, skip={"event"}))}
    if event in {"shutdown", "stop_requested"}:
        return {"time": format_empty(payload.get("time") or payload.get("now_utc")), "event": Text(event, style="yellow"), "details": Text(readable_pairs(payload, skip={"event"}))}
    return {"time": format_empty(payload.get("time") or payload.get("now_utc") or payload.get("as_of")), "event": Text(event, style="magenta"), "details": Text(readable_pairs(payload, skip={"event"}))}


def error_renderable(message: str, *, as_of: Any = None) -> Any:
    table = key_value_table()
    if as_of:
        table.add_row("Время", Text(str(as_of), style="dim"))
    table.add_row("Ошибка", Text(message, style="bold red"))
    return Panel(table, title="Не удалось обновить данные", border_style="red")


def with_refresh_note(renderable: Any, *, as_of: Any = None) -> Any:
    if not as_of:
        return renderable
    return Group(renderable, Text(f"Обновлено UTC: {as_of}", style="dim"))


def key_value_table() -> Any:
    table = Table.grid(expand=True, padding=(0, 1))
    table.add_column(justify="right", style="bold")
    table.add_column(ratio=1)
    return table


def status_text(running: bool) -> Any:
    return Text("ЗАПУЩЕН", style="bold green") if running else Text("ОСТАНОВЛЕН", style="bold red")


def bool_text(value: Any) -> Any:
    return Text("да", style="green") if bool(value) else Text("нет", style="yellow")


def action_text(value: Any) -> Any:
    action = str(value or "-")
    style = "dim"
    lowered = action.lower()
    if lowered in {"buy", "sell", "open", "flip", "enter"}:
        style = "bold green"
    elif lowered in {"close", "exit", "stop"}:
        style = "yellow"
    elif lowered in {"cash", "hold", "skip"}:
        style = "cyan"
    elif "error" in lowered or "block" in lowered:
        style = "bold red"
    return Text(translate_action(action), style=style)


def reason_text(value: Any) -> Any:
    reason = str(value or "-")
    style = "dim" if reason == "-" else "white"
    if "error" in reason or "block" in reason:
        style = "bold red"
    elif "failed" in reason or "skip" in reason:
        style = "yellow"
    elif "entry" in reason or "hold" in reason:
        style = "cyan"
    return Text(translate_reason(reason), style=style)


def action_reason_text(action: Any, reason: Any) -> Any:
    text = Text()
    text.append(translate_action(str(action or "-")), style=action_text(action).style)
    if reason:
        text.append(" | ", style="dim")
        text.append(translate_reason(str(reason)), style=reason_text(reason).style)
    return text


def side_text(value: Any) -> Any:
    side = ru_side(value)
    if side == "лонг":
        return Text(side, style="bold green")
    if side == "шорт":
        return Text(side, style="bold red")
    return Text("нет", style="dim")


def signed_number_text(value: Any) -> Any:
    number = to_float(value)
    if number is None:
        return Text("-")
    if number > 0:
        return Text(format_number(number), style="green")
    if number < 0:
        return Text(format_number(number), style="red")
    return Text(format_number(number), style="dim")


def selected_text(selected: Any) -> Any:
    if not isinstance(selected, Mapping) or not selected:
        return Text("-", style="dim")
    symbol = selected.get("symbol") or selected.get("secid") or selected.get("asset") or "-"
    side = ru_side(selected.get("side") or selected.get("final_side") or selected.get("pred_side"))
    net = selected.get("net_bps") or selected.get("score_net_bps") or selected.get("expected_net_bps") or selected.get("net_pred_move_bps")
    label = f"{symbol} {side}".strip()
    if net not in (None, ""):
        label += f" / {format_number(net)} bps"
    return Text(label, style="green" if side == "лонг" else "red" if side == "шорт" else "white")


def rank_text(row: Any) -> Any:
    if not isinstance(row, Mapping) or not row:
        return Text("-", style="dim")
    symbol = row.get("symbol") or row.get("secid") or "-"
    side = ru_side(row.get("side") or row.get("final_side") or row.get("pred_side"))
    net = row.get("net_bps") or row.get("score_net_bps") or row.get("expected_net_bps") or row.get("net_pred_move_bps")
    entry_ok = row.get("entry_ok")
    if entry_ok is None:
        entry_ok = row.get("entry_selected")
    parts = [str(symbol)]
    if side:
        parts.append(side)
    if net not in (None, ""):
        parts.append(f"{format_number(net)} bps")
    if entry_ok is not None:
        parts.append("вход ок" if bool(entry_ok) else "вход нет")
    style = "white" if entry_ok is None else "green" if bool(entry_ok) else "yellow"
    return Text(" | ".join(parts), style=style)


def compact_active_position(active: Mapping[str, Any]) -> Any:
    if not isinstance(active, Mapping) or not active:
        return Text("нет", style="dim")
    symbol = active.get("symbol") or "-"
    side = ru_side(active.get("side"))
    target = active.get("target_timestamp") or active.get("target")
    label = f"{symbol} {side}".strip()
    if target:
        label += f" до {target}"
    return Text(label, style="green" if side == "лонг" else "red" if side == "шорт" else "white")


def reconciliation_text(value: Any) -> Any:
    if not isinstance(value, Mapping) or not value:
        return Text("нет данных", style="dim")
    status = str(value.get("status") or "-")
    reason = str(value.get("reason") or "-")
    updated_at = value.get("updated_at")
    style = "green" if status.lower() in {"ok", "synced"} else "yellow"
    if "error" in status.lower() or "error" in reason.lower() or "fail" in reason.lower():
        style = "bold red"
    suffix = f" / {reason}"
    if updated_at:
        suffix += f" / {updated_at}"
    return Text(f"{status}{suffix}", style=style)


def human_message_renderable(message: str) -> Any:
    try:
        payload = json.loads(message)
    except Exception:
        return Text(message)
    if not isinstance(payload, Mapping):
        return Text(str(payload))
    table = key_value_table()
    for key, value in payload.items():
        if key in {"process_file", "stop_path", "log_path", "config_path"}:
            style = "dim"
        elif key == "status":
            style = "green" if str(value) in {"started", "already_running", "stopped"} else "yellow"
        else:
            style = ""
        table.add_row(translate_key(key), message_value_text(key, value, style=style))
    return table


def first_rank(payload: Mapping[str, Any]) -> Any:
    for key in ("ranked_rows", "ranked", "ranks", "ranked_candidates", "candidates"):
        rows = payload.get(key)
        if isinstance(rows, list) and rows:
            return rows[0]
    decision = payload.get("decision")
    if isinstance(decision, Mapping):
        for key in ("ranked_rows", "ranked", "candidates"):
            rows = decision.get(key)
            if isinstance(rows, list) and rows:
                return rows[0]
    return {}


def decision_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    decision = payload.get("decision")
    return decision if isinstance(decision, Mapping) else {}


def preflight_summary(result: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        return {"status": label, "result": str(result)}
    checks = result.get("checks")
    checks = checks if isinstance(checks, list) else []
    failed = [row for row in checks if isinstance(row, Mapping) and not bool(row.get("ok"))]
    public_ok = sum(1 for row in checks if isinstance(row, Mapping) and str(row.get("name", "")).startswith("public_market:") and bool(row.get("ok")))
    wallet_ok = sum(1 for row in checks if isinstance(row, Mapping) and str(row.get("name", "")).startswith("private_wallet:") and bool(row.get("ok")))
    position_ok = sum(1 for row in checks if isinstance(row, Mapping) and str(row.get("name", "")).startswith("private_position:") and bool(row.get("ok")))
    symbols = result.get("symbols")
    symbols = symbols if isinstance(symbols, list) else []
    summary: dict[str, Any] = {
        "status": label,
        "ok": bool(result.get("ok")),
        "environment": result.get("environment"),
        "symbols": ", ".join(str(item) for item in symbols if item) or "-",
        "public_market_ok": public_ok,
        "wallet_ok": wallet_ok,
        "positions_ok": position_ok,
        "failed_checks": len(failed),
    }
    if failed:
        summary["first_error"] = failed[0].get("error") or failed[0].get("name")
    return summary


def message_value_text(key: Any, value: Any, *, style: str = "") -> Any:
    if isinstance(value, bool):
        return bool_text(value)
    if str(key) == "status":
        return Text(translate_status(str(value)), style=style)
    if str(key) in {"action", "daemon_command"}:
        return action_text(value)
    if str(key) == "reason":
        return reason_text(value)
    return Text(format_empty(value), style=style)


def translate_status(status: str) -> str:
    mapping = {
        "started": "запущен",
        "already_running": "уже запущен",
        "stopped": "остановлен",
        "stop_requested": "запрошена остановка",
        "OK": "OK",
        "ОШИБКА": "ОШИБКА",
    }
    return mapping.get(status, status)


def readable_pairs(payload: Mapping[str, Any], *, skip: set[str] | None = None) -> str:
    skip = skip or set()
    parts = []
    for key, value in payload.items():
        if key in skip:
            continue
        if isinstance(value, (dict, list)):
            parts.append(f"{translate_key(key)}: {summarize_nested(value)}")
        else:
            parts.append(f"{translate_key(key)}: {format_empty(value)}")
    return " | ".join(parts) if parts else "-"


def summarize_nested(value: Any) -> str:
    if isinstance(value, list):
        return f"{len(value)} строк"
    if isinstance(value, Mapping):
        preferred = []
        for key in ("symbol", "side", "action", "reason", "status", "retMsg", "error"):
            if key in value:
                preferred.append(f"{translate_key(key)}={format_empty(value.get(key))}")
        return ", ".join(preferred) if preferred else f"{len(value)} полей"
    return format_empty(value)


def format_time_field(value: Any) -> str:
    if value in (None, ""):
        return "-"
    token = str(value)
    try:
        if token.isdigit() and len(token) >= 12:
            return datetime.fromtimestamp(int(token) / 1000, tz=timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    except Exception:
        pass
    return token


def format_leverage(value: Any) -> str:
    if value in (None, ""):
        return "-"
    return f"{format_number(value)}x"


def format_number(value: Any, *, precision: int = 6) -> str:
    number = to_float(value)
    if number is None:
        return "-"
    if abs(number) >= 1000:
        return f"{number:,.2f}"
    if abs(number) >= 1:
        return f"{number:,.4f}".rstrip("0").rstrip(".")
    return f"{number:.{precision}f}".rstrip("0").rstrip(".") or "0"


def to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None


def format_empty(value: Any) -> str:
    if value in (None, ""):
        return "-"
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    return summarize_nested(value)


def translate_action(action: str) -> str:
    mapping = {
        "cash": "в кэше",
        "hold": "держать",
        "open": "открыть",
        "close": "закрыть",
        "flip": "перевернуть",
        "buy": "покупка",
        "sell": "продажа",
        "skip": "пропуск",
        "block": "блокировка",
        "-": "-",
    }
    return mapping.get(action.lower(), action)


def translate_reason(reason: str) -> str:
    mapping = {
        "rank1_failed_entry_filter": "лучший сигнал не прошёл фильтры входа",
        "no_candidates": "нет кандидатов",
        "already_active": "позиция уже активна",
        "request_or_symbol_config": "ошибка запроса или настроек символа",
        "preflight_failed": "проверка доступа не прошла",
        "-": "-",
    }
    return mapping.get(reason, reason.replace("_", " "))


def translate_key(key: Any) -> str:
    mapping = {
        "status": "Статус",
        "running": "Запущен",
        "pid": "PID",
        "stale": "Устаревший PID",
        "process_file": "Файл процесса",
        "log_path": "Лог",
        "stop_path": "Stop-файл",
        "started_at": "Запущен",
        "config_path": "Конфиг",
        "dry_run": "Dry-run",
        "action": "Действие",
        "reason": "Причина",
        "symbol": "Символ",
        "side": "Сторона",
        "error": "Ошибка",
        "retMsg": "Ответ Bybit",
        "ok": "OK",
        "environment": "Режим",
        "symbols": "Символы",
        "public_market_ok": "Публичный рынок OK",
        "wallet_ok": "Кошелёк OK",
        "positions_ok": "Позиции OK",
        "failed_checks": "Ошибок проверки",
        "first_error": "Первая ошибка",
    }
    return mapping.get(str(key), str(key))


def account_section_errors(payload: Mapping[str, Any]) -> list[str]:
    labels = {
        "wallet": "Баланс",
        "positions": "Позиции",
        "closed_pnl": "Закрытые PnL Bybit",
        "executions": "Исполнения Bybit",
    }
    out = []
    for key, label in labels.items():
        section = payload.get(key)
        if isinstance(section, Mapping) and section.get("ok") is False:
            out.append(f"{label}: ошибка обновления\n{section.get('error')}")
    return out


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def ru_bool(value: Any) -> str:
    return "да" if bool(value) else "нет"


def ru_side(value: Any) -> str:
    token = str(value or "").lower()
    if token in {"long", "buy"}:
        return "лонг"
    if token in {"short", "sell"}:
        return "шорт"
    return str(value or "")
