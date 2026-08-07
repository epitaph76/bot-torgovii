from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
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
class SymbolStrategyFilter:
    entry_net_pred_move_bps_min: float = 150.0
    entry_vol_ratio_lookback_hours: int = 168
    entry_vol_ratio_min: float = 1.75
    entry_execution_cost_bps_max: float | None = None
    exit_net_pred_move_bps_min: float = 50.0
    exit_vol_ratio_168_min: float = 0.75
    flip_enabled: bool = True
    hl_relevant_wick_ratio_168_min: float = 5.0
    hl_range_ratio_168_min: float = 2.25
    pred_relevant_wick_bps_min: float = 0.0
    pred_range_bps_min: float = 0.0


def _tigrinya_symbol_filters() -> dict[str, SymbolStrategyFilter]:
    return {
        "GRAMUSDT": SymbolStrategyFilter(
            entry_net_pred_move_bps_min=250.0,
            entry_vol_ratio_lookback_hours=168,
            entry_vol_ratio_min=1.0,
            entry_execution_cost_bps_max=90.0,
            exit_net_pred_move_bps_min=-29.04327270616809,
            exit_vol_ratio_168_min=0.47871711434769354,
            flip_enabled=True,
            hl_relevant_wick_ratio_168_min=2.4,
            hl_range_ratio_168_min=2.6,
            pred_relevant_wick_bps_min=475.0,
            pred_range_bps_min=425.0,
        ),
        "ZECUSDT": SymbolStrategyFilter(
            entry_net_pred_move_bps_min=225.0,
            entry_vol_ratio_lookback_hours=168,
            entry_vol_ratio_min=1.15,
            entry_execution_cost_bps_max=45.0,
            exit_net_pred_move_bps_min=-96.13795519570391,
            exit_vol_ratio_168_min=0.2368121471906064,
            flip_enabled=True,
            hl_relevant_wick_ratio_168_min=5.4,
            hl_range_ratio_168_min=1.4,
            pred_relevant_wick_bps_min=150.0,
            pred_range_bps_min=350.0,
        ),
        "BTCUSDT": SymbolStrategyFilter(
            entry_net_pred_move_bps_min=80.0,
            entry_vol_ratio_lookback_hours=72,
            entry_vol_ratio_min=1.0353848575059572,
            entry_execution_cost_bps_max=28.0,
            exit_net_pred_move_bps_min=-78.0846698099054,
            exit_vol_ratio_168_min=0.29448688328479167,
            flip_enabled=False,
        ),
        "SOLUSDT": SymbolStrategyFilter(
            entry_net_pred_move_bps_min=75.0,
            entry_vol_ratio_lookback_hours=168,
            entry_vol_ratio_min=1.7870197723635848,
            entry_execution_cost_bps_max=110.0,
            exit_net_pred_move_bps_min=1.4498890529524076,
            exit_vol_ratio_168_min=0.20183664376091726,
            flip_enabled=True,
            hl_relevant_wick_ratio_168_min=5.727852334173372,
            hl_range_ratio_168_min=3.0170374821005317,
            pred_relevant_wick_bps_min=312.38583620510633,
            pred_range_bps_min=437.0151611157405,
        ),
    }


