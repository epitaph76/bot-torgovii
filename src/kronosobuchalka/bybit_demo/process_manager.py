from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .config import load_config
from .env_store import EnvStore


PROCESS_FILE_NAME = "process.json"
STOP_FILE_NAME = "stop.request"
LOG_FILE_NAME = "bot_live.log"


@dataclass(frozen=True)
class BotProcessStatus:
    running: bool
    pid: int | None
    stale: bool
    process_file: Path
    log_path: Path
    stop_path: Path
    started_at: str | None = None
    config_path: str | None = None
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "pid": self.pid,
            "stale": self.stale,
            "process_file": str(self.process_file),
            "log_path": str(self.log_path),
            "stop_path": str(self.stop_path),
            "started_at": self.started_at,
            "config_path": self.config_path,
            "dry_run": self.dry_run,
        }


class BotProcessManager:
    def __init__(self, *, config_path: str | Path):
        self.config_path = Path(config_path)
        self.root = project_root_for_config(self.config_path)
        if not self.config_path.is_absolute():
            self.config_path = self.root / self.config_path
        self.config = load_config(self.config_path)
        state_path = Path(self.config.state_path)
        self.data_dir = state_path.parent if state_path.is_absolute() else self.root / state_path.parent
        self.process_file = self.data_dir / PROCESS_FILE_NAME
        self.stop_path = self.data_dir / STOP_FILE_NAME
        self.log_path = self.data_dir / LOG_FILE_NAME
        self.env_store = EnvStore(self.root / ".env")

    def status(self) -> BotProcessStatus:
        payload = self._read_process_file()
        pid = _int_or_none(payload.get("pid")) if payload else None
        running = bool(pid and pid_exists(pid))
        stale = bool(pid and not running)
        return BotProcessStatus(
            running=running,
            pid=pid,
            stale=stale,
            process_file=self.process_file,
            log_path=Path(payload.get("log_path") or self.log_path) if payload else self.log_path,
            stop_path=Path(payload.get("stop_path") or self.stop_path) if payload else self.stop_path,
            started_at=str(payload.get("started_at") or "") or None if payload else None,
            config_path=str(payload.get("config_path") or self.config_path) if payload else str(self.config_path),
            dry_run=bool(payload.get("dry_run")) if payload else False,
        )

    def start(self, *, dry_run: bool = False) -> dict[str, Any]:
        current = self.status()
        if current.running:
            return {"status": "already_running", **current.as_dict()}
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self.stop_path.exists():
            self.stop_path.unlink()
        command = [
            str(daemon_python_executable(self.root)),
            "-m",
            "kronosobuchalka",
            "bybit-demo",
            "run",
            "--config",
            str(self.config_path),
            "--stop-file",
            str(self.stop_path),
        ]
        if dry_run:
            command.append("--dry-run")
        env = os.environ.copy()
        self.env_store.apply_to_environ(env)
        creationflags = 0
        startupinfo = None
        if os.name == "nt":
            creationflags = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0
        log_handle = self.log_path.open("w", encoding="utf-8")
        try:
            process = subprocess.Popen(
                command,
                cwd=str(self.root),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=log_handle,
                close_fds=True,
                creationflags=creationflags,
                startupinfo=startupinfo,
            )
        finally:
            log_handle.close()
        payload = {
            "pid": int(process.pid),
            "started_at": utcnow_iso(),
            "config_path": str(self.config_path),
            "dry_run": bool(dry_run),
            "log_path": str(self.log_path),
            "stop_path": str(self.stop_path),
            "command": command_without_secrets(command),
        }
        self._write_process_file(payload)
        return {"status": "started", **self.status().as_dict()}

    def stop(self, *, timeout_seconds: float = 15.0) -> dict[str, Any]:
        current = self.status()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.stop_path.write_text(utcnow_iso() + "\n", encoding="utf-8")
        deadline = time.monotonic() + max(float(timeout_seconds), 0.0)
        while current.running and time.monotonic() < deadline:
            time.sleep(0.25)
            current = self.status()
        if current.running:
            return {"status": "stop_requested", **current.as_dict()}
        if self.process_file.exists():
            self.process_file.unlink()
        return {"status": "stopped", **self.status().as_dict()}

    def tail_logs(self, *, lines: int = 120) -> list[str]:
        return tail_file(self.log_path, lines=lines)

    def _read_process_file(self) -> dict[str, Any]:
        if not self.process_file.exists():
            return {}
        try:
            payload = json.loads(self.process_file.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_process_file(self, payload: Mapping[str, Any]) -> None:
        self.process_file.parent.mkdir(parents=True, exist_ok=True)
        self.process_file.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2), encoding="utf-8")


def project_root_for_config(config_path: str | Path) -> Path:
    path = Path(config_path)
    if not path.is_absolute():
        path = Path.cwd() / path
    cursor = path.resolve().parent if path.exists() else Path.cwd().resolve()
    for candidate in (cursor, *cursor.parents):
        if (candidate / "pyproject.toml").exists() and (candidate / "src").exists():
            return candidate
    return Path.cwd().resolve()


def python_executable(root: Path) -> Path:
    if os.name == "nt":
        candidate = root / ".venv" / "Scripts" / "python.exe"
    else:
        candidate = root / ".venv" / "bin" / "python"
    return candidate if candidate.exists() else Path(sys.executable)


def daemon_python_executable(root: Path) -> Path:
    if os.name == "nt":
        venv_pythonw = root / ".venv" / "Scripts" / "pythonw.exe"
        if venv_pythonw.exists():
            return venv_pythonw
        current_pythonw = Path(sys.executable).with_name("pythonw.exe")
        if current_pythonw.exists():
            return current_pythonw
    return python_executable(root)


def pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _pid_exists_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_exists_windows(pid: int) -> bool:
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    process_query_limited_information = 0x1000
    handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == 259
    finally:
        kernel32.CloseHandle(handle)


def hide_current_console_window() -> bool:
    if os.name != "nt":
        return False
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()  # type: ignore[attr-defined]
        if not hwnd:
            return False
        ctypes.windll.user32.ShowWindow(hwnd, 0)  # type: ignore[attr-defined]
        return True
    except Exception:
        return False


def tail_file(path: str | Path, *, lines: int = 120) -> list[str]:
    file_path = Path(path)
    if not file_path.exists():
        return []
    count = max(int(lines), 1)
    raw = file_path.read_bytes()
    if not raw:
        return []
    encoding = "utf-8"
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        encoding = "utf-16"
    elif raw[:2000].count(b"\x00") >= max(len(raw[:2000]) // 10, 2):
        encoding = "utf-16-le"
    return raw.decode(encoding, errors="replace").splitlines(keepends=True)[-count:]


def command_without_secrets(command: list[str]) -> list[str]:
    return [str(part) for part in command]


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None
