from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .client import DEMO_BASE_URL, BybitCredentials


@dataclass(frozen=True)
class KronosRuntimeConfig:
    code_dir: str = "kronos/model"
    weights_dir: str = "kronos/weights"
    model: str = "base"
    context_rows: int = 512
    pred_len: int = 1
    sample_count: int = 10
    temperature: float = 0.2
    top_p: float = 0.9
    device: str = "auto"


@dataclass(frozen=True)
class StrategyConfig:
    symbols: tuple[str, ...] = ("GRAMUSDT", "ZECUSDT")
    category: str = "linear"
    interval: str = "60"
    history_limit: int = 720
    entry_vol_ratio_168_min: float = 1.75
    entry_net_pred_move_bps_min: float = 150.0
    exit_net_pred_move_bps_min: float = 50.0
    exit_vol_ratio_168_min: float = 0.75
    hl_relevant_wick_ratio_168_min: float = 5.0
    hl_range_ratio_168_min: float = 2.25
    stop_loss_pct: float = 0.05
    sl_trigger_by: str = "LastPrice"
    commission_rate: float = 0.0005
    minimum_spread_pct: float = 0.0002
    maximum_spread_pct: float = 0.0030
    slippage_spread_multiplier: float = 0.5
    range_fallback_fraction: float = 0.05
    liquidity_lookback: int = 48
    liquidity_min_periods: int = 12
    liquidity_penalty_min: float = 0.5
    liquidity_penalty_max: float = 3.0


@dataclass(frozen=True)
class SleeveConfig:
    sleeve_id: str
    kind: str = "trade"
    target_weight: float = 0.0
    leverage: float = 1.0
    api_key_env: str = ""
    api_secret_env: str = ""
    member_id: str = ""
    account_type: str = "UNIFIED"
    dry_run_equity_usdt: float = 0.0

    @property
    def is_cash(self) -> bool:
        return self.kind == "cash" or self.leverage <= 0


@dataclass(frozen=True)
class SplitPortfolioConfig:
    rebalance_threshold_pp: float = 5.0
    coin: str = "USDT"
    sleeves: tuple[SleeveConfig, ...] = field(
        default_factory=lambda: (
            SleeveConfig("cash", kind="cash", target_weight=0.20, leverage=0.0, dry_run_equity_usdt=2000.0),
            SleeveConfig("sleeve_3x", target_weight=0.50, leverage=3.0, dry_run_equity_usdt=5000.0),
            SleeveConfig("sleeve_5x", target_weight=0.30, leverage=5.0, dry_run_equity_usdt=3000.0),
        )
    )


@dataclass(frozen=True)
class MasterAccountConfig:
    api_key_env: str = "BYBIT_DEMO_MASTER_API_KEY"
    api_secret_env: str = "BYBIT_DEMO_MASTER_API_SECRET"
    member_id: str = ""
    account_type: str = "UNIFIED"


@dataclass(frozen=True)
class BybitDemoBotConfig:
    environment: str = "bybit_demo"
    base_url: str = DEMO_BASE_URL
    public_base_url: str = DEMO_BASE_URL
    state_path: str = "data/bybit_demo/state.sqlite3"
    decision_delay_seconds: int = 20
    loop_sleep_seconds: int = 30
    recv_window: int = 5000
    request_timeout_seconds: float = 15.0
    request_retries: int = 2
    master_account: MasterAccountConfig = field(default_factory=MasterAccountConfig)
    split: SplitPortfolioConfig = field(default_factory=SplitPortfolioConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    kronos: KronosRuntimeConfig = field(default_factory=KronosRuntimeConfig)

    def validate(self) -> None:
        if self.environment != "bybit_demo":
            raise ValueError("environment must be 'bybit_demo'")
        if self.base_url.rstrip("/") != DEMO_BASE_URL:
            raise ValueError(f"base_url must be {DEMO_BASE_URL}")
        if self.public_base_url.rstrip("/") != DEMO_BASE_URL:
            raise ValueError(f"public_base_url must be {DEMO_BASE_URL}")
        if tuple(self.strategy.symbols) != ("GRAMUSDT", "ZECUSDT"):
            raise ValueError("strategy.symbols must be GRAMUSDT,ZECUSDT for this runtime")
        total_weight = sum(float(sleeve.target_weight) for sleeve in self.split.sleeves)
        if abs(total_weight - 1.0) > 1e-9:
            raise ValueError(f"split sleeve weights must sum to 1.0, got {total_weight}")
        trade_sleeves = [sleeve for sleeve in self.split.sleeves if not sleeve.is_cash]
        if not trade_sleeves:
            raise ValueError("at least one trade sleeve is required")
        if len({sleeve.sleeve_id for sleeve in self.split.sleeves}) != len(self.split.sleeves):
            raise ValueError("sleeve_id values must be unique")

    def master_credentials(self) -> BybitCredentials:
        return _credentials_from_env(self.master_account.api_key_env, self.master_account.api_secret_env)

    def sleeve_credentials(self, sleeve: SleeveConfig) -> BybitCredentials:
        return _credentials_from_env(sleeve.api_key_env, sleeve.api_secret_env)


def load_config(path: str | Path) -> BybitDemoBotConfig:
    with Path(path).open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    config = parse_config(raw)
    config.validate()
    return config


def parse_config(raw: Mapping[str, Any]) -> BybitDemoBotConfig:
    strategy = StrategyConfig(**_section(raw, "strategy", tuple_fields={"symbols"}))
    kronos = KronosRuntimeConfig(**_section(raw, "kronos"))
    master = MasterAccountConfig(**_section(raw, "master_account"))
    split_raw = _section(raw, "split")
    sleeves = tuple(SleeveConfig(**item) for item in split_raw.pop("sleeves", [])) if "sleeves" in split_raw else SplitPortfolioConfig().sleeves
    split = SplitPortfolioConfig(sleeves=sleeves, **split_raw)
    top = {key: value for key, value in raw.items() if key not in {"strategy", "kronos", "master_account", "split"}}
    return BybitDemoBotConfig(strategy=strategy, kronos=kronos, master_account=master, split=split, **top)


def _section(raw: Mapping[str, Any], key: str, *, tuple_fields: set[str] | None = None) -> dict[str, Any]:
    value = raw.get(key, {})
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} must be an object")
    out = dict(value)
    for field_name in tuple_fields or set():
        if field_name in out:
            out[field_name] = tuple(str(item).upper() for item in out[field_name])
    return out


def _credentials_from_env(api_key_env: str, api_secret_env: str) -> BybitCredentials:
    if not api_key_env or not api_secret_env:
        return BybitCredentials("", "")
    return BybitCredentials(os.getenv(api_key_env, "").strip(), os.getenv(api_secret_env, "").strip())