@dataclass(frozen=True)
class StrategyConfig:
    symbols: tuple[str, ...] = ("GRAMUSDT", "ZECUSDT", "BTCUSDT", "SOLUSDT")
    ranking_mode: str = "rank1_only"
    category: str = "linear"
    interval: str = "60"
    history_limit: int = 720
    entry_vol_ratio_168_min: float = 1.75
    entry_net_pred_move_bps_min: float = 150.0
    exit_net_pred_move_bps_min: float = 50.0
    exit_vol_ratio_168_min: float = 0.75
    hl_relevant_wick_ratio_168_min: float = 5.0
    hl_range_ratio_168_min: float = 2.25
    entry_execution_cost_bps_max: float | None = None
    pred_relevant_wick_bps_min: float = 0.0
    pred_range_bps_min: float = 0.0
    flip_enabled: bool = True
    symbol_filters: Mapping[str, SymbolStrategyFilter | Mapping[str, Any]] = field(default_factory=_tigrinya_symbol_filters)
    stop_loss_pct: float = 0.05
    sl_trigger_by: str = "LastPrice"
    commission_rate: float = 0.00068
    minimum_spread_pct: float = 0.0002
    maximum_spread_pct: float = 0.0030
    slippage_spread_multiplier: float = 0.5
    range_fallback_fraction: float = 0.05
    liquidity_lookback: int = 48
    liquidity_min_periods: int = 12
    liquidity_penalty_min: float = 0.5
    liquidity_penalty_max: float = 3.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbols", tuple(str(symbol).upper() for symbol in self.symbols))
        object.__setattr__(self, "ranking_mode", _normalize_ranking_mode(self.ranking_mode))
        filters: dict[str, SymbolStrategyFilter] = {}
        for symbol, raw in dict(self.symbol_filters or {}).items():
            filters[str(symbol).upper()] = self._coerce_symbol_filter(raw)
        object.__setattr__(self, "symbol_filters", filters)

    def filter_for(self, symbol: str) -> SymbolStrategyFilter:
        return dict(self.symbol_filters).get(str(symbol).upper(), self._default_symbol_filter())

    def _default_symbol_filter(self) -> SymbolStrategyFilter:
        return SymbolStrategyFilter(
            entry_net_pred_move_bps_min=float(self.entry_net_pred_move_bps_min),
            entry_vol_ratio_lookback_hours=168,
            entry_vol_ratio_min=float(self.entry_vol_ratio_168_min),
            entry_execution_cost_bps_max=self.entry_execution_cost_bps_max,
            exit_net_pred_move_bps_min=float(self.exit_net_pred_move_bps_min),
            exit_vol_ratio_168_min=float(self.exit_vol_ratio_168_min),
            flip_enabled=bool(self.flip_enabled),
            hl_relevant_wick_ratio_168_min=float(self.hl_relevant_wick_ratio_168_min),
            hl_range_ratio_168_min=float(self.hl_range_ratio_168_min),
            pred_relevant_wick_bps_min=float(self.pred_relevant_wick_bps_min),
            pred_range_bps_min=float(self.pred_range_bps_min),
        )

    def _coerce_symbol_filter(self, raw: SymbolStrategyFilter | Mapping[str, Any]) -> SymbolStrategyFilter:
        if isinstance(raw, SymbolStrategyFilter):
            return raw
        if not isinstance(raw, Mapping):
            raise ValueError("strategy.symbol_filters values must be objects")
        data = asdict(self._default_symbol_filter())
        data.update(_flat_symbol_filter(raw))
        return SymbolStrategyFilter(**data)


@dataclass(frozen=True)
class SleeveConfig:
    sleeve_id: str
    kind: str = "trade"
    target_weight: float = 0.0
    leverage: float = 1.0
    max_notional_usdt: float = 0.0
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
    heartbeat_seconds: int = 300
    loop_sleep_seconds: int = 30
    recv_window: int = 15000
    request_timeout_seconds: float = 15.0
    request_retries: int = 4
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
        if not self.strategy.symbols:
            raise ValueError("strategy.symbols must not be empty")
        if len(set(self.strategy.symbols)) != len(self.strategy.symbols):
            raise ValueError("strategy.symbols values must be unique")
        if self.strategy.ranking_mode not in {"rank1_only", "rank_cascade"}:
            raise ValueError("strategy.ranking_mode must be 'rank1_only' or 'rank_cascade'")
        total_weight = sum(float(sleeve.target_weight) for sleeve in self.split.sleeves)
        if abs(total_weight - 1.0) > 1e-9:
            raise ValueError(f"split sleeve weights must sum to 1.0, got {total_weight}")
        trade_sleeves = [sleeve for sleeve in self.split.sleeves if not sleeve.is_cash]
        if not trade_sleeves:
            raise ValueError("at least one trade sleeve is required")
        if len({sleeve.sleeve_id for sleeve in self.split.sleeves}) != len(self.split.sleeves):
            raise ValueError("sleeve_id values must be unique")
        for sleeve in self.split.sleeves:
            if float(sleeve.max_notional_usdt) < 0:
                raise ValueError("sleeve max_notional_usdt must be non-negative")

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
    strategy_raw = _section(raw, "strategy", tuple_fields={"symbols"})
    filters_by_symbol = strategy_raw.pop("filters_by_symbol", None)
    if filters_by_symbol is not None and "symbol_filters" not in strategy_raw:
        strategy_raw["symbol_filters"] = filters_by_symbol
    strategy = StrategyConfig(**strategy_raw)
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


