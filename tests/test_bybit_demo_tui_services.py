from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kronosobuchalka import cli  # noqa: E402
from kronosobuchalka.bybit_demo import commands  # noqa: E402
from kronosobuchalka.bybit_demo.account import _safe_section  # noqa: E402
from kronosobuchalka.bybit_demo.env_store import (  # noqa: E402
    BYBIT_DEMO_API_KEY_ENV,
    BYBIT_DEMO_API_SECRET_ENV,
    EnvStore,
    is_real_secret,
    mask_secret,
    redact_secrets,
)
from kronosobuchalka.bybit_demo.process_manager import BotProcessManager  # noqa: E402
from kronosobuchalka.bybit_demo import tui  # noqa: E402


class FakePopen:
    def __init__(self, *args, **kwargs):
        self.pid = 43210
        self.args = args
        self.kwargs = kwargs


def make_repo(root: Path) -> Path:
    (root / "src").mkdir()
    (root / "configs").mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='bot-torgovii'\n", encoding="utf-8")
    config = root / "configs" / "bot.json"
    config.write_text(json.dumps({"state_path": "data/bybit_demo/state.sqlite3"}), encoding="utf-8")
    return config


class EnvStoreTests(unittest.TestCase):
    def test_env_store_reads_writes_masks_and_redacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            store = EnvStore(env_path)
            store.update_bybit_demo_credentials(api_key="abcd1234efgh", api_secret="secret value")

            snapshot = store.read()

            self.assertEqual(snapshot.values[BYBIT_DEMO_API_KEY_ENV], "abcd1234efgh")
            self.assertEqual(snapshot.values[BYBIT_DEMO_API_SECRET_ENV], "secret value")
            self.assertEqual(mask_secret("abcd1234efgh"), "abcd...efgh")
            self.assertNotIn("secret value", redact_secrets("token=secret value", snapshot.values))

    def test_apply_to_environ_does_not_require_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {"KEEP": "1"}
            EnvStore(Path(tmp) / ".env").apply_to_environ(env)
            self.assertEqual(env, {"KEEP": "1"})

    def test_placeholders_are_not_treated_as_credentials(self) -> None:
        self.assertFalse(is_real_secret("put_demo_api_key_here"))
        self.assertFalse(is_real_secret("put_demo_api_secret_here"))
        self.assertTrue(is_real_secret("real-looking-value"))


