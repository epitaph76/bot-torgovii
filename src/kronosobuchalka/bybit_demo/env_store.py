from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


BYBIT_DEMO_API_KEY_ENV = "BYBIT_DEMO_API_KEY"
BYBIT_DEMO_API_SECRET_ENV = "BYBIT_DEMO_API_SECRET"
DEFAULT_ENV_PATH = ".env"
PLACEHOLDER_VALUES = {"put_demo_api_key_here", "put_demo_api_secret_here", "..."}

_ASSIGNMENT_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$")


@dataclass(frozen=True)
class EnvSnapshot:
    path: Path
    values: dict[str, str]

    @property
    def api_key_masked(self) -> str:
        return mask_secret(self.values.get(BYBIT_DEMO_API_KEY_ENV, ""))

    @property
    def api_secret_masked(self) -> str:
        return mask_secret(self.values.get(BYBIT_DEMO_API_SECRET_ENV, ""))

    @property
    def credentials_present(self) -> bool:
        return bool(is_real_secret(self.values.get(BYBIT_DEMO_API_KEY_ENV, "")) and is_real_secret(self.values.get(BYBIT_DEMO_API_SECRET_ENV, "")))


class EnvStore:
    def __init__(self, path: str | Path = DEFAULT_ENV_PATH):
        self.path = Path(path)

    def read(self) -> EnvSnapshot:
        return EnvSnapshot(path=self.path, values=self.read_values())

    def read_values(self) -> dict[str, str]:
        values: dict[str, str] = {}
        if not self.path.exists():
            return values
        for line in self.path.read_text(encoding="utf-8").splitlines():
            parsed = parse_env_line(line)
            if parsed is None:
                continue
            key, value = parsed
            values[key] = value
        return values

    def update_values(self, values: Mapping[str, str]) -> EnvSnapshot:
        current = self.read_values()
        for key, value in values.items():
            if value is not None:
                current[str(key)] = str(value)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"{key}={quote_env_value(value)}" for key, value in sorted(current.items())]
        self.path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        return EnvSnapshot(path=self.path, values=current)

    def update_bybit_demo_credentials(self, *, api_key: str, api_secret: str) -> EnvSnapshot:
        return self.update_values(
            {
                BYBIT_DEMO_API_KEY_ENV: api_key.strip(),
                BYBIT_DEMO_API_SECRET_ENV: api_secret.strip(),
            }
        )

    def apply_to_environ(self, environ: dict[str, str] | None = None) -> dict[str, str]:
        target = environ if environ is not None else os.environ
        for key, value in self.read_values().items():
            if is_real_secret(value) or key not in {BYBIT_DEMO_API_KEY_ENV, BYBIT_DEMO_API_SECRET_ENV}:
                target[key] = value
        return target


def parse_env_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    match = _ASSIGNMENT_RE.match(line)
    if not match:
        return None
    key, raw_value = match.groups()
    return key, unquote_env_value(raw_value.strip())


def unquote_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        body = value[1:-1]
        if value[0] == '"':
            return body.replace(r"\"", '"').replace(r"\\", "\\")
        return body
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    return value


def quote_env_value(value: str) -> str:
    if not value:
        return '""'
    if re.search(r"\s|#|\"|'", value):
        return '"' + value.replace("\\", r"\\").replace('"', r"\"") + '"'
    return value


def mask_secret(value: str, *, visible: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= visible * 2:
        return "*" * len(value)
    return f"{value[:visible]}...{value[-visible:]}"


def is_real_secret(value: str) -> bool:
    stripped = str(value or "").strip()
    return bool(stripped and stripped not in PLACEHOLDER_VALUES)


def redact_secrets(text: str, values: Mapping[str, str]) -> str:
    redacted = str(text)
    for value in values.values():
        if value:
            redacted = redacted.replace(value, mask_secret(value))
    return redacted