def _flat_symbol_filter(raw: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    entry = raw.get("entry", {})
    if isinstance(entry, Mapping):
        _copy_if_present(entry, out, "net_pred_move_bps_min", "entry_net_pred_move_bps_min")
        _copy_if_present(entry, out, "vol_ratio_lookback_hours", "entry_vol_ratio_lookback_hours")
        _copy_if_present(entry, out, "vol_ratio_min", "entry_vol_ratio_min")
        _copy_if_present(entry, out, "execution_cost_bps_max", "entry_execution_cost_bps_max")
    hold = raw.get("hold", {})
    if isinstance(hold, Mapping):
        _copy_if_present(hold, out, "net_pred_move_bps_min", "exit_net_pred_move_bps_min")
        _copy_if_present(hold, out, "vol_ratio_168_min", "exit_vol_ratio_168_min")
    flip = raw.get("flip", {})
    if isinstance(flip, Mapping):
        _copy_if_present(flip, out, "enabled", "flip_enabled")
        _copy_if_present(flip, out, "hl_relevant_wick_ratio_168_min", "hl_relevant_wick_ratio_168_min")
        _copy_if_present(flip, out, "hl_range_ratio_168_min", "hl_range_ratio_168_min")
        _copy_if_present(flip, out, "pred_relevant_wick_bps_min", "pred_relevant_wick_bps_min")
        _copy_if_present(flip, out, "pred_range_bps_min", "pred_range_bps_min")
    direct_aliases = {
        "entry_net_pred_move_bps_min": "entry_net_pred_move_bps_min",
        "entry_vol_ratio_lookback_hours": "entry_vol_ratio_lookback_hours",
        "entry_vol_ratio_min": "entry_vol_ratio_min",
        "entry_execution_cost_bps_max": "entry_execution_cost_bps_max",
        "execution_cost_bps_max": "entry_execution_cost_bps_max",
        "exit_net_pred_move_bps_min": "exit_net_pred_move_bps_min",
        "exit_vol_ratio_168_min": "exit_vol_ratio_168_min",
        "flip_enabled": "flip_enabled",
        "hl_relevant_wick_ratio_168_min": "hl_relevant_wick_ratio_168_min",
        "hl_range_ratio_168_min": "hl_range_ratio_168_min",
        "pred_relevant_wick_bps_min": "pred_relevant_wick_bps_min",
        "pred_range_bps_min": "pred_range_bps_min",
    }
    for source, target in direct_aliases.items():
        _copy_if_present(raw, out, source, target)
    return out


def _copy_if_present(source: Mapping[str, Any], target: dict[str, Any], source_key: str, target_key: str) -> None:
    if source_key in source and source[source_key] is not None:
        target[target_key] = source[source_key]


def _normalize_ranking_mode(value: Any) -> str:
    token = str(value or "rank1_only").strip().lower().replace("-", "_")
    aliases = {
        "rank1": "rank1_only",
        "rank_1": "rank1_only",
        "rank1_only": "rank1_only",
        "rank_1_only": "rank1_only",
        "old_rank1": "rank1_only",
        "old_rank1_with_btc": "rank1_only",
        "rankcascade": "rank_cascade",
        "rank_cascade": "rank_cascade",
        "cascade": "rank_cascade",
    }
    if token not in aliases:
        raise ValueError("strategy.ranking_mode must be 'rank1_only' or 'rank_cascade'")
    return aliases[token]


def _credentials_from_env(api_key_env: str, api_secret_env: str) -> BybitCredentials:
    if not api_key_env or not api_secret_env:
        return BybitCredentials("", "")
    return BybitCredentials(os.getenv(api_key_env, "").strip(), os.getenv(api_secret_env, "").strip())