class ProcessManagerTests(unittest.TestCase):
    def test_start_writes_pid_file_without_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = make_repo(root)
            (root / ".env").write_text(
                "BYBIT_DEMO_API_KEY=visible_key\nBYBIT_DEMO_API_SECRET=visible_secret\n",
                encoding="utf-8",
            )
            with patch("kronosobuchalka.bybit_demo.process_manager.subprocess.Popen", FakePopen), patch(
                "kronosobuchalka.bybit_demo.process_manager.pid_exists", return_value=True
            ):
                manager = BotProcessManager(config_path=config)
                result = manager.start(dry_run=True)

            self.assertEqual(result["status"], "started")
            payload = json.loads((root / "data" / "bybit_demo" / "process.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["pid"], 43210)
            serialized = json.dumps(payload)
            self.assertNotIn("visible_key", serialized)
            self.assertNotIn("visible_secret", serialized)
            self.assertIn("--dry-run", payload["command"])

    @unittest.skipIf(os.name != "nt", "pythonw daemon launcher is Windows-specific")
    def test_start_prefers_pythonw_for_hidden_windows_daemon(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = make_repo(root)
            scripts = root / ".venv" / "Scripts"
            scripts.mkdir(parents=True)
            (scripts / "pythonw.exe").write_text("", encoding="utf-8")
            with patch("kronosobuchalka.bybit_demo.process_manager.subprocess.Popen", FakePopen), patch(
                "kronosobuchalka.bybit_demo.process_manager.pid_exists", return_value=True
            ):
                manager = BotProcessManager(config_path=config)
                manager.start(dry_run=False)

            payload = json.loads((root / "data" / "bybit_demo" / "process.json").read_text(encoding="utf-8"))
            self.assertTrue(str(payload["command"][0]).endswith("pythonw.exe"))

    def test_stop_creates_stop_request_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = make_repo(root)
            manager = BotProcessManager(config_path=config)
            manager.process_file.parent.mkdir(parents=True, exist_ok=True)
            manager.process_file.write_text(json.dumps({"pid": 123, "log_path": str(manager.log_path), "stop_path": str(manager.stop_path)}), encoding="utf-8")

            with patch("kronosobuchalka.bybit_demo.process_manager.pid_exists", return_value=False):
                result = manager.stop(timeout_seconds=0.01)

            self.assertEqual(result["status"], "stopped")
            self.assertTrue(manager.stop_path.exists())

    def test_tail_logs_returns_requested_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = make_repo(root)
            manager = BotProcessManager(config_path=config)
            manager.log_path.parent.mkdir(parents=True, exist_ok=True)
            manager.log_path.write_text("a\nb\nc\n", encoding="utf-8")
            self.assertEqual(manager.tail_logs(lines=2), [f"b{os.linesep}", f"c{os.linesep}"])

    def test_tail_logs_handles_utf16_powershell_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = make_repo(root)
            manager = BotProcessManager(config_path=config)
            manager.log_path.parent.mkdir(parents=True, exist_ok=True)
            manager.log_path.write_bytes("one\r\ntwo\r\n".encode("utf-16-le"))
            self.assertEqual(manager.tail_logs(lines=1), ["two\r\n"])


class CliParserTests(unittest.TestCase):
    def test_tui_parser_accepts_config(self) -> None:
        args = cli._parser().parse_args(["tui", "--config", "configs/x.json"])
        self.assertEqual(args.command, "tui")
        self.assertEqual(args.config, "configs/x.json")

    def test_daemon_parser_accepts_start_stop_status_logs(self) -> None:
        parser = cli._parser()
        self.assertEqual(parser.parse_args(["bybit-demo", "daemon", "start"]).daemon_command, "start")
        self.assertEqual(parser.parse_args(["bybit-demo", "daemon", "stop"]).daemon_command, "stop")
        self.assertEqual(parser.parse_args(["bybit-demo", "daemon", "status"]).daemon_command, "status")
        self.assertEqual(parser.parse_args(["bybit-demo", "daemon", "logs"]).daemon_command, "logs")

    def test_account_command_prints_json_error(self) -> None:
        args = cli._parser().parse_args(["bybit-demo", "account", "--config", "configs/x.json", "--json"])
        with patch("kronosobuchalka.bybit_demo.commands.BybitAccountService", side_effect=RuntimeError("missing keys")):
            out = StringIO()
            with redirect_stdout(out):
                code = commands._cmd_account(args)

        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())["error"], "missing keys")

    def test_safe_account_section_keeps_snapshot_alive_after_endpoint_error(self) -> None:
        section = _safe_section(lambda: (_ for _ in ()).throw(RuntimeError("range too large")))
        self.assertFalse(section["ok"])
        self.assertEqual(section["rows"], [])
        self.assertEqual(section["error"], "range too large")


class TuiSmokeTests(unittest.TestCase):
    @unittest.skipIf(tui._TEXTUAL_IMPORT_ERROR is not None, "Textual is not installed in this interpreter")
    def test_tui_app_constructs(self) -> None:
        app = tui.BotTuiApp(config_path=ROOT / "configs" / "bybit_demo_1x.example.json")
        self.assertTrue(str(app.config_path).endswith("bybit_demo_1x.example.json"))

    def test_process_button_selected_map_marks_current_state(self) -> None:
        self.assertEqual(tui.process_button_selected_map(running=True), {"start": True, "stop": False})
        self.assertEqual(tui.process_button_selected_map(running=False), {"start": False, "stop": True})

    @unittest.skipIf(tui._TEXTUAL_IMPORT_ERROR is not None, "Textual is not installed in this interpreter")
    def test_positions_render_as_human_table(self) -> None:
        text = render_rich(
            tui.positions_renderable(
                [
                    {
                        "symbol": "GRAMUSDT",
                        "side": "Buy",
                        "size": "12.5",
                        "avgPrice": "1.2345",
                        "markPrice": "1.25",
                        "unrealisedPnl": "3.21",
                        "leverage": "2",
                    }
                ],
                as_of="2026-07-29T12:00:00",
            )
        )

        self.assertIn("Текущие позиции", text)
        self.assertIn("GRAMUSDT", text)
        self.assertIn("лонг", text)
        self.assertNotIn('{"symbol"', text)

    @unittest.skipIf(tui._TEXTUAL_IMPORT_ERROR is not None, "Textual is not installed in this interpreter")
    def test_logs_render_json_events_as_human_rows(self) -> None:
        text = render_rich(
            tui.logs_renderable(
                [
                    '{"event":"heartbeat","now_utc":"2026-07-29T12:00:00","next_decision_utc":"2026-07-29T13:00:20","active_symbol":"GRAMUSDT","active_side":"long","dry_run":false}\n'
                ]
            )
        )

        self.assertIn("сигнал жизни", text)
        self.assertIn("следующая: 2026-07-29T13:00:20", text)
        self.assertNotIn('{"event"', text)

    @unittest.skipIf(tui._TEXTUAL_IMPORT_ERROR is not None, "Textual is not installed in this interpreter")
    def test_decisions_use_nested_decision_rank_not_stale_selected(self) -> None:
        text = render_rich(
            tui.decisions_table(
                [
                    {
                        "created_at": "2026-07-29T12:00:52",
                        "as_of": "2026-07-29T11:00:00",
                        "payload": {
                            "action": "cash",
                            "reason": "rank1_failed_entry_filter",
                            "selected": {"secid": "ZECUSDT", "final_side": "short"},
                            "decision": {
                                "selected": None,
                                "ranked_rows": [
                                    {
                                        "secid": "GRAMUSDT",
                                        "final_side": "long",
                                        "net_pred_move_bps": "10.89",
                                        "entry_selected": False,
                                    }
                                ],
                            },
                        },
                    }
                ]
            )
        )

        self.assertIn("в кэше", text)
        self.assertIn("лучший сигнал", text)
        self.assertIn("фильтры", text)
        self.assertIn("входа", text)
        self.assertIn("GRAMUSDT", text)
        self.assertIn("вход нет", text)
        self.assertNotIn("ZECUSDT", text)


def render_rich(renderable: object) -> str:
    from rich.console import Console

    out = StringIO()
    console = Console(file=out, force_terminal=False, color_system=None, width=240)
    console.print(renderable)
    return out.getvalue()


if __name__ == "__main__":
    unittest.main()
