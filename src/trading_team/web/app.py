from __future__ import annotations

import json
import os
import random
import sqlite3
import threading
import time
import traceback
import hashlib
from functools import wraps
from dataclasses import asdict, replace
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from trading_team.core.config import RiskConfig, AppConfig
from trading_team.core.resilience import BoundedTTLCache, retry_call
from trading_team.core.symbols import catalog_list, symbol_meta, parse_watchlist, list_broker_symbols, resolve_mt5_symbol
from trading_team.market.registry import InstrumentRegistry
from trading_team.market.desks import DESK_IDS, DESK_LABELS, MarketDirectorPool, decode_snapshot, encode_snapshot, snapshot_fingerprint
from trading_team.storage.orchestration import DurableScanQueue
from trading_team.core.models import Candle, MarketSnapshot
from trading_team.agents.risk import RiskAgent
from trading_team.adapters.execution import ExecutionAdapter
from trading_team.storage.journal import Journal
from trading_team.storage.state import AccountState
from trading_team.storage.positions import PositionStore
from trading_team.storage.orders import OrderStore
from trading_team.adapters.mt5_execution import MT5ExecutionAdapter, MT5ExecutionError
from trading_team.adapters.trade_engine import TradeEngine
from trading_team.data.account import AccountSync
from trading_team.monitor.notify import Notifier
from trading_team.monitor.lifecycle import TradeLifecycleManager
from trading_team.management.position_manager import PositionManager, ManagementConfig
from trading_team.storage.performance import PerformanceStore
from trading_team.data.mt5 import MT5DataFeed, assess_quote_freshness
from trading_team.data.mtf import MultiTimeframeFeed
from trading_team.analysis.structure import analyze_structure
from trading_team.ai_mesh import AgentMesh
from trading_team.core.paths import PROJECT_ROOT, DB_PATH as _DB_PATH_OBJ, RUNTIME_DIR, DATA_DIR
from trading_team.data.session import detect_session_from_mt5, session_confirmed_open
from trading_team.research.memory import ResearchMemory
from trading_team.research.evidence import EvidenceGate, StrategyDecay
from trading_team.research.run_cards import make_run_card, save_run_card, list_run_cards
from trading_team.research.shadow import profile_trades, extract_behavior_rules, load_trade_csv
from trading_team.research.self_improve import SelfImprovementAdvisor
from trading_team.research.progress import ProgressTrace
from trading_team.research.strategy_registry import StrategyRegistry
from trading_team.research.cross_market import run_directory
from trading_team.research.copilot import ResearchCopilot
from trading_team.data.news_rss import RSSNewsProvider
from trading_team.data.news_sentiment import analyze_news
from trading_team.data.news_analyst import NewsAnalystEngine
from trading_team.data.news_intelligence import (
    build_news_intelligence, enrich_news_items, assess_news_for_instrument,
)

VERSION = "4.39.0"
ROOT = Path(__file__).resolve().parent
PROJECT = PROJECT_ROOT
STATIC = ROOT / "static"
DB_PATH = str(_DB_PATH_OBJ)



class _TtlCache(BoundedTTLCache):
    """Shared thread-safe LRU cache with bounded memory and short stale fallback."""
    def __init__(self):
        try:
            max_entries = max(256, min(8192, int(os.getenv("RESPONSE_CACHE_MAX_ENTRIES", "8192"))))
        except (TypeError, ValueError):
            max_entries = 8192
        try:
            stale_ttl = max(0.0, float(os.getenv("RESPONSE_CACHE_STALE_TTL", "4.0")))
        except (TypeError, ValueError):
            stale_ttl = 4.0
        super().__init__(max_entries=max_entries, default_stale_ttl=stale_ttl)


_RESPONSE_CACHE = _TtlCache()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def env_num(name, default, cast=float):
    try:
        return cast(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return cast(default)


def _env_bool(name, default=False):
    return os.getenv(name, "1" if default else "0").lower() in {"1", "true", "yes", "on"}


def management_config_from_env():
    return ManagementConfig(
        enabled=_env_bool("POSITION_MANAGER_ENABLED", True),
        break_even_enabled=_env_bool("BREAK_EVEN_ENABLED", True),
        break_even_r=env_num("BREAK_EVEN_R", 1.0),
        break_even_offset_r=env_num("BREAK_EVEN_OFFSET_R", 0.05),
        trailing_enabled=_env_bool("TRAILING_ENABLED", True),
        trailing_start_r=env_num("TRAILING_START_R", 1.5),
        trailing_atr_mult=env_num("TRAILING_ATR_MULT", 1.6),
        trailing_use_structure=_env_bool("TRAILING_USE_STRUCTURE", True),
        partial_exit_enabled=_env_bool("PARTIAL_EXIT_ENABLED", True),
        partial_exit_r=env_num("PARTIAL_EXIT_R", 2.0),
        partial_exit_percent=env_num("PARTIAL_EXIT_PERCENT", 40.0),
        second_partial_exit_enabled=_env_bool("SECOND_PARTIAL_EXIT_ENABLED", True),
        second_partial_exit_r=env_num("SECOND_PARTIAL_EXIT_R", 3.5),
        second_partial_exit_percent=env_num("SECOND_PARTIAL_EXIT_PERCENT", 30.0),
        peak_giveback_enabled=_env_bool("PEAK_GIVEBACK_ENABLED", True),
        peak_giveback_trigger_r=env_num("PEAK_GIVEBACK_TRIGGER_R", 2.0),
        peak_giveback_r=env_num("PEAK_GIVEBACK_R", 0.9),
        time_stop_enabled=_env_bool("TIME_STOP_ENABLED", False),
        max_hold_minutes=env_num("MAX_HOLD_MINUTES", 240, int),
        time_stop_min_progress_r=env_num("TIME_STOP_MIN_PROGRESS_R", 0.25),
        close_if_unprotected=_env_bool("CLOSE_IF_UNPROTECTED", True),
        ai_invalidation_enabled=_env_bool("AI_INVALIDATION_ENABLED", True),
        ai_invalidation_confidence=env_num("AI_INVALIDATION_CONFIDENCE", 0.85),
        ai_invalidation_cycles=env_num("AI_INVALIDATION_CYCLES", 2, int),
    )


def safe_number(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _serialized_runtime_mt5(method):
    """Serialize runtime broker mutations with every other MT5 IPC operation."""
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        feed = getattr(self, "feed", None)
        api_lock = getattr(feed, "_api_lock", None) if feed is not None else None
        if api_lock is None:
            return method(self, *args, **kwargs)
        with api_lock:
            return method(self, *args, **kwargs)
    return wrapper


class Runtime:
    def __init__(self):
        self.lock = threading.RLock()
        # Settings changes must not wait for the long-running market-analysis lock.
        # This dedicated lock serializes edits/persistence only.
        self._settings_lock = threading.RLock()
        self.running = True
        self.paused = False
        self.kill_switch = True
        self.live_armed = False
        self.auto_trade = True  # paper + universe execution; disable only via settings
        # Restored after _saved_runtime_settings load (see below)
        self.interval = max(1, int(os.getenv("MONITOR_INTERVAL", "2")))
        self.config = AppConfig.from_env()
        _rs = os.getenv("RUNTIME_SETTINGS_FILE")
        if not _rs:
            _rd = os.getenv("TRADING_RUNTIME_DIR") or str(RUNTIME_DIR)
            _rs = str(Path(_rd) / "runtime_settings.json")
        self._runtime_settings_file = Path(_rs)

        self._saved_runtime_settings = self._load_runtime_settings()
        saved_symbol = str(self._saved_runtime_settings.get("symbol") or self.config.symbol).strip().upper()
        saved_symbols = self._saved_runtime_settings.get("symbols")
        self.watchlist: list[str] = [str(x).strip().upper() for x in (saved_symbols if isinstance(saved_symbols, list) and saved_symbols else self.config.symbols) if str(x).strip().upper() != "GBXUSD"]
        if saved_symbol == "GBXUSD":
            saved_symbol = next((x for x in self.watchlist if x), "EURUSD")
        if saved_symbol and saved_symbol.upper() != "GBXUSD" and saved_symbol not in self.watchlist:
            self.watchlist.insert(0, saved_symbol)
        self.config = AppConfig(
            paper_trading=self.config.paper_trading, symbol=saved_symbol or self.config.symbol,
            symbols=tuple(self.watchlist), timeframe=self.config.timeframe,
            lookback=max(100, min(2000, int(self._saved_runtime_settings.get("lookback", self.config.lookback)))),
            live_trading_enabled=self.config.live_trading_enabled, magic=self.config.magic,
            deviation=self.config.deviation, trade_cooldown_seconds=self.config.trade_cooldown_seconds,
            one_position_per_symbol=self.config.one_position_per_symbol, risk=self.config.risk,
        )
        self.interval = max(1, min(60, int(self._saved_runtime_settings.get("interval", self.interval))))
        self.analyze_all_symbols_enabled = bool(self._saved_runtime_settings.get("analyze_all_symbols_enabled", _env_bool("ANALYZE_ALL_SYMBOLS", True)))
        # Scan the full broker catalogue by default; 0 is the unlimited sentinel.
        raw_scan_limit = int(self._saved_runtime_settings.get("all_symbol_scan_limit", env_num("ALL_SYMBOL_SCAN_LIMIT", 0, int)))
        # Upgrade legacy defaults that imposed an arbitrary universe cap. Users can
        # still set any explicit non-legacy positive limit from Settings when needed.
        if raw_scan_limit in {1000, 5000}:
            raw_scan_limit = 0
        self.all_symbol_scan_limit = max(0, min(1_000_000, raw_scan_limit))
        raw_batch_size = int(self._saved_runtime_settings.get("all_symbol_scan_batch_size", env_num("ALL_SYMBOL_SCAN_BATCH_SIZE", 25, int)))
        self.all_symbol_scan_batch_size = max(1, min(100, 25 if raw_batch_size == 1 else raw_batch_size))
        self.all_symbol_rescan_seconds = max(10, min(3600, int(self._saved_runtime_settings.get("all_symbol_rescan_seconds", env_num("ALL_SYMBOL_RESCAN_SECONDS", 20, int)))))
        # When true, specialist BUY/SELL on secondary symbols may execute through
        # the same risk gateway as the primary symbol (paper or live-armed).
        self.trade_universe_enabled = bool(self._saved_runtime_settings.get(
            "trade_universe_enabled", _env_bool("TRADE_UNIVERSE_ENABLED", True)))
        self.trade_universe_max_per_cycle = max(1, min(200, int(self._saved_runtime_settings.get(
            "trade_universe_max_per_cycle", env_num("TRADE_UNIVERSE_MAX_PER_CYCLE", 15, int)))))
        self._universe_trades_this_minute = 0
        self._universe_trades_minute_key = ""

        self.debate_consensus_threshold = float(self._saved_runtime_settings.get(
            "debate_consensus_threshold", os.getenv("DEBATE_CONSENSUS_THRESHOLD", "0.48")))
        self.debate_consensus_threshold = max(0.40, min(0.85, self.debate_consensus_threshold))
        self.strong_research_override = float(self._saved_runtime_settings.get(
            "strong_research_override", os.getenv("STRONG_RESEARCH_OVERRIDE", "0.72")))
        self.strong_research_override = max(0.60, min(0.95, self.strong_research_override))
        os.environ["DEBATE_CONSENSUS_THRESHOLD"] = str(self.debate_consensus_threshold)
        os.environ["STRONG_RESEARCH_OVERRIDE"] = str(self.strong_research_override)

        # Restore mode controls from disk (user-editable settings)
        if "kill_switch" in self._saved_runtime_settings:
            self.kill_switch = bool(self._saved_runtime_settings.get("kill_switch"))
        if "live_armed" in self._saved_runtime_settings:
            self.live_armed = bool(self._saved_runtime_settings.get("live_armed"))
        if "auto_trade" in self._saved_runtime_settings:
            self.auto_trade = bool(self._saved_runtime_settings.get("auto_trade"))
        if "paused" in self._saved_runtime_settings:
            self.paused = bool(self._saved_runtime_settings.get("paused"))
        if "trade_universe_enabled" in self._saved_runtime_settings:
            self.trade_universe_enabled = bool(self._saved_runtime_settings.get("trade_universe_enabled"))
        if "trade_universe_max_per_cycle" in self._saved_runtime_settings:
            try:
                self.trade_universe_max_per_cycle = max(1, min(200, int(self._saved_runtime_settings.get("trade_universe_max_per_cycle"))))
            except (TypeError, ValueError):
                pass

        # Local LLM (Ollama) — opt-in by default to prevent continuous CPU contention.
        def _flag_saved(key, env_key, default=True):
            if key in self._saved_runtime_settings:
                return bool(self._saved_runtime_settings.get(key))
            return _env_bool(env_key, default)
        self.local_ai_enabled = _flag_saved("local_ai_enabled", "LOCAL_AI_ENABLED", False)
        self.local_ai_tiered = _flag_saved("local_ai_tiered", "LOCAL_AI_TIERED", True)
        self.local_ai_can_influence = _flag_saved("local_ai_can_influence_decision", "LOCAL_AI_CAN_INFLUENCE_DECISION", False)
        self.local_ai_base_url = str(self._saved_runtime_settings.get("local_ai_base_url") or os.getenv("LOCAL_AI_BASE_URL") or "http://127.0.0.1:11434/v1")
        self.local_ai_gate_model = str(self._saved_runtime_settings.get("local_ai_gate_model") or os.getenv("LOCAL_AI_GATE_MODEL") or "qwen2.5:1.5b")
        self.local_ai_deep_model = str(self._saved_runtime_settings.get("local_ai_deep_model") or os.getenv("LOCAL_AI_DEEP_MODEL") or os.getenv("LOCAL_AI_MODEL") or "qwen2.5:3b")
        os.environ["LOCAL_AI_ENABLED"] = "true" if self.local_ai_enabled else "false"
        os.environ["LOCAL_AI_TIERED"] = "true" if self.local_ai_tiered else "false"
        os.environ["LOCAL_AI_CAN_INFLUENCE_DECISION"] = "true" if self.local_ai_can_influence else "false"
        os.environ["LOCAL_AI_BASE_URL"] = self.local_ai_base_url
        os.environ["LOCAL_AI_GATE_MODEL"] = self.local_ai_gate_model
        os.environ["LOCAL_AI_DEEP_MODEL"] = self.local_ai_deep_model
        os.environ["LOCAL_AI_MODEL"] = self.local_ai_deep_model
        # The Settings toggle uses the bounded async local-agent path exclusively.
        # Legacy continuous guardian/chair and single-model research calls used to run
        # synchronously while refresh held the runtime lock, causing Settings/runtime
        # timeouts when LOCAL_AI_ENABLED was turned on.
        os.environ["HYBRID_LLM_ENRICH"] = "true" if self.local_ai_enabled else "false"
        try:
            _desk_limit = max(1, min(4, int(os.getenv("HYBRID_LLM_MAX_DESKS", "1") or 1)))
        except (TypeError, ValueError):
            _desk_limit = 1
        os.environ["HYBRID_LLM_MAX_DESKS"] = str(_desk_limit)
        os.environ["HYBRID_LLM_INCLUDE_NON_VOTING"] = "false"
        os.environ["CONTINUOUS_GUARDIAN_LLM"] = "false"
        os.environ["CONTINUOUS_CHAIR_LLM"] = "false"
        os.environ["LOCAL_AI_SYNC_RESEARCH"] = "false"

        # Backtest / optimize defaults (Settings + Backtest Lab)
        self.bt_symbol = str(self._saved_runtime_settings.get("bt_symbol") or self.config.symbol or "XAUUSD")
        self.bt_strategy = str(self._saved_runtime_settings.get("bt_strategy") or "trend_following")
        self.bt_bars = max(500, min(50000, int(self._saved_runtime_settings.get("bt_bars", 3000))))
        self.bt_equity = float(self._saved_runtime_settings.get("bt_equity", 10000))
        self.bt_risk_pct = float(self._saved_runtime_settings.get("bt_risk_pct", 0.5))
        self.bt_execution_model = str(self._saved_runtime_settings.get("bt_execution_model") or "ohlc")
        self.bt_objective = str(self._saved_runtime_settings.get("bt_objective") or "composite")
        self.bt_max_trials = max(5, min(200, int(self._saved_runtime_settings.get("bt_max_trials", 40))))
        self.bt_folds = max(2, min(12, int(self._saved_runtime_settings.get("bt_folds", 4))))
        self.bt_is_ratio = float(self._saved_runtime_settings.get("bt_is_ratio", 0.7))
        self.bt_commission_bps = float(self._saved_runtime_settings.get("bt_commission_bps", 0.0))

        # Apply saved trading mode to frozen AppConfig
        try:
            saved_mode = str(self._saved_runtime_settings.get("mode") or "").upper()
            saved_paper = self._saved_runtime_settings.get("paper_trading")
            saved_live_en = self._saved_runtime_settings.get("live_trading_enabled")
            if saved_mode in {"PAPER", "LIVE"}:
                saved_paper = saved_mode == "PAPER"
                if saved_mode == "LIVE":
                    saved_live_en = True
            if saved_paper is not None or saved_live_en is not None:
                paper = bool(saved_paper) if saved_paper is not None else self.config.paper_trading
                live_en = bool(saved_live_en) if saved_live_en is not None else self.config.live_trading_enabled
                if paper:
                    live_en = live_en  # allow flag independent in paper
                self.config = AppConfig(
                    paper_trading=paper,
                    symbol=self.config.symbol,
                    symbols=self.config.symbols,
                    timeframe=self.config.timeframe,
                    lookback=self.config.lookback,
                    live_trading_enabled=live_en,
                    magic=self.config.magic,
                    deviation=self.config.deviation,
                    trade_cooldown_seconds=self.config.trade_cooldown_seconds,
                    one_position_per_symbol=self.config.one_position_per_symbol,
                    risk_fraction=self.config.risk_fraction,
                    lifecycle_reconcile_seconds=self.config.lifecycle_reconcile_seconds,
                    risk=self.config.risk,
                )
        except Exception:
            pass

        # One sequential background mesh bounds DB connections regardless of universe size.
        # Position invalidation uses a separate mesh so scan and position workers cannot race.
        self._universe_mesh: AgentMesh | None = None
        self._position_mesh: AgentMesh | None = None
        self._universe_results: dict[str, dict] = {}
        self._universe_lock = threading.RLock()
        self._thread_start_lock = threading.RLock()
        self._analysis_thread = None
        self.scan_queue = DurableScanQueue(DB_PATH, lease_seconds=max(90, int(os.getenv("SYMBOL_SCAN_JOB_LEASE_SECONDS", "180"))))
        self.instrument_registry = InstrumentRegistry(DB_PATH)
        configured_desks = self._saved_runtime_settings.get("market_desks_enabled")
        if not isinstance(configured_desks, (list, tuple)):
            configured_desks = [x.strip().lower() for x in os.getenv("MARKET_DESKS_ENABLED", ",".join(DESK_IDS)).split(",") if x.strip()]
        self.market_desks_enabled = sorted({str(x).lower() for x in configured_desks if str(x).lower() in DESK_IDS}) or list(DESK_IDS)
        self.market_directors = MarketDirectorPool(
            enabled_desks=self.market_desks_enabled,
            factor_max_age_seconds=env_num("MARKET_DESK_FACTOR_MAX_AGE_SECONDS", 900, int),
        )
        self.market_directors.restore_stats(self.scan_queue.state_get("market_director_stats", {}))
        self.market_directors.hydrate_observations(self.scan_queue.results(limit=5000))
        self._registry_sync_seconds = max(30, int(os.getenv("INSTRUMENT_REGISTRY_SYNC_SECONDS", "300")))
        self._registry_last_sync_epoch = 0.0
        self._available_symbols_cache: tuple[float, tuple[Any, ...], list[str]] | None = None
        self._available_symbols_cache_seconds = max(5.0, min(120.0, env_num("UNIVERSE_CATALOG_CACHE_SECONDS", 20, float)))
        self._universe_cursor = int(self.scan_queue.state_get("broker_universe_cursor", 0) or 0)
        self._universe_worker_thread = None  # backward-compatible primary worker handle
        self._universe_worker_threads: list[threading.Thread] = []
        try:
            self.universe_worker_count = max(1, min(4, int(os.getenv("UNIVERSE_WORKERS", "2"))))
        except (TypeError, ValueError):
            self.universe_worker_count = 2
        self._universe_mesh_analysis_lock = threading.RLock()
        self._universe_worker_status = {
            "state": "NOT_STARTED", "worker_id": None, "last_started": None,
            "last_finished": None, "last_symbol": None, "last_error": None,
            "completed": 0, "failed": 0, "heartbeat_at": None, "progress_at": None,
            "blocked_reason": "Worker has not started", "stage": "STARTUP",
            "self_healing": {"attempts": 0, "successes": 0, "last_action": None, "last_error_fingerprint": None},
        }
        self._self_heal_lock = threading.RLock()
        self._self_heal_recent: dict[str, float] = {}
        for _stored_row in self.scan_queue.candidate_results(limit=2000):
            _stored_symbol = str(_stored_row.get("symbol") or "").upper()
            if _stored_symbol:
                _agent_views = _stored_row.pop("agent_views", [])
                _stored_row["agent_count"] = int(_stored_row.get("agent_count") or len(_agent_views or []))
                _stored_row.pop("agent_views", None)
                self._universe_results[_stored_symbol] = _stored_row
        self.universe_state: dict = {"enabled": self.analyze_all_symbols_enabled, "status": "Waiting for first scan",
                                     "total_available": 0, "analyzed_count": len(self._universe_results),
                                     "scan_limit": self.all_symbol_scan_limit or "ALL", "results": [],
                                     "registry": self.instrument_registry.summary(), "orchestration": self.scan_queue.stats()}
        self.last = None
        self.history: list[dict] = []
        self.candle_series: dict = {"M5": [], "M15": [], "H1": []}
        self.watchlist_quotes: dict[str, dict] = {}
        self._last_refresh_ts: float = 0.0
        self.mt5_connected: bool = False
        self.connection_detail: str = "Starting"
        # Unknown/offline is not treated as an executable market session.
        self.market_open: bool = False
        self.session_info: dict = {}
        self.errors: list[dict] = []
        self.events: list[dict] = []
        self.started = utc_now()
        self.feed = None
        self._portfolio_snapshot_last_attempt = 0.0
        self._portfolio_snapshot_last_error_log = 0.0
        self.demo_mode = False
        self.activity = "INITIALIZING"
        self.agent_status: dict[str, dict] = {}
        self.last_cycle_started = None
        self.last_cycle_finished = None
        self.last_cycle_ms = 0
        self.consecutive_errors = 0
        self.total_cycles = 0
        self.last_error_time = None
        self.next_retry_at = None
        self.reconnect_attempts = 0
        self.max_backoff = max(10, int(os.getenv("MAX_RECOVERY_BACKOFF", "60")))
        self.auto_recover = os.getenv("AUTO_RECOVER", "1").lower() not in {"0", "false", "no"}
        self.notifier = Notifier()
        self.account_sync = AccountSync(DB_PATH)
        self.account_live = False
        self.lifecycle = TradeLifecycleManager(DB_PATH, self.config.magic, self.config.deviation, auto_stop_repair=False)
        self.performance = PerformanceStore(DB_PATH)
        exit_cfg = management_config_from_env()
        saved_exit_cfg = self._saved_runtime_settings.get("exit_management")
        if isinstance(saved_exit_cfg, dict):
            allowed = set(ManagementConfig.__dataclass_fields__)
            try:
                exit_cfg = ManagementConfig(**{**{k: getattr(exit_cfg, k) for k in allowed}, **{k: v for k, v in saved_exit_cfg.items() if k in allowed}})
            except (TypeError, ValueError):
                pass
        self.management = PositionManager(DB_PATH, exit_cfg, self.config.magic, self.config.deviation)
        self.management_state: dict = {"actions": [], "alerts": [], "managed": []}
        self.lifecycle_state: dict = {"managed_open": [], "alerts": [], "events": [], "lifecycle_events": []}
        self.last_account_sync = None
        self.last_order_client_id = None
        self.last_execution = None
        self.trade_attempts = 0
        self.agent_mesh = AgentMesh()
        self.news_provider = RSSNewsProvider()
        # The news analyst consumes the same cached articles as the News Hub. Its own analysis
        # never performs provider I/O on the MT5 decision path; optional Ollama work is queued.
        self.news_analyst = NewsAnalystEngine()
        self.news_analysis_feed_cache: dict[str, Any] = {}
        self.news_items: list[dict] = []
        self.news_last_fetch = 0.0
        self.last_snapshot = None
        self.news_refresh_seconds = max(60, int(os.getenv("NEWS_REFRESH_SECONDS", "300")))
        self.research_memory = ResearchMemory(DB_PATH)
        self.strategy_registry = StrategyRegistry(DB_PATH)
        self.copilot = ResearchCopilot(self.agent_mesh.provider)
        # Ensure disk mirror exists so browser refresh / process restart keep settings
        try:
            self._persist_runtime_settings()
        except Exception:
            pass
        try:
            self.log_event("provider_ready", self.agent_mesh.provider.status())
        except Exception:
            pass
        self.active_strategy_registered = False
        self.evidence_gate = EvidenceGate(
            min_trades=int(os.getenv("EVIDENCE_MIN_TRADES","30")),
            min_days=int(os.getenv("EVIDENCE_MIN_DAYS","60")),
            min_sharpe=float(os.getenv("EVIDENCE_MIN_SHARPE","0.5")),
            max_dd_pct=float(os.getenv("EVIDENCE_MAX_DD_PCT","15")),
            stale_days=int(os.getenv("EVIDENCE_STALE_DAYS","45")),
        )
        self.strategy_decay = StrategyDecay()
        self.improvement_advisor = SelfImprovementAdvisor()
        self.strategy_id = os.getenv("STRATEGY_ID","xauusd_ai_mesh_v2")
        try:
            if not self.strategy_registry.list(5):
                self.strategy_registry.register("XAUUSD AI Mesh", "3.0.0", "PAPER", hypothesis="Evidence-gated multi-agent MT5 strategy")
            try:
                created = self.strategy_registry.seed_catalog()
                if created:
                    pass  # catalog strategies registered
            except Exception:
                pass
            self.active_strategy_registered=True
        except Exception:
            pass
        self.research_state: dict = {"strategy_id": self.strategy_id, "health": {}, "improvement": None, "memory_count": 0}
        self.shadow_state: dict = {"profile": None, "rules": [], "source": None}
        self.progress_state: list[dict] = []
        self.last_run_card: dict | None = None
        self.last_run_bar_key: str | None = None
        self.ai_mesh_state: dict = {}
        self.execution_diagnostics: list[dict] = []
        self._pipeline_lock = threading.RLock()
        self.pipeline_state: dict = {
            "cycle_id": 0, "symbol": self.config.symbol, "status": "WAITING",
            "started_at": None, "updated_at": utc_now().isoformat(), "finished_at": None,
            "decision": "WAIT", "confidence": 0.0, "risk_status": "NOT_EVALUATED",
            "execution_status": "NOT_ATTEMPTED", "live_order_verified": False,
            "stages": [], "blockers": ["No completed analysis cycle yet."],
        }
        # Defer first analysis cycle to the background loop so the HTTP server starts immediately
        self.activity = "INITIALIZING — STARTING ENGINE LOOP"
        self.connection_detail = "Waiting for first cycle"

    def log_event(self, typ: str, payload: dict):
        event = {"time": utc_now().isoformat(), "type": typ, "payload": payload}
        with self.lock:
            self.events.insert(0, event)
            self.events = self.events[:150]
        try:
            Journal(DB_PATH).write({"type": typ, **payload})
        except Exception:
            pass

    def set_activity(self, message: str):
        self.activity = message
        self.log_event("activity", {"message": message})

    def error(self, message: str, detail: str | None = None):
        item = {"time": utc_now().isoformat(), "message": str(message), "detail": detail or ""}
        with self.lock:
            self.errors.insert(0, item)
            self.errors = self.errors[:80]
        self.log_event("system_error", {"message": str(message)})

    def _no_market_error(self, symbol: str, reason: str, *, terminal_alive: bool = False):
        """Record that live market data is unavailable (no synthetic fallback).

        When terminal_alive=True the MT5 process itself is healthy; only the
        requested symbol (or quote) failed. Keep mt5_connected=True so the UI
        does not falsely show "MT5 offline".
        """
        self.demo_mode = False
        if not terminal_alive:
            self.mt5_connected = False
        self.market_open = False
        self.session_info = {"market_open": False, "source": "mt5_required", "reason": reason}
        self.connection_detail = reason
        self.activity = f"NO MARKET DATA — {reason}"
        self.error(reason)
        raise RuntimeError(reason)

    def _connect_mt5(self) -> bool:
        """Ensure an MT5DataFeed is connected. Returns True on success."""
        try:
            if self.feed is None:
                self.feed = MT5DataFeed(auto_connect=True)
            elif not self.feed.is_connected():
                self.feed.connect(retries=3, delay=0.7)
            self.mt5_connected = self.feed.is_connected()
            if self.mt5_connected:
                st = self.feed.connection_status()
                acct = st.get("account") or {}
                term = st.get("terminal") or {}
                login = acct.get("login") or "?"
                server = acct.get("server") or "?"
                tname = term.get("name") or "MT5"
                self.connection_detail = f"MT5 live · {tname} · account {login} @ {server}"
                self.demo_mode = False
                # Point analyst trade-learning context at the currently connected account,
                # preventing cached outcomes from another terminal login from leaking in.
                try:
                    from trading_team.research.trade_history_learning import set_active_account_scope
                    set_active_account_scope(acct.get("login"), acct.get("server"))
                except Exception:
                    pass
                return True
            self.connection_detail = f"MT5 not connected: {getattr(self.feed, '_last_error', '')}"
            return False
        except Exception as exc:
            self.mt5_connected = False
            self.demo_mode = False
            self.connection_detail = f"MT5 unavailable ({exc})"
            self.reconnect_attempts += 1
            return False

    def _symbol_candidates(self, primary: str) -> list[str]:
        """Ordered list of symbols to try when the primary is missing on the broker."""
        seen: set[str] = set()
        out: list[str] = []
        for s in [primary] + list(getattr(self, "watchlist", None) or []) + ["XAUUSD", "EURUSD"]:
            key = str(s or "").strip().upper()
            if key and key not in seen:
                seen.add(key)
                out.append(key)
        return out

    def get_snapshot(self, symbol: str | None = None):
        """Fetch multi-TF snapshot from MT5 only (no synthetic/demo data).

        If the configured primary symbol is not available on the broker, try
        other watchlist symbols before declaring total failure. A missing
        symbol no longer forces mt5_connected=False when the terminal itself
        is healthy.
        """
        primary = (symbol or self.config.symbol or "XAUUSD").upper()
        connected = self._connect_mt5()
        if not connected:
            self._no_market_error(
                primary,
                self.connection_detail or "MT5 not connected — start MetaTrader 5 and log in",
                terminal_alive=False,
            )

        candidates = self._symbol_candidates(primary)
        last_exc: Exception | None = None
        for sym in candidates:
            try:
                snap = MultiTimeframeFeed(self.feed).snapshot(sym, ("M5", "M15", "H1"), self.config.lookback)
                self.demo_mode = False
                self.mt5_connected = True
                # Persist the working symbol so subsequent cycles do not keep
                # retrying a missing primary.
                if sym != primary:
                    try:
                        self.config.symbol = sym
                    except Exception:
                        pass
                    self.connection_detail = (
                        f"MT5 REALTIME · {snap.metadata.get('broker_symbol', sym)} "
                        f"(fallback from unavailable {primary})"
                    )
                else:
                    broker = snap.metadata.get("broker_symbol", sym)
                    tick_t = snap.metadata.get("tick_time")
                    self.connection_detail = f"MT5 REALTIME · {broker}" + (f" · tick {tick_t}" if tick_t else "")
                try:
                    session = detect_session_from_mt5(self.feed.mt5, snap.metadata.get("broker_symbol", sym))
                except Exception as sess_exc:
                    # Do not infer that a session is open when the broker/session check failed.
                    session = {
                        "market_open": False,
                        "source": "session_check_error",
                        "reason": f"session check failed closed: {sess_exc}",
                    }
                self.market_open = session_confirmed_open(session)
                if session.get("market_open") and not self.market_open:
                    session["reason"] = (
                        f"Session not positively confirmed for new orders "
                        f"(source={session.get('source', 'unknown')})"
                    )
                session["market_open"] = self.market_open
                self.session_info = session
                snap.metadata["market_open"] = self.market_open
                snap.metadata["session"] = session
                snap.metadata["demo"] = False
                if snap.metadata.get("quote_fresh") is False:
                    age = snap.metadata.get("tick_age_seconds")
                    status = str(snap.metadata.get("quote_status") or "invalid_quote")
                    session["quote_fresh"] = False
                    session["quote_status"] = status
                    session["tick_raw_age_seconds"] = snap.metadata.get("tick_raw_age_seconds")
                    session["tick_clock_diagnostic"] = snap.metadata.get("tick_clock_diagnostic")
                    if status == "tick_timestamp_in_future":
                        self.connection_detail = (
                            f"MT5 connected · INVALID TICK CLOCK (corrected age={age}s); "
                            "no new-price update — set MT5_TIMESTAMP_OFFSET_SECONDS=10800 in .env if verified"
                        )
                    else:
                        self.connection_detail = (
                            f"MT5 connected · STALE / INVALID QUOTE ({age}s age; no new-price update)"
                        )
                elif not self.market_open:
                    self.connection_detail = f"MT5 connected · MARKET CLOSED ({session.get('reason', '')})"
                return snap
            except Exception as exc:
                last_exc = exc
                msg = str(exc).lower()
                # Symbol missing / not in Market Watch → try next candidate
                if "symbol unavailable" in msg or "symbol not found" in msg or "add it to market watch" in msg:
                    self.error(f"Symbol {sym} unavailable on this broker — trying next candidate")
                    continue
                # Real connection / data failure
                break

        # All candidates failed
        self.reconnect_attempts += 1
        terminal_ok = False
        try:
            if self.feed is not None and self.feed.is_connected():
                terminal_ok = True
            elif self.feed is not None and not self.feed.is_connected():
                try:
                    self.feed.shutdown()
                except Exception:
                    pass
                self.feed = None
                self.mt5_connected = False
        except Exception:
            self.feed = None
            self.mt5_connected = False

        reason = f"MT5 data unavailable for {primary}: {last_exc}"
        if terminal_ok and last_exc is not None:
            reason = (
                f"No usable symbol from candidates {candidates[:6]}. "
                f"Last error: {last_exc}. "
                "Open Market Watch → Show All and set TRADING_SYMBOL to an exact broker name "
                "(see http://127.0.0.1:8105/api/symbols)."
            )
        self._no_market_error(primary, reason, terminal_alive=terminal_ok)

    def refresh_watchlist_quotes(self):
        """Lightweight last-price scan across the watchlist (non-blocking best-effort)."""
        cache_key = "watchlist_quotes:" + ",".join(list(self.watchlist)[:40])
        cached = _RESPONSE_CACHE.get(cache_key)
        if cached is not None:
            self.watchlist_quotes = cached
            return cached
        quotes = {}
        for sym in list(self.watchlist)[:40]:
            try:
                if self.feed is not None and not self.demo_mode:
                    resolved = self.feed.resolve(sym)
                    tick = self.feed.mt5.symbol_info_tick(resolved)
                    if tick is not None:
                        quotes[sym] = {
                            "symbol": sym,
                            "bid": float(tick.bid),
                            "ask": float(tick.ask),
                            "price": float(tick.last or tick.bid or 0),
                            "spread": float(tick.ask - tick.bid) if tick.ask and tick.bid else 0,
                            "broker_symbol": resolved,
                            "demo": False,
                        }
                        continue
                quotes[sym] = {
                    "symbol": sym,
                    "price": None,
                    "bid": None, "ask": None, "spread": None,
                    "broker_symbol": sym, "demo": False, "error": True,
                    "reason": "MT5 quote unavailable",
                }
            except Exception as qexc:
                quotes[sym] = {
                    "symbol": sym,
                    "price": None,
                    "bid": None, "ask": None, "spread": None,
                    "broker_symbol": sym, "demo": False, "error": True,
                    "reason": str(qexc),
                }
        self.watchlist_quotes = quotes
        _RESPONSE_CACHE.set(cache_key, quotes, ttl=1.5)
        return quotes

    def _run_agent(self, agent, snap):
        name = getattr(agent, "name", agent.__class__.__name__.upper())
        started = time.perf_counter()
        self.agent_status[name] = {"state": "RUNNING", "started": utc_now().isoformat(), "latency_ms": 0}
        try:
            signal = agent.analyze(snap)
            latency = round((time.perf_counter() - started) * 1000, 2)
            self.agent_status[name] = {
                "state": "READY",
                "started": self.agent_status[name]["started"],
                "finished": utc_now().isoformat(),
                "latency_ms": latency,
                "side": signal.side.value,
                "confidence": signal.confidence,
            }
            return signal
        except Exception as exc:
            latency = round((time.perf_counter() - started) * 1000, 2)
            self.agent_status[name] = {"state": "ERROR", "latency_ms": latency, "message": str(exc)}
            self.error(f"{name} failed: {exc}", traceback.format_exc())
            raise

    def _trade_sharpe(self, recent_rows):
        vals=[float(x.get("realized_r") or 0.0) for x in recent_rows if int(x.get("closed") or 0)]
        if len(vals)<2:
            return 0.0
        import statistics
        sd=statistics.pstdev(vals)
        return (statistics.mean(vals)/sd) if sd>1e-9 else (10.0 if statistics.mean(vals)>0 else 0.0)

    def _update_research_state(self):
        perf=self.performance.summary()
        recent=self.performance.recent(500)
        sharpe=self._trade_sharpe(recent)
        dd=0.0
        equity=0.0
        peak=0.0
        for x in reversed([r for r in recent if int(r.get("closed") or 0)]):
            equity += float(x.get("realized_pnl") or 0.0)
            peak=max(peak,equity)
            dd=max(dd,peak-equity)
        decay=self.strategy_decay.evaluate(
            sharpe=sharpe,
            max_dd_pct=dd / max(abs(float(self.account_sync.get_cached().get("equity",10000.0) if hasattr(self.account_sync,'get_cached') else 10000.0)),1.0)*100,
            win_rate=float(perf.get("win_rate",0) or 0),
        )
        health={
            "trade_sharpe": round(sharpe,3),
            "win_rate_pct": round(float(perf.get("win_rate",0) or 0),2),
            "realized_pnl": round(float(perf.get("realized_pnl",0) or 0),4),
            "avg_r": round(float(perf.get("avg_r",0) or 0),3),
            "closed_trades": int(perf.get("closed",0) or 0),
            "decay": decay,
        }
        proposal=self.improvement_advisor.propose(self.strategy_id, perf, self.performance.agent_summary(), decay)
        self.research_state={"strategy_id":self.strategy_id,"health":health,"improvement":proposal.to_dict(),"memory_count":len(self.research_memory.recent(100))}
        return self.research_state

    def _record_run_card(self, snap, mesh, source):
        metrics=self.performance.summary()
        ev={"strategy_health":self.research_state.get("health",{}),
            "provider":mesh.provider,"decision":mesh.decision,"risk_gate":(mesh.risk_debate.to_dict() if mesh.risk_debate else None)}
        card=make_run_card(
            symbol=snap.symbol,
            timeframes=tuple(snap.metadata.get("timeframes",("M5","M15","H1"))),
            data_source=source,
            strategy_id=self.strategy_id,
            metrics=metrics,
            evidence=ev,
            config=self.config.__dict__,
        )
        self.last_run_card=__import__("dataclasses").asdict(card)
        try: save_run_card(card)
        except Exception as exc: self.error(f"Run card persistence failed: {exc}")

    def _load_runtime_settings(self):
        """Load the newest valid settings copy from the atomic file or SQLite fallback."""
        candidates = []
        try:
            value = json.loads(self._runtime_settings_file.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                candidates.append(value)
        except Exception:
            pass
        # A DB mirror protects settings when the runtime directory is read-only, locked,
        # or replaced during upgrades. It is intentionally read before startup workers start.
        try:
            with sqlite3.connect(DB_PATH, timeout=2.0) as db:
                db.execute("PRAGMA busy_timeout=2000")
                row = db.execute("SELECT payload_json FROM runtime_settings WHERE id=1").fetchone()
            if row and row[0]:
                value = json.loads(row[0])
                if isinstance(value, dict):
                    candidates.append(value)
        except Exception:
            pass
        if not candidates:
            return {}
        def _stamp(item):
            raw = str(item.get("_saved_at") or "")
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
            except Exception:
                return 0.0
        return max(candidates, key=_stamp)

    def _persist_runtime_settings(self):
        """Durably persist settings to file and SQLite; success requires either copy."""
        with self._settings_lock:
            payload = {
                "symbol": self.config.symbol,
                "symbols": list(self.watchlist),
                "interval": self.interval,
                "lookback": self.config.lookback,
                "analyze_all_symbols_enabled": bool(self.analyze_all_symbols_enabled),
                "all_symbol_scan_limit": int(self.all_symbol_scan_limit),
                "all_symbol_scan_batch_size": int(self.all_symbol_scan_batch_size),
                "all_symbol_rescan_seconds": int(self.all_symbol_rescan_seconds),
                "market_desks_enabled": list(self.market_desks_enabled),
                "exit_management": asdict(self.management.cfg),
                "debate_consensus_threshold": float(getattr(self, "debate_consensus_threshold", 0.55)),
                "strong_research_override": float(getattr(self, "strong_research_override", 0.72)),
                "timeframe": self.config.timeframe,
                "trade_cooldown_seconds": int(self.config.trade_cooldown_seconds),
                "deviation": int(self.config.deviation),
                "lifecycle_reconcile_seconds": int(self.config.lifecycle_reconcile_seconds),
                "one_position_per_symbol": bool(self.config.one_position_per_symbol),
                "risk_fraction": float(self.config.risk_fraction),
                "risk": asdict(self.config.risk) if hasattr(self.config.risk, "__dataclass_fields__") else dict(self.config.risk),
                "ai_analysis_interval_seconds": int(getattr(self, "ai_min_interval", 60)),
                "mode": "PAPER" if self.config.paper_trading else "LIVE",
                "paper_trading": bool(self.config.paper_trading),
                "live_trading_enabled": bool(self.config.live_trading_enabled),
                "live_armed": bool(self.live_armed),
                "kill_switch": bool(self.kill_switch),
                "auto_trade": bool(getattr(self, "auto_trade", True)),
                "paused": bool(getattr(self, "paused", False)),
                "trade_universe_enabled": bool(getattr(self, "trade_universe_enabled", True)),
                "trade_universe_max_per_cycle": int(getattr(self, "trade_universe_max_per_cycle", 15)),
                "local_ai_enabled": bool(getattr(self, "local_ai_enabled", False)),
                "local_ai_tiered": bool(getattr(self, "local_ai_tiered", True)),
                "local_ai_can_influence_decision": bool(getattr(self, "local_ai_can_influence", False)),
                "local_ai_base_url": str(getattr(self, "local_ai_base_url", "http://127.0.0.1:11434/v1")),
                "local_ai_gate_model": str(getattr(self, "local_ai_gate_model", "qwen2.5:1.5b")),
                "local_ai_deep_model": str(getattr(self, "local_ai_deep_model", "qwen2.5:3b")),
                "bt_symbol": str(getattr(self, "bt_symbol", "XAUUSD")),
                "bt_strategy": str(getattr(self, "bt_strategy", "trend_following")),
                "bt_bars": int(getattr(self, "bt_bars", 3000)),
                "bt_equity": float(getattr(self, "bt_equity", 10000)),
                "bt_risk_pct": float(getattr(self, "bt_risk_pct", 0.5)),
                "bt_execution_model": str(getattr(self, "bt_execution_model", "ohlc")),
                "bt_objective": str(getattr(self, "bt_objective", "composite")),
                "bt_max_trials": int(getattr(self, "bt_max_trials", 40)),
                "bt_folds": int(getattr(self, "bt_folds", 4)),
                "bt_is_ratio": float(getattr(self, "bt_is_ratio", 0.7)),
                "bt_commission_bps": float(getattr(self, "bt_commission_bps", 0.0)),
                "_saved_at": utc_now().isoformat(),
            }
            packed = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
            successes = []
            errors = []
            try:
                self._runtime_settings_file.parent.mkdir(parents=True, exist_ok=True)
                temp = self._runtime_settings_file.with_name(
                    self._runtime_settings_file.name + f".{os.getpid()}.{threading.get_ident()}.tmp"
                )
                with temp.open("w", encoding="utf-8", newline="\n") as stream:
                    stream.write(packed)
                    stream.flush()
                    os.fsync(stream.fileno())
                temp.replace(self._runtime_settings_file)
                successes.append("file")
            except Exception as exc:
                errors.append(f"file: {type(exc).__name__}: {exc}")
            try:
                with sqlite3.connect(DB_PATH, timeout=8.0) as db:
                    db.execute("PRAGMA busy_timeout=8000")
                    db.execute("""CREATE TABLE IF NOT EXISTS runtime_settings (
                        id INTEGER PRIMARY KEY CHECK(id=1), payload_json TEXT NOT NULL,
                        saved_at TEXT NOT NULL)""")
                    db.execute("""INSERT INTO runtime_settings(id,payload_json,saved_at) VALUES(1,?,?)
                        ON CONFLICT(id) DO UPDATE SET payload_json=excluded.payload_json,saved_at=excluded.saved_at""",
                        (packed, payload["_saved_at"]))
                    db.commit()
                successes.append("sqlite")
            except Exception as exc:
                errors.append(f"sqlite: {type(exc).__name__}: {exc}")
            if not successes:
                raise OSError("Runtime settings could not be saved to either storage target: " + "; ".join(errors))
            self._saved_runtime_settings = payload
            self._last_settings_persist_ts = time.time()
            if errors:
                try:
                    self.error("Settings saved with fallback storage; " + "; ".join(errors)[:400])
                except Exception:
                    pass
            return True

    def management_ready(self):
        """Allow safety management of existing bot positions while entries are locked.

        Unlike live_ready(), this intentionally does not require live_armed or the
        master entry kill switch to be released. It still requires explicit live
        configuration, a real MT5 account sync, and an enabled exit supervisor.
        Each individual broker mutation then requires a fresh validated quote.
        """
        return bool(
            self.config.live_trading_enabled and not self.config.paper_trading
            and self.feed is not None and self.mt5_connected and self.account_live
            and not self.demo_mode and self.management.cfg.enabled
        )

    def _sync_instrument_registry(self, force: bool = False) -> dict:
        """Refresh the persistent instrument registry from broker-supplied MT5 metadata."""
        now = time.time()
        if self.feed is None or not self.mt5_connected:
            return {"status": "offline", "updated": 0}
        if not force and now - self._registry_last_sync_epoch < self._registry_sync_seconds:
            return {"status": "cached", "updated": 0}
        try:
            api_lock = getattr(self.feed, "_api_lock", None)
            bridge = self.feed.get_bridge()
            def read_catalogue():
                if api_lock is None:
                    total = bridge.symbols_total()
                    return total, bridge.symbols_get(limit=max(1, int(total or 1_000_000)))
                with api_lock:
                    total = bridge.symbols_total()
                    return total, bridge.symbols_get(limit=max(1, int(total or 1_000_000)))
            source_total, rows = retry_call(
                read_catalogue, attempts=2, base_delay=0.08, max_delay=0.2, jitter=0.15,
                on_retry=lambda attempt, exc, delay: self.log_event("instrument_registry_retry", {
                    "attempt": attempt, "delay_seconds": round(delay, 3), "error": str(exc)[:200]
                }),
            )
            if not rows:
                # A failed or empty broker query must not deactivate the last good catalogue.
                return {"status": "unavailable", "updated": 0, "reason": "MT5 returned no symbol metadata"}
            # A capped response is a partial catalogue. Only a source-count-confirmed
            # complete snapshot can deactivate symbols no longer offered by the broker.
            complete = bool(source_total is not None and int(source_total) > 0 and len(rows) == int(source_total))
            stats = self.instrument_registry.sync(
                rows, complete_snapshot=complete,
                source_total=int(source_total) if source_total else None,
                source_id="MetaTrader5.symbols_get",
            )
            self._registry_last_sync_epoch = now
            self._available_symbols_cache = None
            _RESPONSE_CACHE.invalidate("registry:")
            self.log_event("instrument_registry_synced", stats)
            return {"status": "updated", **stats}
        except Exception as exc:
            self.error(f"Instrument registry sync failed: {exc}")
            return {"status": "error", "updated": 0, "reason": str(exc)[:400]}

    def _available_analysis_symbols(self):
        """Return the full exact broker universe, cached between registry generations."""
        cache_key = (
            str(self.config.symbol or "").strip(),
            tuple(str(x or "").strip() for x in self.watchlist),
            int(self.all_symbol_scan_limit or 0),
            float(self._registry_last_sync_epoch or 0),
        )
        cached = self._available_symbols_cache
        now_mono = time.monotonic()
        if cached is not None and cached[1] == cache_key and now_mono - cached[0] < self._available_symbols_cache_seconds:
            return list(cached[2])

        broker_names = self.instrument_registry.symbol_names(
            limit=(self.all_symbol_scan_limit or None), include_disabled=True
        )
        by_upper: dict[str, str] = {}
        for raw_name in broker_names:
            # Use broker-exact casing for MT5 IPC; normalized keys are index-only.
            exact = str(raw_name or "").strip()
            if exact and exact.upper() != "GBXUSD":
                by_upper.setdefault(exact.upper(), exact)
        ordered = []
        for requested in [self.config.symbol, *self.watchlist]:
            raw = str(requested or "").strip()
            if not raw or raw.upper() == "GBXUSD":
                continue
            try:
                if self.feed is not None and getattr(self.feed, "mt5", None) is not None:
                    resolved = str(resolve_mt5_symbol(self.feed.mt5, raw) or "").strip()
                else:
                    resolved = by_upper.get(raw.upper(), "")
            except Exception:
                resolved = by_upper.get(raw.upper(), "")
            exact = by_upper.get(resolved.upper(), "") if resolved else ""
            if exact:
                ordered.append(exact)
        ordered.extend(by_upper.values())
        unique = []
        seen = set()
        for symbol in ordered:
            symbol = str(symbol or "").strip()
            key = symbol.upper()
            if not key or key == "GBXUSD" or key in seen:
                continue
            seen.add(key)
            unique.append(symbol)
            if self.all_symbol_scan_limit and len(unique) >= self.all_symbol_scan_limit:
                break
        self._available_symbols_cache = (now_mono, cache_key, list(unique))
        return unique

    @staticmethod
    def _universe_row(snapshot, mesh, *, primary=False):
        report_rows = []
        for report in (getattr(mesh, "reports", None) or []):
            side = getattr(report, "side", "FLAT")
            side = getattr(side, "value", side)
            report_rows.append({
                "agent": str(getattr(report, "agent", "Analyst")),
                "role": str(getattr(report, "role", "")),
                "side": str(side),
                "confidence": round(float(getattr(report, "confidence", 0) or 0), 4),
                "thesis": str(getattr(report, "thesis", "") or "")[:260],
                "evidence": [str(x)[:180] for x in (getattr(report, "evidence", None) or [])[:2]],
            })
        md = getattr(snapshot, "metadata", {}) or {}
        return {
            "symbol": str(getattr(snapshot, "symbol", "")).upper(),
            "broker_symbol": str(md.get("broker_symbol") or getattr(snapshot, "symbol", "")),
            "price": float(getattr(snapshot, "price", 0) or 0),
            "spread": float(getattr(snapshot, "spread", 0) or 0),
            "timestamp": (getattr(snapshot, "timestamp", None).isoformat() if hasattr(getattr(snapshot, "timestamp", None), "isoformat") else str(getattr(snapshot, "timestamp", ""))),
            "updated": utc_now().isoformat(),
            "quote_fresh": bool(md.get("quote_fresh", False)),
            "quote_status": str(md.get("quote_status") or ("verified" if md.get("quote_fresh") else "unverified")),
            "timestamp_integrity_status": str(md.get("timestamp_integrity_status") or "not_checked"),
            "system_clock_utc": md.get("system_clock_utc"),
            "latest_bar_time_utc": md.get("latest_bar_time_utc"),
            "latest_bar_age_seconds": md.get("latest_bar_age_seconds"),
            "bar_timestamp_in_future": bool(md.get("bar_timestamp_in_future", False)),
            "tick_time_fields_consistent": md.get("tick_time_fields_consistent"),
            "tick_time_fields_delta_seconds": md.get("tick_time_fields_delta_seconds"),
            "timestamp_offset_configuration_conflict": bool(md.get("timestamp_offset_configuration_conflict", False)),
            "desk_decision": str(getattr(mesh, "decision", "WAIT")),
            "desk_score": round(float(getattr(mesh, "confidence", 0) or 0), 4),
            "thesis": str(getattr(mesh, "ai_thesis", "") or ""),
            "reasons": [str(x)[:220] for x in (getattr(mesh, "reasons", None) or [])[:3]],
            "agent_views": report_rows,
            "primary_symbol": bool(primary),
            "execution_authorized": False,
            "execution_path": "Instrument discovery and analysis do not authorize an order. Full risk preflight remains mandatory.",
            "note": "Director screening only. Specialist review + TRADE_UNIVERSE_ENABLED may route through the central risk gateway.",
            "scanned_epoch": time.time(),
        }

    def _enqueue_universe_jobs(self, available: list[str]) -> dict:
        """Queue a rotating bounded window without per-symbol database round trips.

        The cursor advances across the *entire* broker catalogue, while batched queue
        reads avoid N+1 SQL lookups and bound dispatcher CPU for very large universes.
        """
        if not available:
            return {"queued_now": 0, "cursor": self._universe_cursor, "looked_at": 0}
        now = time.time()
        bucket = int(now // max(10, self.all_symbol_rescan_seconds))
        batch_size = max(1, min(100, self.all_symbol_scan_batch_size))
        start = self._universe_cursor % len(available)
        lookahead = min(len(available), max(500, batch_size * 25))
        window = [available[(start + i) % len(available)] for i in range(lookahead)]
        primary = str(self.config.symbol).strip().upper()
        if self.feed is not None and getattr(self.feed, "mt5", None) is not None:
            try:
                primary = str(resolve_mt5_symbol(self.feed.mt5, self.config.symbol) or primary).strip().upper()
            except Exception:
                pass
        watch = {str(x).strip().upper() for x in self.watchlist}
        if self.feed is not None and getattr(self.feed, "mt5", None) is not None:
            for requested in list(self.watchlist):
                try:
                    resolved = resolve_mt5_symbol(self.feed.mt5, requested)
                    if resolved:
                        watch.add(str(resolved).strip().upper())
                except Exception:
                    continue

        result_by_symbol = self.scan_queue.result_state_map([str(x).upper() for x in window])
        pending = self.scan_queue.pending_symbols()
        max_pending = max(batch_size, min(10_000, env_num("SCAN_QUEUE_MAX_PENDING", max(200, batch_size * 8), int)))
        if len(pending) >= max_pending:
            return {"queued_now": 0, "cursor": self._universe_cursor, "looked_at": 0,
                    "lookahead_window": lookahead, "universe_size": len(available),
                    "pending_jobs": len(pending), "pending_limit": max_pending, "queue_backpressure": True}
        queued_now = 0
        looked = 0
        for offset, exact_name in enumerate(window):
            broker_symbol = str(exact_name).strip()
            symbol = broker_symbol.upper()
            looked += 1
            if symbol == primary:
                continue
            previous = result_by_symbol.get(symbol)
            scanned_epoch = float((previous or {}).get("scanned_epoch") or 0)
            previous_status = str((previous or {}).get("scan_job_status") or (previous or {}).get("status") or "").upper()
            failed_previous = previous_status in {"FAILED", "ERROR", "SCAN_FAILED"}
            cooldown = min(30, self.all_symbol_rescan_seconds) if failed_previous else self.all_symbol_rescan_seconds
            if previous and now - scanned_epoch < cooldown:
                continue
            if symbol in pending:
                continue
            if failed_previous:
                retry_bucket = int(now // max(10, cooldown))
                job_key = f"symbol-scan:{symbol}:retry:{retry_bucket}"
            else:
                job_key = f"symbol-scan:{symbol}:{bucket}"
            result = self.scan_queue.enqueue(
                job_key=job_key, symbol=symbol, job_type="symbol_scan",
                payload={"symbol": symbol, "broker_symbol": broker_symbol, "timeframes": ["M5", "M15", "H1"],
                         "lookback": int(self.config.lookback), "registry_generation": self._registry_last_sync_epoch},
                priority=10 if symbol in watch else 0, max_attempts=4,
            )
            if result.get("status") == "QUEUED":
                queued_now += 1
                pending.add(symbol)
            if queued_now >= batch_size:
                break
        self._universe_cursor = (start + looked) % len(available)
        self.scan_queue.state_set("broker_universe_cursor", self._universe_cursor)
        return {"queued_now": queued_now, "cursor": self._universe_cursor, "looked_at": looked,
                "lookahead_window": lookahead, "universe_size": len(available)}

    @staticmethod
    def _compact_instrument_for_handoff(instrument: dict) -> dict:
        """Keep only the reviewed contract context needed by a queued specialist task."""
        raw = instrument.get("raw_metadata") if isinstance(instrument.get("raw_metadata"), dict) else {}
        keys = ("name", "path", "description", "category", "exchange", "sector", "sector_name",
                "industry", "industry_name", "isin", "currency_base", "currency_profit", "currency_margin")
        return {
            "broker_symbol": str(instrument.get("broker_symbol_exact") or instrument.get("broker_symbol") or raw.get("name") or ""),
            "broker_symbol_exact": str(instrument.get("broker_symbol_exact") or raw.get("name") or instrument.get("broker_symbol") or ""),
            "description": str(instrument.get("description") or raw.get("description") or ""),
            "path": str(instrument.get("path") or raw.get("path") or ""),
            "category": str(instrument.get("category") or raw.get("category") or ""),
            "exchange": str(instrument.get("exchange") or raw.get("exchange") or ""),
            "currency_base": str(instrument.get("currency_base") or raw.get("currency_base") or ""),
            "currency_profit": str(instrument.get("currency_profit") or raw.get("currency_profit") or ""),
            "currency_margin": str(instrument.get("currency_margin") or raw.get("currency_margin") or ""),
            "market_desk": str(instrument.get("market_desk") or "unknown"),
            "underlying_market": str(instrument.get("underlying_market") or "unknown"),
            "product_type": str(instrument.get("product_type") or "unknown"),
            "classification_status": str(instrument.get("classification_status") or "needs_review"),
            "entry_metadata_ready": bool(instrument.get("entry_metadata_ready")),
            "entry_eligible": bool(instrument.get("entry_eligible")),
            "cfd_overlay_required": bool(instrument.get("cfd_overlay_required")),
            "review_reasons": list(instrument.get("review_reasons") or [])[:12],
            "raw_metadata": {key: raw.get(key) for key in keys if raw.get(key) not in (None, "")},
        }

    @staticmethod
    def _apply_market_director_result(row: dict, director_result: dict, instrument: dict, *, primary: bool = False) -> dict:
        """Join lightweight market-director evidence to a display row, never to order authority."""
        status = str(director_result.get("candidate_status") or "NO_SETUP")
        candidate = bool(director_result.get("candidate"))
        side = str(director_result.get("candidate_side") or "FLAT")
        if status == "CANDIDATE" and candidate:
            display_decision = side
        elif status == "WATCH" and side in {"BUY", "SELL"}:
            display_decision = "WATCH"
        elif status == "BLOCKED_DATA":
            display_decision = "WAIT"
        elif status == "UNMAPPED_RESEARCH":
            display_decision = "UNMAPPED"
        else:
            display_decision = "WAIT"
        row.update({
            "symbol": str(director_result.get("symbol") or row.get("symbol") or "").upper(),
            "broker_symbol": str(director_result.get("broker_symbol") or row.get("broker_symbol") or ""),
            "market_desk": str(director_result.get("market_desk") or instrument.get("market_desk") or "unknown"),
            "underlying_market": str(director_result.get("underlying_market") or instrument.get("underlying_market") or "unknown"),
            "product_type": str(director_result.get("product_type") or instrument.get("product_type") or "unknown"),
            "classification_status": str(instrument.get("classification_status") or "needs_review"),
            "entry_metadata_ready": bool(instrument.get("entry_metadata_ready")),
            "entry_eligible_by_registry": bool(instrument.get("entry_eligible")),
            "registry_review_reasons": list(instrument.get("review_reasons") or [])[:8],
            "cfd_overlay_required": bool(director_result.get("cfd_overlay_required")),
            "market_desk_routes": list(director_result.get("desk_routes") or []),
            "market_subuniverses": dict(director_result.get("subuniverses") or {}),
            "candidate_status": status,
            "candidate_side": side,
            "candidate_score": float(director_result.get("candidate_score") or 0.0),
            "trigger_families": list(director_result.get("trigger_families") or []),
            "market_director": director_result,
            "market_features": director_result.get("market_features") or {},
            "scout_screen": director_result.get("scout_screen") or {},
            "currency_factor_context": director_result.get("currency_factor_context") or {},
            "snapshot_id": str(director_result.get("snapshot_id") or ""),
            "desk_decision": display_decision,
            "desk_score": float(director_result.get("candidate_score") or 0.0),
            "thesis": " ".join(str(x) for x in (director_result.get("candidate_reasons") or [])[:3]),
            "reasons": list(director_result.get("candidate_reasons") or [])[:8],
            "agent_views": [],
            "agent_count": 0,
            "primary_symbol": bool(primary),
            "execution_authorized": False,
            "research_only": True,
            "execution_path": "Director screening finds candidates; specialist mesh may authorize via TRADE_UNIVERSE_ENABLED + central risk gateway.",
            "note": "Research-only market-discovery result. A candidate must pass the normal selected-symbol, risk, contract, timestamp and execution gates before any live action.",
            "scanned_epoch": float(director_result.get("scanned_epoch") or time.time()),
            "quote_assessed_at": director_result.get("evaluated_at"),
            "specialist_review_status": "QUEUED" if candidate else "NOT_ROUTED",
            "specialist_decision": None,
            "snapshot_integrity_status": "IMMUTABLE_SNAPSHOT_CAPTURED",
        })
        return row

    def _attempt_scan_self_heal(self, error: BaseException, *, job_type: str = "symbol_scan") -> dict:
        """Apply conservative, bounded recovery actions for known transient failures.

        It never edits strategy logic or relaxes execution/risk gates. Failed work remains
        queued for the durable retry policy; this routine only restores safe infrastructure.
        """
        message = f"{type(error).__name__}: {error}"[:500]
        fingerprint = hashlib.sha256(message.lower().encode("utf-8", "replace")).hexdigest()[:16]
        now = time.monotonic()
        wall_now = time.time()
        try:
            registry = self.scan_queue.state_get("self_heal_registry", {})
            if not isinstance(registry, dict):
                registry = {}
        except Exception:
            registry = {}
        prior_record = registry.get(fingerprint, {}) if isinstance(registry, dict) else {}
        if not isinstance(prior_record, dict):
            prior_record = {}
        prior_attempts = max(0, int(prior_record.get("attempts", 0) or 0))
        prior_epoch = float(prior_record.get("last_attempt_epoch", 0) or 0)
        prior_succeeded = bool(prior_record.get("last_recovery_succeeded", False))
        persistent_cooldown = 15.0 if prior_succeeded else min(600.0, 15.0 * (2 ** min(max(0, prior_attempts - 1), 5)))
        if prior_attempts and wall_now - prior_epoch < persistent_cooldown:
            return {"attempted": False, "reason": "persistent fingerprint cooldown",
                    "retry_after_seconds": round(persistent_cooldown - (wall_now - prior_epoch), 1),
                    "fingerprint": fingerprint}
        with self._self_heal_lock:
            prior = self._self_heal_recent.get(fingerprint, 0.0)
            if now - prior < 15.0:
                return {"attempted": False, "reason": "same error recently recovered", "fingerprint": fingerprint}
            self._self_heal_recent[fingerprint] = now
            if len(self._self_heal_recent) > 256:
                oldest = sorted(self._self_heal_recent, key=self._self_heal_recent.get)[:64]
                for key in oldest:
                    self._self_heal_recent.pop(key, None)

        actions: list[str] = []
        success = False
        lowered = message.lower()
        try:
            if any(term in lowered for term in ("database is locked", "database is busy", "sqlite", "disk i/o", "lease")):
                try:
                    with self.scan_queue._lock:
                        self.scan_queue.db.execute("PRAGMA busy_timeout=15000")
                    recovered = self.scan_queue.recover_expired()
                    actions.append(f"queue_recovery(requeued={recovered.get('requeued', 0)},failed={recovered.get('failed', 0)})")
                    success = True
                except Exception as recovery_exc:
                    actions.append(f"queue_recovery_failed:{type(recovery_exc).__name__}")
            if any(term in lowered for term in ("mt5", "terminal", "not connected", "market data", "quote", "tick", "rates")):
                feed = self.feed
                ensure = getattr(feed, "ensure_connected", None) if feed is not None else None
                if callable(ensure):
                    connected = bool(ensure())
                    self.mt5_connected = connected
                    self.connection_detail = "Connected after automatic recovery" if connected else str(getattr(feed, "_last_error", "MT5 reconnect failed"))[:300]
                    actions.append("mt5_reconnect_succeeded" if connected else "mt5_reconnect_not_ready")
                    success = success or connected
                    if connected:
                        try:
                            from trading_team.data.latency_cache import cache_invalidate
                            cache_invalidate()
                            actions.append("invalidated_short_ttl_market_cache")
                        except Exception:
                            pass
            if not actions:
                actions.append("durable_job_retry_scheduled")
            with self._universe_lock:
                status = self._universe_worker_status.setdefault("self_healing", {"attempts": 0, "successes": 0})
                status["attempts"] = int(status.get("attempts", 0)) + 1
                status["successes"] = int(status.get("successes", 0)) + int(success)
                status["last_action"] = ";".join(actions)[:400]
                status["last_error_fingerprint"] = fingerprint
                status["last_attempt_at"] = utc_now().isoformat()
                status["last_recovery_succeeded"] = bool(success)
            registry[fingerprint] = {
                "attempts": prior_attempts + 1,
                "successes": int(prior_record.get("successes", 0) or 0) + int(success),
                "last_attempt_epoch": wall_now,
                "last_attempt_at": utc_now().isoformat(),
                "last_recovery_succeeded": bool(success),
                "last_action": ";".join(actions)[:400],
                "last_error": message[:240],
            }
            if len(registry) > 128:
                retained = sorted(registry.items(), key=lambda pair: float((pair[1] or {}).get("last_attempt_epoch", 0) or 0), reverse=True)[:128]
                registry = dict(retained)
            try:
                # Persist bounded recovery history so restarts do not hammer the same
                # recovery path over and over; failed recovery gets exponential cooldown.
                self.scan_queue.state_set("self_heal_registry", registry)
            except Exception:
                pass
            try:
                self.log_event("self_healing_attempt", {"job_type": job_type, "fingerprint": fingerprint,
                    "actions": actions, "success": success, "error": message[:240]})
            except Exception:
                pass
            return {"attempted": True, "success": success, "actions": actions, "fingerprint": fingerprint}
        except Exception as recovery_exc:
            return {"attempted": True, "success": False, "actions": actions,
                    "error": f"{type(recovery_exc).__name__}: {recovery_exc}"[:200], "fingerprint": fingerprint}

    def _universe_worker_loop(self, worker_index: int = 1):
        worker_id = f"market-directors-{os.getpid()}-{id(self):x}-{int(worker_index)}"
        pending_jobs: list[dict] = []
        self._universe_worker_status.update({"state": "READY", "worker_id": worker_id,
                                             "worker_count": self.universe_worker_count, "last_error": None,
                                             "blocked_reason": None, "heartbeat_at": utc_now().isoformat()})
        while self.running:
            try:
                self._universe_worker_status["heartbeat_at"] = utc_now().isoformat()
                if self.paused:
                    self._universe_worker_status.update({"state": "PAUSED", "blocked_reason": "Runtime is paused"})
                    time.sleep(0.5)
                    continue
                if not self.analyze_all_symbols_enabled:
                    self._universe_worker_status.update({"state": "DISABLED", "blocked_reason": "All-symbol analysis is disabled in runtime settings"})
                    time.sleep(0.5)
                    continue
                if self.feed is None or not self.mt5_connected or self.demo_mode:
                    self._universe_worker_status.update({"state": "WAITING_FOR_FEED", "blocked_reason": "A real connected MT5 market feed is required for broker-universe scanning"})
                    time.sleep(0.5)
                    continue
                if not pending_jobs:
                    pending_jobs = self.scan_queue.claim_batch(
                        worker_id, batch_size=max(2, min(8, self.universe_worker_count))
                    )
                if not pending_jobs:
                    self._universe_worker_status.update({"state": "WAITING_FOR_JOBS", "blocked_reason": "No due scan jobs; next dispatcher cycle will enqueue due instruments"})
                    time.sleep(0.35)
                    continue
                job = pending_jobs.pop(0)
                symbol = str(job.get("symbol") or "").upper()  # normalized queue/database identity
                payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
                job_type = str(job.get("job_type") or "symbol_scan")
                current_registry = self.instrument_registry.get(symbol) or {}
                broker_symbol = str(payload.get("broker_symbol") or current_registry.get("broker_symbol_exact") or current_registry.get("broker_symbol") or symbol).strip()
                job_id = int(job["id"])
                self._universe_worker_status.update({"state": "RUNNING", "stage": job_type, "last_started": utc_now().isoformat(),
                                                      "last_symbol": broker_symbol, "last_error": None,
                                                      "blocked_reason": None, "heartbeat_at": utc_now().isoformat(),
                                                      "progress_at": utc_now().isoformat()})
                lease_stop = threading.Event()
                lease_lost = threading.Event()

                def keep_lease_alive():
                    interval = max(2.0, float(self.scan_queue.lease_seconds) / 3.0)
                    while not lease_stop.wait(interval):
                        try:
                            if not self.scan_queue.renew_lease(job_id, worker_id):
                                lease_lost.set()
                                return
                        except Exception as lease_exc:
                            self._universe_worker_status["lease_warning"] = str(lease_exc)[:300]

                lease_thread = threading.Thread(target=keep_lease_alive, name=f"market-job-lease-{job_id}", daemon=True)
                lease_thread.start()
                try:
                    if job_type == "specialist_review":
                        director_result = payload.get("director_result") if isinstance(payload.get("director_result"), dict) else {}
                        instrument = payload.get("instrument") if isinstance(payload.get("instrument"), dict) else {}
                        source_snapshot = decode_snapshot(str(payload.get("snapshot_blob") or ""))
                        expected_fingerprint = str(payload.get("snapshot_id") or director_result.get("snapshot_id") or "")
                        actual_fingerprint = snapshot_fingerprint(source_snapshot)
                        resolved_snapshot_symbol = str((source_snapshot.metadata or {}).get("broker_symbol") or source_snapshot.symbol).strip()
                        if not expected_fingerprint or actual_fingerprint != expected_fingerprint:
                            raise RuntimeError("Persisted candidate snapshot fingerprint mismatch; specialist review refused")
                        if resolved_snapshot_symbol.upper() != broker_symbol.upper():
                            raise RuntimeError(f"Persisted snapshot symbol mismatch: expected {broker_symbol}, got {resolved_snapshot_symbol}")
                        # The original captured snapshot remains untouched. The shared mesh receives
                        # its own reconstruction; any mutation is detected and rejects the review.
                        specialist_snapshot = decode_snapshot(str(payload.get("snapshot_blob") or ""))
                        before_mesh_fingerprint = snapshot_fingerprint(specialist_snapshot)
                        with self._universe_lock:
                            if self._universe_mesh is None:
                                self._universe_mesh = AgentMesh()
                            mesh = self._universe_mesh
                        # The mesh/provider is stateful. Queue workers may claim and prepare
                        # jobs in parallel, but must not mutate the shared analyst instance concurrently.
                        with self._universe_mesh_analysis_lock:
                            pair_decision = mesh.analyze(specialist_snapshot, force_ai=True)
                        after_mesh_fingerprint = snapshot_fingerprint(specialist_snapshot)
                        row = self._universe_row(source_snapshot, pair_decision, primary=False)
                        self._apply_market_director_result(row, director_result, instrument, primary=False)
                        row["symbol"] = symbol
                        row["broker_symbol"] = broker_symbol
                        row["snapshot_id"] = expected_fingerprint
                        row["snapshot_integrity_status"] = "VERIFIED_UNCHANGED" if before_mesh_fingerprint == after_mesh_fingerprint == expected_fingerprint else "REVIEW_REJECTED_SNAPSHOT_MUTATION"
                        row["candidate_epoch"] = director_result.get("scanned_epoch")
                        row["director_candidate_status"] = str(director_result.get("candidate_status") or "CANDIDATE")
                        row["specialist_review_status"] = "COMPLETED" if row["snapshot_integrity_status"] == "VERIFIED_UNCHANGED" else "REJECTED_SNAPSHOT_MUTATION"
                        row["specialist_decision"] = str(getattr(pair_decision, "decision", "WAIT")) if row["snapshot_integrity_status"] == "VERIFIED_UNCHANGED" else "WAIT"
                        if row["snapshot_integrity_status"] == "VERIFIED_UNCHANGED":
                            row["desk_decision"] = str(getattr(pair_decision, "decision", "WAIT"))
                            row["desk_score"] = round(float(getattr(pair_decision, "confidence", 0.0) or 0.0), 4)
                            row["thesis"] = str(getattr(pair_decision, "ai_thesis", "") or row.get("thesis") or "")
                            row["reasons"] = [str(x)[:220] for x in (getattr(pair_decision, "reasons", None) or [])[:6]]
                            row["candidate_status"] = "DEEP_REVIEWED" if row["specialist_decision"] in {"BUY", "SELL"} else "REVIEWED_WAIT"
                            row["agent_views"] = row.pop("agent_views", [])
                            row["agent_count"] = len(row.get("agent_views") or [])
                        else:
                            row["desk_decision"] = "WAIT"
                            row["desk_score"] = 0.0
                            row["agent_views"] = []
                            row["agent_count"] = 0
                            row["candidate_status"] = "REVIEW_REJECTED"
                            row["reasons"] = ["Specialist mesh mutated the immutable snapshot representation; result rejected."]
                        row["execution_authorized"] = False
                        row["research_only"] = not self.trade_universe_enabled
                        row["note"] = (
                            "Specialist-reviewed candidate. When TRADE_UNIVERSE_ENABLED is on, "
                            "BUY/SELL may route through the central risk gateway (paper or live-armed)."
                            if self.trade_universe_enabled else
                            "Specialist-reviewed candidate. Research-only — universe trading disabled."
                        )
                        row["scan_job_id"] = job_id
                        row["scanned_epoch"] = time.time()
                        row["updated"] = utc_now().isoformat()
                        # Optional multi-symbol execution (same risk path as primary)
                        if (
                            self.trade_universe_enabled
                            and row.get("specialist_decision") in {"BUY", "SELL"}
                            and row.get("snapshot_integrity_status") == "VERIFIED_UNCHANGED"
                        ):
                            try:
                                exec_result = self._try_universe_execution(
                                    broker_symbol, source_snapshot, pair_decision, row
                                )
                                row["universe_execution"] = exec_result
                                if exec_result.get("status") in {
                                    "PAPER_FILLED", "FILLED_VERIFIED", "SUBMITTED", "SUBMITTED_UNCONFIRMED"
                                }:
                                    row["execution_authorized"] = True
                                    row["research_only"] = False
                                    row["note"] = f"Universe trade path: {exec_result.get('status')}"
                            except Exception as univ_exc:
                                row["universe_execution"] = {
                                    "status": "ERROR",
                                    "reason": f"{type(univ_exc).__name__}: {univ_exc}"[:300],
                                }
                        self.market_directors.record_handoff(director_result, completed=True)
                        self.log_event("market_candidate_specialist_reviewed", {
                            "symbol": broker_symbol, "snapshot_id": expected_fingerprint,
                            "desk_routes": director_result.get("desk_routes"),
                            "candidate_side": director_result.get("candidate_side"),
                            "specialist_decision": row.get("specialist_decision"),
                            "candidate_status": row.get("candidate_status"),
                            "execution_authorized": bool(row.get("execution_authorized")),
                        })
                    else:
                        pair_snapshot = MultiTimeframeFeed(self.feed).snapshot(broker_symbol, ("M5", "M15", "H1"), int(payload.get("lookback") or self.config.lookback))
                        instrument = current_registry
                        if not instrument:
                            raise RuntimeError(f"Instrument registry row disappeared during scan: {symbol}")
                        # Snapshot capture and feature calculation share the MT5 lock through
                        # MultiTimeframeFeed. The director consumes completed bars only.
                        director_result = self.market_directors.evaluate(instrument, pair_snapshot)
                        self.market_directors.record_scan(director_result)
                        result_for_factors = {
                            **director_result,
                            "currency_base": instrument.get("currency_base"),
                            "currency_profit": instrument.get("currency_profit"),
                            "market_features": director_result.get("market_features") or {},
                            "scanned_epoch": director_result.get("scanned_epoch") or time.time(),
                        }
                        self.market_directors.record_research_result(result_for_factors, instrument)
                        row = self._universe_row(pair_snapshot, None, primary=False)
                        self._apply_market_director_result(row, director_result, instrument, primary=False)
                        row["symbol"] = symbol
                        row["broker_symbol"] = broker_symbol
                        row["scan_job_id"] = job_id
                        if bool(director_result.get("candidate")):
                            specialist_payload = {
                                "symbol": symbol,
                                "broker_symbol": broker_symbol,
                                "snapshot_id": director_result.get("snapshot_id"),
                                "snapshot_blob": encode_snapshot(pair_snapshot),
                                "director_result": director_result,
                                "instrument": self._compact_instrument_for_handoff(instrument),
                            }
                            review_key = f"specialist-review:{symbol}:{director_result.get('snapshot_id')}"
                            queued = self.scan_queue.enqueue(
                                job_key=review_key, symbol=symbol, job_type="specialist_review", payload=specialist_payload,
                                priority=100 + int(100 * float(director_result.get("candidate_score") or 0.0)), max_attempts=2,
                            )
                            row["specialist_review_status"] = "QUEUED" if queued.get("status") in {"QUEUED", "RUNNING"} else str(queued.get("status") or "QUEUE_UNAVAILABLE")
                            row["specialist_review_job_id"] = queued.get("id")
                            if row["specialist_review_status"] == "QUEUED":
                                self.market_directors.record_handoff(director_result, completed=False)
                            self.log_event("market_candidate_routed", {
                                "symbol": broker_symbol, "snapshot_id": director_result.get("snapshot_id"),
                                "desk_routes": director_result.get("desk_routes"), "subuniverses": director_result.get("subuniverses"),
                                "candidate_side": director_result.get("candidate_side"), "candidate_score": director_result.get("candidate_score"),
                                "review_job_status": row["specialist_review_status"], "execution_authorized": False,
                            })
                        row["scanned_epoch"] = time.time()
                        row["updated"] = utc_now().isoformat()
                        row["note"] = "Market-director screening result. Candidate setups are sent to the shared specialist mesh using the exact same snapshot; the review remains research-only."
                    row["scan_job_type"] = job_type
                    row["scan_job_status"] = "SUCCEEDED"
                    row["scan_completed"] = True
                    row["scan_job_completed_at"] = utc_now().isoformat()
                    row.setdefault("scan_job_id", job_id)
                    lease_stop.set()
                    lease_thread.join(timeout=2.0)
                    if lease_lost.is_set():
                        raise RuntimeError("Scan job lease was lost before result commit; result will not be published")
                    committed = self.scan_queue.complete(job_id, row, worker_id=worker_id)
                    if not committed:
                        raise RuntimeError("Scan job ownership changed before result commit; stale result discarded")
                    with self._universe_lock:
                        director_status = str(row.get("director_candidate_status") or row.get("candidate_status") or "").upper()
                        director_side = str(row.get("candidate_side") or "").upper()
                        is_discovery_candidate = (director_status == "CANDIDATE" and director_side in {"BUY", "SELL"}
                                                  and row.get("quote_fresh") is not False)
                        if is_discovery_candidate:
                            compact_row = {k: v for k, v in row.items() if k != "agent_views"}
                            compact_row["agent_count"] = len(row.get("agent_views") or [])
                            self._universe_results[symbol] = compact_row
                        else:
                            self._universe_results.pop(symbol, None)
                        self._universe_worker_status["completed"] = int(self._universe_worker_status.get("completed", 0)) + 1
                        self._universe_worker_status["state"] = "READY"
                        self._universe_worker_status["stage"] = "IDLE"
                        self._universe_worker_status["blocked_reason"] = None
                        self._universe_worker_status["last_finished"] = utc_now().isoformat()
                        self._universe_worker_status["heartbeat_at"] = utc_now().isoformat()
                        self._universe_worker_status["progress_at"] = utc_now().isoformat()
                        if self._universe_worker_status["completed"] % 10 == 0:
                            self.scan_queue.prune_terminal(
                                max_rows=env_num("SCAN_QUEUE_TERMINAL_ROW_LIMIT", 10000, int),
                                retain_days=env_num("SCAN_QUEUE_RETENTION_DAYS", 14, int),
                            )
                    self.scan_queue.state_set("market_director_stats", self.market_directors.stats_snapshot())
                except Exception as exc:
                    lease_stop.set()
                    lease_thread.join(timeout=2.0)
                    self.scan_queue.fail(job_id, f"{type(exc).__name__}: {exc}", retry_delay_seconds=min(90.0, (1.5 * (2 ** max(0, int(job.get("attempts", 1)) - 1))) * random.uniform(0.85, 1.15)), worker_id=worker_id)
                    self._attempt_scan_self_heal(exc, job_type=job_type)
                    with self._universe_lock:
                        self._universe_worker_status["state"] = "ERROR"
                        self._universe_worker_status["stage"] = job_type
                        self._universe_worker_status["last_error"] = f"{type(exc).__name__}: {exc}"[:500]
                        self._universe_worker_status["failed"] = int(self._universe_worker_status.get("failed", 0)) + 1
                        self._universe_worker_status["blocked_reason"] = "Last scan job failed; check last_error and queue retry state"
                        self._universe_worker_status["heartbeat_at"] = utc_now().isoformat()
                        self._universe_worker_status["last_finished"] = utc_now().isoformat()
                    if job_type == "specialist_review":
                        candidate_data = payload.get("director_result") if isinstance(payload.get("director_result"), dict) else {}
                        self.market_directors.record_handoff(candidate_data, completed=False, error=f"{type(exc).__name__}: {exc}")
                        self.scan_queue.state_set("market_director_stats", self.market_directors.stats_snapshot())
                    self.error(f"{job_type} failed for {broker_symbol}: {exc}")
            except Exception as exc:
                self._universe_worker_status.update({"state": "ERROR", "last_error": f"{type(exc).__name__}: {exc}"[:500]})
                self.error(f"Market director worker loop failed: {exc}")
                time.sleep(1)
        self._universe_worker_status["state"] = "STOPPED"

    def _refresh_universe_analysis(self, primary_snapshot, primary_mesh):
        """Publish the selected-symbol result and enqueue background broker-universe scans."""
        primary_row = self._universe_row(primary_snapshot, primary_mesh, primary=True)
        pkey = str(primary_row.get("broker_symbol") or primary_snapshot.symbol).strip().upper()
        primary_row["symbol"] = pkey
        # Screen the active symbol with the same numeric directors. Reuse the single
        # registry row below rather than issuing duplicate lookups during each UI poll.
        instrument = {}
        try:
            if self.feed is not None and self.mt5_connected:
                self._sync_instrument_registry()
            broker_symbol = str(primary_row.get("broker_symbol") or primary_snapshot.symbol).strip()
            instrument = self.instrument_registry.get(broker_symbol)
            if not instrument and broker_symbol.upper() != pkey.upper():
                instrument = self.instrument_registry.get(pkey)
            instrument = instrument or {
                "broker_symbol": pkey, "broker_symbol_exact": broker_symbol, "market_desk": "unknown",
                "underlying_market": "unknown", "product_type": "unknown", "classification_status": "needs_review",
                "entry_eligible": False, "entry_metadata_ready": False, "review_reasons": ["No verified instrument registry row is available."],
                "raw_metadata": {"name": broker_symbol},
            }
            director_result = self.market_directors.evaluate(instrument, primary_snapshot)
            base_decision = primary_row.get("desk_decision", "WAIT")
            base_score = primary_row.get("desk_score", 0.0)
            base_thesis = primary_row.get("thesis", "")
            base_reasons = primary_row.get("reasons", [])
            base_views = primary_row.get("agent_views", [])
            base_count = primary_row.get("agent_count", len(base_views))
            self._apply_market_director_result(primary_row, director_result, instrument, primary=True)
            primary_row.update({"desk_decision": base_decision, "desk_score": base_score, "thesis": base_thesis,
                                "reasons": base_reasons, "agent_views": base_views, "agent_count": base_count,
                                "specialist_decision": base_decision,
                                "specialist_review_status": "ACTIVE_PIPELINE_ALREADY_REVIEWED" if director_result.get("candidate") else "NOT_ROUTED"})
            primary_row["primary_symbol"] = True
            # Count a given active-symbol closed-bar state once, not once per web poll.
            tf = (director_result.get("market_features") or {}).get("timeframes") or {}
            bar_key = "|".join(str((tf.get(name) or {}).get("last_closed_bar_utc") or "") for name in ("M5", "M15", "H1"))
            previous_bar_key = self.scan_queue.state_get("market_director_active_bar_key", "")
            if bar_key and bar_key != previous_bar_key:
                self.market_directors.record_scan(director_result)
                self.market_directors.record_research_result({**director_result,
                    "currency_base": instrument.get("currency_base"),
                    "currency_profit": instrument.get("currency_profit"),
                    "scanned_epoch": director_result.get("scanned_epoch") or time.time()}, instrument)
                if director_result.get("candidate"):
                    # The active symbol already traversed the specialist pipeline.
                    self.market_directors.record_handoff(director_result, completed=True)
                self.scan_queue.state_set("market_director_active_bar_key", bar_key)
                self.scan_queue.state_set("market_director_stats", self.market_directors.stats_snapshot())
        except Exception as director_exc:
            primary_row["market_director_error"] = f"{type(director_exc).__name__}: {director_exc}"[:240]
        primary_row["market_desk"] = str(instrument.get("market_desk") or primary_row.get("market_desk") or "unknown")
        primary_row["entry_eligible_by_registry"] = bool(instrument.get("entry_eligible"))
        primary_row.update({"scan_job_type": "active_symbol_pipeline", "scan_job_status": "SUCCEEDED",
                            "scan_completed": True, "scan_job_completed_at": utc_now().isoformat()})
        with self._universe_lock:
            self._universe_results[pkey] = primary_row
        if not self.analyze_all_symbols_enabled:
            self.universe_state = {
                "enabled": False, "status": "Disabled — active symbol only", "total_available": 1,
                "analyzed_count": 1, "scan_limit": self.all_symbol_scan_limit or "ALL",
                "note": "Enable all-symbol analysis in Settings to enqueue broker-listed instruments. Secondary-symbol results never submit orders automatically.",
                "registry": self.instrument_registry.summary(), "orchestration": self.scan_queue.stats(),
                "worker": dict(self._universe_worker_status), "results": [primary_row],
            }
            return
        if self.feed is None or self.demo_mode or not self.mt5_connected:
            self.universe_state = {
                "enabled": True, "status": "Waiting for a connected MT5 market feed", "total_available": 0,
                "analyzed_count": 0, "scan_limit": self.all_symbol_scan_limit or "ALL",
                "scan_progress": {"available_symbols": 0, "completed_symbols": 0, "remaining_symbols": 0,
                                  "failed_symbols": 0, "coverage_pct": 0.0, "basis": "successfully completed background scans only"},
                "note": "The active symbol can still have a selected-symbol analysis, but broker-universe scanning is blocked until real MT5 catalogue and rates are available.",
                "registry": self.instrument_registry.summary(), "orchestration": self.scan_queue.stats(),
                "worker": dict(self._universe_worker_status), "results": [primary_row],
            }
            return
        sync_status = self._sync_instrument_registry()
        # Initial startup may have an MT5 connection but the cadence check is still zero;
        # retry one immediate sync if the first attempt returned no rows.
        if self.instrument_registry.summary().get("total_active", 0) == 0:
            sync_status = self._sync_instrument_registry(force=True)
        try:
            available = self._available_analysis_symbols()
            enqueued = self._enqueue_universe_jobs(available)
            # Candidate rows are already compacted in the worker's hot cache. Complete
            # outcomes and detailed analyst evidence remain in the durable result store.
            # This avoids per-symbol registry.get() calls and constructing placeholder
            # objects for every instrument on every dashboard refresh.
            with self._universe_lock:
                self._universe_results[pkey] = primary_row
                all_rows = list(self._universe_results.values())
            results = []
            for row in all_rows:
                status = str(row.get("director_candidate_status") or row.get("candidate_status") or "").upper()
                side = str(row.get("candidate_side") or "").upper()
                if status == "CANDIDATE" and side in {"BUY", "SELL"} and row.get("quote_fresh") is not False:
                    results.append(row)
            results.sort(key=lambda x: (not bool(x.get("primary_symbol")), -float(x.get("scanned_epoch") or 0), str(x.get("symbol", ""))))
            compact_results = []
            for row in results:
                compact = {k: v for k, v in row.items() if k != "agent_views"}
                compact["agent_count"] = int(row.get("agent_count") or len(row.get("agent_views") or []))
                compact_results.append(compact)
            coverage_counts = self.scan_queue.coverage_counts()
            succeeded_count = min(len(available), int(coverage_counts.get("succeeded", 0) or 0) + int(bool(primary_row.get("scan_completed"))))
            failed_count = min(len(available), int(coverage_counts.get("failed", 0) or 0))
            progress_count = succeeded_count
            coverage = round(progress_count / len(available) * 100.0, 2) if available else 0.0
            queue_stats = self.scan_queue.stats() or {}
            worker_state = str(self._universe_worker_status.get("state") or "UNKNOWN")
            blocked_reason = self._universe_worker_status.get("blocked_reason")
            scanner_status = (
                "Scanner paused" if worker_state == "PAUSED" else
                "Scanner disabled in settings" if worker_state == "DISABLED" else
                "Waiting for a connected MT5 market feed" if worker_state == "WAITING_FOR_FEED" else
                "Scanner requires attention" if worker_state in {"ERROR", "STOPPED"} else
                "Background scanner processing" if worker_state in {"RUNNING", "READY"} else
                "Background scanner waiting for due jobs"
            )
            self.universe_state = {
                "enabled": True,
                "status": scanner_status,
                "total_available": len(available),
                "analyzed_count": progress_count,
                "queued_count": int(queue_stats.get("queued", 0) or 0),
                "scan_progress_basis": "unique symbols with a successful completed background scan or completed active-symbol cycle",
                "scan_progress": {
                    "available_symbols": len(available), "completed_symbols": progress_count,
                    "remaining_symbols": max(0, len(available) - progress_count),
                    "failed_symbols": failed_count,
                    "coverage_pct": coverage,
                    "jobs_succeeded": int(queue_stats.get("succeeded", 0) or 0),
                    "jobs_running": int(queue_stats.get("running", 0) or 0),
                    "jobs_queued": int(queue_stats.get("queued", 0) or 0),
                    "jobs_failed": int(queue_stats.get("failed", 0) or 0),
                    "basis": "successful completed scans; a missing candidate is not counted as a failed scan",
                },
                "blocked_reason": blocked_reason,
                "scan_limit": self.all_symbol_scan_limit or "ALL",
                "note": (
                    "Continuous broker-universe scan (24/7 worker). The default scan limit is ALL broker-listed symbols. "
                    "With TRADE_UNIVERSE_ENABLED, specialist BUY/SELL can execute through the central risk gateway "
                    f"(max {self.trade_universe_max_per_cycle}/min). Coverage = completed scans."
                ),
                "registry": self.instrument_registry.summary(),
                "registry_sync": sync_status,
                "orchestration": queue_stats,
                "queue_dispatch": enqueued,
                "worker": dict(self._universe_worker_status),
                "results": compact_results,
            }
        except Exception as exc:
            self.universe_state = {
                "enabled": True, "status": f"Universe scan error: {type(exc).__name__}",
                "total_available": 0, "analyzed_count": 1, "scan_limit": self.all_symbol_scan_limit or "ALL",
                "note": str(exc), "registry": self.instrument_registry.summary(),
                "orchestration": self.scan_queue.stats(), "worker": dict(self._universe_worker_status),
                "results": [primary_row],
            }

    def live_ready(self):
        return bool(self.config.live_trading_enabled and not self.config.paper_trading and self.live_armed and not self.kill_switch and self.feed is not None and not self.demo_mode)

    def _client_id(self, snap, packet):
        m5 = (getattr(snap, "candles", None) or {}).get("M5") or []
        try:
            bar_time = m5[-1].timestamp.isoformat() if m5 else snap.timestamp.isoformat()
        except Exception:
            bar_time = str(getattr(snap, "timestamp", "") or "")
        plan = getattr(packet, "plan", None)
        decision = getattr(getattr(packet, "decision", None), "value", None) or str(getattr(packet, "decision", "WAIT") or "WAIT")
        if plan is not None:
            symbol = str(getattr(plan, "symbol", None) or getattr(snap, "symbol", "") or "")
            try:
                entry = float(getattr(plan, "entry", 0) or 0)
                stop = float(getattr(plan, "stop", 0) or 0)
                target = float(getattr(plan, "target", 0) or 0)
            except (TypeError, ValueError):
                entry = stop = target = 0.0
            raw = f"{symbol}|{decision}|{bar_time}|{entry:.8f}|{stop:.8f}|{target:.8f}"
        else:
            # WAIT / no-plan cycles still need a stable attribution id
            symbol = str(getattr(snap, "symbol", "") or "")
            try:
                px = float(getattr(snap, "price", 0) or 0)
            except (TypeError, ValueError):
                px = 0.0
            raw = f"{symbol}|{decision}|{bar_time}|{px:.8f}|0|0"
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    def finalize_trade_learning(self, ticket, realized_pnl):
        """Finalize a linked learning label once an actual position is closed."""
        try:
            if realized_pnl is None or float(realized_pnl) == 0.0:
                return {"enabled": True, "mode": "skipped", "note": "Zero-PnL/ambiguous close not labelled"}
            attribution = self.performance.decision_for_ticket(ticket)
            if not attribution or not attribution.get("client_id"):
                return {"enabled": True, "mode": "unlinked", "note": "No originating decision record linked to this ticket"}
            decision_side = str(attribution.get("decision") or "").upper()
            contributions = {}
            for sig in attribution.get("signals") or []:
                agent = str(sig.get("agent") or sig.get("name") or "UNKNOWN")
                raw_side = str(sig.get("side") or "FLAT").upper().split(".")[-1]
                try:
                    magnitude = abs(float(sig.get("score") or 0)) * max(0.0, min(1.0, float(sig.get("confidence") or 0)))
                except (TypeError, ValueError):
                    magnitude = 0.0
                if raw_side in {"BUY", "SELL"} and decision_side in {"BUY", "SELL"}:
                    contributions[agent] = magnitude if raw_side == decision_side else -magnitude
            result = self.agent_mesh.finalize_learning_outcome(
                attribution["client_id"], float(realized_pnl) > 0.0, contributions
            )
            self.ai_mesh_state["adaptive_learning"] = result
            return result
        except Exception as exc:
            self.error(f"Adaptive learning outcome finalization failed: {exc}")
            return {"enabled": True, "mode": "error", "error": str(exc)}

    def _broker_exposure_after(self, adapter, plan, volume, equity, current_margin):
        tick = adapter.mt5.symbol_info_tick(plan.symbol)
        if tick is None:
            raise MT5ExecutionError(f'No tick for exposure preflight: {plan.symbol}')
        price = float(tick.ask if plan.side.value == 'BUY' else tick.bid)
        order_type = adapter._order_type(plan.side)
        margin = adapter.mt5.order_calc_margin(order_type, plan.symbol, volume, price)
        if margin is None:
            raise MT5ExecutionError(f'order_calc_margin failed: {adapter.mt5.last_error()}')
        projected = current_margin + abs(float(margin))
        return (projected / equity * 100.0) if equity > 0 else 999999.0, float(margin)

    def attempt_live_execution(self, snap, packet, risk, acct):
        """The single authorized live-entry gateway used by all trade engines.

        This method intentionally delegates to one risk-gated implementation. Callers
        must never fall back to MT5ExecutionAdapter.submit() if this gateway is absent.
        """
        return self._attempt_live_execution(snap, packet, risk, acct)


    def quote_price(self, symbol: str) -> float:
        """Best-effort last price for paper MTM across symbols (MT5 tick or 0)."""
        sym = str(symbol or "").strip()
        if not sym:
            return 0.0
        try:
            if self.feed is not None and getattr(self.feed, "mt5", None) is not None:
                resolved = str(resolve_mt5_symbol(self.feed.mt5, sym) or sym)
                tick = self.feed.mt5.symbol_info_tick(resolved)
                if tick is not None:
                    bid = float(getattr(tick, "bid", 0) or 0)
                    ask = float(getattr(tick, "ask", 0) or 0)
                    last = float(getattr(tick, "last", 0) or 0)
                    if bid > 0 and ask > 0:
                        return (bid + ask) / 2.0
                    return last or bid or ask or 0.0
        except Exception:
            pass
        try:
            # Fallback: any open paper position mark
            if hasattr(self, "positions") and self.positions is not None:
                for p in self.positions.list():
                    if str(p.get("symbol")) == sym and float(p.get("price_current") or 0) > 0:
                        return float(p.get("price_current"))
        except Exception:
            pass
        return 0.0

    def _try_universe_execution(self, broker_symbol: str, snapshot, pair_decision, row: dict) -> dict:
        """Risk-gated trade path for secondary (universe) symbols.

        Uses the same TradeEngine / RiskAgent stack as the primary symbol.
        Paper by default; live only when the account is armed and kill switch is off.
        Rate-limited per minute to avoid flooding the broker with entries.
        """
        if not self.trade_universe_enabled:
            return {"status": "DISABLED", "reason": "TRADE_UNIVERSE_ENABLED is false"}
        # Kill switch blocks LIVE only; paper universe fills remain allowed for validation.
        if self.kill_switch and not self.config.paper_trading:
            return {"status": "KILL_SWITCH", "reason": "Kill switch is active (live entries blocked)"}
        if not getattr(self, "auto_trade", True):
            return {"status": "AUTO_TRADE_OFF", "reason": "Auto-trade is disabled"}
        decision = str(getattr(pair_decision, "decision", "WAIT") or "WAIT").upper()
        if decision not in {"BUY", "SELL"}:
            return {"status": "NO_PLAN", "reason": f"Specialist decision is {decision}"}
        # Fresh quote required
        meta = getattr(snapshot, "metadata", None) or {}
        if meta.get("quote_fresh") is not True:
            return {
                "status": "STALE_MARKET_DATA",
                "reason": f"Quote not fresh ({meta.get('quote_status')})",
            }
        # Session: skip clearly closed non-crypto instruments
        if meta.get("market_open") is False:
            return {"status": "MARKET_CLOSED", "reason": str((meta.get("session") or {}).get("reason") or "session closed")}
        # Rate limit
        minute_key = time.strftime("%Y%m%d%H%M", time.gmtime())
        if minute_key != self._universe_trades_minute_key:
            self._universe_trades_minute_key = minute_key
            self._universe_trades_this_minute = 0
        if self._universe_trades_this_minute >= self.trade_universe_max_per_cycle:
            return {
                "status": "RATE_LIMITED",
                "reason": f"Max {self.trade_universe_max_per_cycle} universe trades/minute reached",
            }
        # Registry gate when available
        try:
            check = self.instrument_registry.entry_check(broker_symbol, decision)
            if check is not None and check.get("allowed") is False:
                return {
                    "status": "REGISTRY_BLOCKED",
                    "reason": str(check.get("reason") or "Instrument not entry-eligible"),
                }
        except Exception:
            pass
        # Build plan from mesh decision
        from trading_team.core.models import Decision, DecisionPacket, TradePlan
        entry = getattr(pair_decision, "entry", None)
        stop = getattr(pair_decision, "stop", None)
        target = getattr(pair_decision, "target", None)
        conf = float(getattr(pair_decision, "confidence", 0.0) or 0.0)
        side = getattr(pair_decision, "side", None)
        if entry is None or stop is None or target is None or side is None:
            return {"status": "NO_PLAN", "reason": "Mesh did not supply entry/stop/target"}
        min_conf = float(os.getenv("MIN_FINAL_CONFIDENCE", "0.52"))
        if conf < min_conf:
            return {"status": "LOW_CONFIDENCE", "reason": f"Confidence {conf:.2f} below floor {min_conf:.2f}"}
        reasons = list(getattr(pair_decision, "reasons", None) or [])
        plan = TradePlan(
            str(broker_symbol),
            side,
            float(entry),
            float(stop),
            float(target),
            conf,
            reasons[:8],
        )
        packet = DecisionPacket(Decision(decision), str(broker_symbol), conf, plan, reasons[:8])
        # Account snapshot
        try:
            acct = self.account_snapshot() if hasattr(self, "account_snapshot") else {
                "equity": float((self.account or {}).get("equity") or 10000),
                "daily_pnl": float((self.account or {}).get("daily_pnl") or 0),
                "exposure": float((self.account or {}).get("exposure") or 0),
                "open_positions": int((self.account or {}).get("open_positions") or 0),
            }
        except Exception:
            acct = {"equity": 10000.0, "daily_pnl": 0.0, "exposure": 0.0, "open_positions": 0}
        try:
            from trading_team.agents.risk import RiskAgent
            risk = RiskAgent(self.config.risk).evaluate(
                snapshot, plan, acct["equity"], acct["daily_pnl"], acct["exposure"], acct["open_positions"]
            )
        except Exception as risk_exc:
            return {"status": "RISK_ERROR", "reason": str(risk_exc)[:240]}
        if not risk.approved:
            return {
                "status": "RISK_BLOCKED",
                "reason": str(risk.reason or "risk rejected"),
                "blockers": list(risk.blockers or [])[:6],
            }
        try:
            if not hasattr(self, "trade_engine") or self.trade_engine is None:
                from trading_team.adapters.trade_engine import TradeEngine
                self.trade_engine = TradeEngine(self)
            execution = self.trade_engine.execute(snapshot, packet, risk, acct)
            status = str(execution.get("status") or "")
            if status in {"PAPER_FILLED", "FILLED_VERIFIED", "SUBMITTED", "SUBMITTED_UNCONFIRMED"}:
                self._universe_trades_this_minute += 1
                try:
                    # Attribute universe fills so Performance / agent win rates update
                    cid = self._client_id(snapshot, packet) if hasattr(self, "_client_id") else f"univ-{broker_symbol}-{int(time.time())}"
                    reports = list(getattr(pair_decision, "reports", None) or [])
                    sig_payload = []
                    for r in reports:
                        try:
                            sig_payload.append({
                                "agent": str(getattr(r, "agent", None) or r.get("agent") or "UNKNOWN"),
                                "side": str(getattr(getattr(r, "side", None), "value", None) or getattr(r, "side", None) or (r.get("side") if isinstance(r, dict) else "FLAT")),
                                "confidence": float(getattr(r, "confidence", None) or (r.get("confidence") if isinstance(r, dict) else 0) or 0),
                                "score": float(getattr(r, "score", None) or (r.get("score") if isinstance(r, dict) else 0) or 0),
                            })
                        except Exception:
                            continue
                    self.performance.record_decision(
                        cid, str(broker_symbol), decision, conf,
                        float(entry), float(stop), float(target), sig_payload,
                    )
                    ticket = execution.get("position_ticket") or execution.get("ticket")
                    if ticket:
                        self.performance.attach_position(cid, ticket)
                except Exception as attr_exc:
                    try:
                        self.error(f"Universe performance attribution failed: {attr_exc}")
                    except Exception:
                        pass
                self.log_event("universe_trade_executed", {
                    "symbol": broker_symbol,
                    "side": decision,
                    "status": status,
                    "confidence": conf,
                })
            else:
                self.log_event("universe_trade_blocked", {
                    "symbol": broker_symbol,
                    "side": decision,
                    "status": status,
                    "reason": execution.get("reason"),
                })
            return execution if isinstance(execution, dict) else {"status": str(execution)}
        except Exception as exec_exc:
            return {"status": "EXEC_ERROR", "reason": f"{type(exec_exc).__name__}: {exec_exc}"[:300]}

    @_serialized_runtime_mt5
    def attempt_live_pending_order(self, symbol: str, side: str, volume: float, price: float,
                                   order_kind: str, sl: float, tp: float) -> dict:
        """Authorized gateway for operator-requested pending entries.

        Pending orders share the same arm/kill, exact-symbol registry, fresh-quote,
        broker-contract, loss-budget and margin preflight as market entries. They are
        not allowed to bypass the central live-entry controls.
        """
        if not self.live_ready() or not self.account_live:
            return {"status": "LIVE_GATE_BLOCKED", "reason": "Pending entries require live mode, live authorization, a connected live account and a released kill switch."}
        side_u = str(side or "").upper()
        kind = str(order_kind or "").upper().replace("-", "_")
        if side_u not in {"BUY", "SELL"} or kind not in {"LIMIT", "STOP"}:
            return {"status": "INVALID_PENDING_PLAN", "reason": "Only BUY/SELL LIMIT and STOP entries are supported; STOP_LIMIT is not enabled."}
        try:
            requested = str(symbol or "").strip()
            if not requested:
                raise MT5ExecutionError("Broker symbol is required")
            bridge = self.feed.get_bridge()
            exact_name = str(self.feed.resolve(requested) or "").strip()
            if not exact_name or exact_name.upper() != requested.upper():
                raise MT5ExecutionError("Pending entries require the exact broker-listed symbol name; aliases are not accepted")
            metadata = bridge.symbol_info(exact_name)
            actual_name = str(metadata.get("broker_symbol") or metadata.get("name") or "").strip()
            if not actual_name or actual_name.upper() != exact_name.upper():
                raise MT5ExecutionError("Fresh broker contract metadata does not match the requested symbol")
            self.instrument_registry.sync([metadata], complete_snapshot=False, source_id="pending_entry_preflight")
            registry_check = self.instrument_registry.entry_check(exact_name, side_u)
            if not registry_check.get("allowed"):
                raise MT5ExecutionError("Instrument registry rejected pending entry: " + str(registry_check.get("reason", "unverified symbol")))
            tick = self.feed.live_tick(exact_name)
            fresh, age, quote_status = assess_quote_freshness(tick)
            if not fresh:
                raise MT5ExecutionError(f"Broker quote is not fresh ({quote_status}; age={age}s)")
            adapter = MT5ExecutionAdapter(self.feed, DB_PATH, self.config.magic, self.config.deviation)
            volume = adapter._normalize_volume(exact_name, float(volume))
            price, sl, tp = float(price), float(sl), float(tp)
            if min(price, sl, tp) <= 0:
                raise MT5ExecutionError("Pending entry price, stop-loss and take-profit must all be positive")
            if side_u == "BUY" and not sl < price < tp:
                raise MT5ExecutionError("BUY pending plan requires SL < entry < TP")
            if side_u == "SELL" and not tp < price < sl:
                raise MT5ExecutionError("SELL pending plan requires TP < entry < SL")
            # Model the planned stop-loss in account currency. Reject orders which
            # exceed the same per-position equity-risk ceiling as algorithmic entries.
            mt5 = self.feed.mt5
            order_type = getattr(mt5, "ORDER_TYPE_BUY", 0) if side_u == "BUY" else getattr(mt5, "ORDER_TYPE_SELL", 1)
            projected_loss = mt5.order_calc_profit(order_type, exact_name, volume, price, sl)
            if projected_loss is None or float(projected_loss) >= 0:
                raise MT5ExecutionError("Broker loss calculation did not confirm a loss at the proposed stop")
            account = bridge.account_info()
            equity = float(account.get("equity") or 0.0)
            if equity <= 0:
                raise MT5ExecutionError("Account equity is unavailable")
            max_loss = equity * float(self.config.risk.max_position_risk_pct) / 100.0
            loss_cash = abs(float(projected_loss))
            if loss_cash > max_loss * 1.001:
                raise MT5ExecutionError(f"Pending stop risk {loss_cash:.2f} exceeds the per-position limit {max_loss:.2f}")
            margin = mt5.order_calc_margin(order_type, exact_name, volume, price)
            if margin is None:
                raise MT5ExecutionError("Broker margin calculation failed for pending order")
            current_margin = float(account.get("margin") or 0.0)
            projected_margin_pct = (current_margin + abs(float(margin))) / equity * 100.0
            if projected_margin_pct > float(self.config.risk.max_account_exposure_pct):
                raise MT5ExecutionError(f"Projected margin use {projected_margin_pct:.2f}% exceeds the configured cap {self.config.risk.max_account_exposure_pct:.2f}%")
            result = adapter.place_pending(exact_name, side_u, volume, price, kind, sl, tp)
            self.log_event("pending_order_lifecycle", {"symbol": exact_name, "side": side_u, "kind": kind,
                                "volume": volume, "price": price, "sl": sl, "tp": tp,
                                "loss_budget": max_loss, "estimated_loss": loss_cash, "result": result})
            return {**result, "symbol": exact_name, "estimated_loss": loss_cash, "max_loss": max_loss,
                    "projected_margin_pct": projected_margin_pct}
        except Exception as exc:
            self.log_event("pending_order_blocked", {"symbol": str(symbol or ""), "side": side_u, "kind": kind, "reason": str(exc)[:1000]})
            return {"status": "PENDING_ORDER_BLOCKED", "reason": str(exc)}

    @_serialized_runtime_mt5
    def _attempt_live_execution(self, snap, packet, risk, acct):
        diagnostics = []
        def mark(code, label, passed, detail):
            diagnostics.append({"code": code, "label": label, "passed": bool(passed), "detail": str(detail)})
        self.execution_diagnostics = diagnostics
        if not risk or not risk.approved or not packet.plan:
            mark("PLAN", "Actionable trade plan", False, "No approved trade plan")
            return {'status': 'RISK_BLOCKED' if risk and not risk.approved else 'NO_PLAN', 'reason': risk.reason if risk and not risk.approved else 'No actionable plan'}
        mark("PLAN", "Actionable trade plan", True, f"{packet.plan.side.value} {packet.plan.symbol}")
        if self.config.paper_trading:
            mark("MODE", "Live mode", False, "TRADING_MODE=paper")
            return {'status': 'PAPER_HOLD', 'reason': 'TRADING_MODE=paper'}
        mark("MODE", "Live mode", True, "TRADING_MODE=live")
        if not self.config.live_trading_enabled:
            mark("LIVE_DISABLED", "Live execution enabled", False, "LIVE_TRADING_ENABLED=false")
            return {'status': 'LIVE_DISABLED', 'reason': 'LIVE_TRADING_ENABLED is false'}
        mark("LIVE_ENABLED", "Live execution enabled", True, "LIVE_TRADING_ENABLED=true")
        if self.kill_switch:
            mark("KILL_SWITCH", "Master kill switch released", False, "Kill switch is ON")
            return {'status': 'KILL_SWITCH_ACTIVE', 'reason': 'Kill switch is ON'}
        mark("KILL_SWITCH", "Master kill switch released", True, "Kill switch is OFF")
        if not self.live_armed:
            mark("ARM", "Live execution armed", False, "ARM LIVE has not been confirmed")
            return {'status': 'LIVE_NOT_ARMED', 'reason': 'Live execution requires explicit ARM LIVE confirmation'}
        mark("ARM", "Live execution armed", True, "Runtime is armed")
        if self.feed is None or self.demo_mode or not self.account_live:
            mark("MT5", "Live MT5/account state", False, "MT5 feed or account synchronization unavailable")
            return {'status': 'NO_LIVE_MT5', 'reason': 'Live execution requires a connected MT5 feed and successful account synchronization'}
        mark("MT5", "Live MT5/account state", True, f"Connected account sync={self.account_live}")

        snapshot_metadata = getattr(snap, "metadata", {}) or {}
        clock_status = str(snapshot_metadata.get("timestamp_integrity_status") or "")
        clock_blockers = {
            "timestamp_offset_configuration_conflict",
            "tick_time_fields_disagree",
            "tick_and_bar_timestamps_in_future",
            "tick_timestamp_in_future",
            "bar_timestamp_in_future",
        }
        if clock_status in clock_blockers:
            reason = (
                "Source clock integrity is not verified "
                f"({clock_status}); refresh and reconcile MT5 tick/bar timestamps before new entries."
            )
            mark("SOURCE_CLOCK_INTEGRITY", "Consistent MT5 tick and candle timestamps", False, reason)
            return {"status": "SOURCE_CLOCK_INTEGRITY_BLOCKED", "reason": reason, "diagnostics": diagnostics}
        mark("SOURCE_CLOCK_INTEGRITY", "Consistent MT5 tick and candle timestamps", True,
             clock_status or "No MT5 timestamp-integrity metadata was supplied by this feed")

        # Canonicalize the live order to the broker's exact symbol and refresh its
        # contract metadata immediately before sizing/submission. A logical alias is
        # never sent straight to MT5, and an ambiguous/unclassified contract fails closed.
        requested_symbol = str(getattr(packet.plan, "symbol", "") or getattr(snap, "symbol", "")).strip()
        snapshot_broker_symbol = str((getattr(snap, "metadata", {}) or {}).get("broker_symbol") or "").strip()
        try:
            # Re-resolve against the current terminal catalogue rather than trusting
            # a cached logical alias. If two suffix variants now exist, ambiguity
            # blocks the order instead of retaining a stale first match.
            broker_symbol = str(resolve_mt5_symbol(self.feed.mt5, requested_symbol) or "").strip()
            if not broker_symbol:
                raise MT5ExecutionError(f"Broker symbol {requested_symbol!r} is missing or ambiguous in the current catalogue")
            if snapshot_broker_symbol and broker_symbol.upper() != snapshot_broker_symbol.upper():
                mark("SYMBOL_IDENTITY", "Snapshot and broker symbol match", False,
                     f"snapshot={snapshot_broker_symbol}; order_resolution={broker_symbol}")
                return {"status": "SYMBOL_IDENTITY_MISMATCH", "reason": "Market snapshot and live order resolve to different broker symbols", "diagnostics": diagnostics}
            bridge = self.feed.get_bridge()
            metadata = bridge.symbol_info(broker_symbol)
            exact_name = str(metadata.get("broker_symbol") or metadata.get("name") or "").strip()
            if not exact_name or exact_name.upper() != broker_symbol.upper():
                mark("SYMBOL_METADATA", "Live broker contract metadata", False, "Fresh symbol metadata does not match the resolved broker symbol")
                return {"status": "BROKER_PREFLIGHT_REJECTED", "reason": "Fresh broker contract metadata does not match the resolved symbol", "diagnostics": diagnostics}
            self.instrument_registry.sync([metadata], complete_snapshot=False, source_id="live_order_preflight")
            registry_check = self.instrument_registry.entry_check(broker_symbol, packet.plan.side.value)
            if not registry_check.get("allowed"):
                mark("INSTRUMENT_REGISTRY", "Broker symbol and contract registry", False, registry_check.get("reason", "Symbol registry rejected the entry"))
                return {"status": "INSTRUMENT_REGISTRY_BLOCKED", "symbol": broker_symbol,
                        "reason": registry_check.get("reason", "Symbol registry rejected the entry"), "diagnostics": diagnostics}
            mark("INSTRUMENT_REGISTRY", "Broker symbol and contract registry", True,
                 f"{broker_symbol}: {registry_check.get('reason', 'metadata approved')}")
            # The exact broker name is required by MT5's APIs and remains the identity
            # used for duplicate checks, order storage and post-fill reconciliation.
            packet.plan.symbol = exact_name
            packet.symbol = exact_name
            tick = self.feed.live_tick(exact_name)
            fresh, age, quote_status = assess_quote_freshness(tick)
            if not fresh:
                mark("FINAL_QUOTE", "Fresh broker quote at entry preflight", False,
                     f"{quote_status}; corrected age={age}s; symbol={exact_name}")
                return {"status": "STALE_MARKET_DATA", "symbol": exact_name,
                        "reason": f"Final broker quote rejected: {quote_status} (age={age}s)", "diagnostics": diagnostics}
            mark("FINAL_QUOTE", "Fresh broker quote at entry preflight", True,
                 f"{exact_name}; corrected age={age:.3f}s")
        except Exception as exc:
            mark("SYMBOL_PREFLIGHT", "Broker symbol resolution and metadata", False, str(exc))
            return {"status": "BROKER_PREFLIGHT_REJECTED", "reason": str(exc), "diagnostics": diagnostics}

        client_id = self._client_id(snap, packet)
        store = OrderStore(DB_PATH)
        recent = store.recent_same_signal(packet.plan.symbol, packet.plan.side.value, self.config.trade_cooldown_seconds)
        if recent:
            mark("DUPLICATE", "Duplicate/cooldown guard", False, f"Recent {packet.plan.side.value} signal already processed ({recent[1]})")
            return {'status':'DUPLICATE_BLOCKED','reason':f'Recent {packet.plan.side.value} signal already processed ({recent[1]})','client_id':recent[0]}
        mark("DUPLICATE", "Duplicate/cooldown guard", True, "No matching recent signal")
        if self.config.one_position_per_symbol:
            try:
                live_positions = [
                    {'ticket': getattr(p,'ticket',0), 'symbol': getattr(p,'symbol',''), 'magic': getattr(p,'magic',0)}
                    for p in MT5ExecutionAdapter(self.feed, DB_PATH, self.config.magic, self.config.deviation).positions(packet.plan.symbol)
                ]
            except Exception as exc:
                live_positions = []
                mark("POSITION_QUERY", "Existing-position query", False, f"{exc}")
            canonical_symbol_key = str(packet.plan.symbol).upper()
            existing = [p for p in live_positions if str(p.get('symbol') or '').upper() == canonical_symbol_key and int(p.get('magic') or 0) == self.config.magic]
            if existing:
                mark("ONE_POSITION", "One-position-per-symbol guard", False, f"Managed position(s) already open: {[str(p.get('ticket')) for p in existing]}")
                return {'status':'POSITION_EXISTS','reason':'One-position-per-symbol guard','tickets':[str(p.get('ticket')) for p in existing]}
        mark("ONE_POSITION", "One-position-per-symbol guard", True, "No managed position is blocking entry")

        adapter = MT5ExecutionAdapter(self.feed, DB_PATH, self.config.magic, self.config.deviation)
        self.trade_attempts += 1
        self.activity = 'BROKER RISK SIZING / PREFLIGHT'
        try:
            volume, sizing = adapter.risk_size(packet.plan, risk.max_loss)
            mark("VOLUME", "Broker volume/risk sizing", True, f"volume={volume}")
        except Exception as exc:
            mark("VOLUME", "Broker volume/risk sizing", False, str(exc))
            return {'status':'BROKER_PREFLIGHT_REJECTED','reason':str(exc),'diagnostics':diagnostics}
        current_margin = float(acct.get('margin_used', 0.0) or 0.0)
        try:
            exposure_after, margin_needed = self._broker_exposure_after(adapter, packet.plan, volume, float(acct['equity']), current_margin)
            passed = exposure_after <= self.config.risk.max_account_exposure_pct
            mark("MARGIN", "Projected margin exposure", passed, f"projected={exposure_after:.3f}% limit={self.config.risk.max_account_exposure_pct:.3f}%")
            if not passed:
                return {'status':'BROKER_RISK_BLOCKED','reason':f'Projected margin exposure {exposure_after:.2f}% exceeds limit {self.config.risk.max_account_exposure_pct:.2f}%','volume':volume,'margin_needed':margin_needed,'exposure_after':exposure_after,'diagnostics':diagnostics}
        except Exception as exc:
            mark("MARGIN", "Projected margin exposure", False, str(exc))
            return {'status':'BROKER_PREFLIGHT_REJECTED','reason':str(exc),'diagnostics':diagnostics}
        risk.approved = True; risk.quantity = volume; risk.exposure_after = exposure_after
        self.activity = 'MT5 ORDER CHECK'
        try:
            result = adapter.submit(client_id, packet.plan, volume, risk.max_loss)
        except Exception as exc:
            mark("ORDER_SEND", "Broker order lifecycle", False, str(exc))
            result = {'status':'BROKER_EXCEPTION','message':str(exc)}
        else:
            passed = result.get('status') == 'FILLED_VERIFIED'
            mark("ORDER_SEND", "Broker order lifecycle", passed, result.get('message') or result.get('status'))
        self.last_order_client_id = client_id; self.last_execution = result
        self.log_event('order_lifecycle', {'client_id': client_id, 'status': result.get('status'), 'symbol': packet.plan.symbol, 'side': packet.plan.side.value, 'volume': volume, 'result': result, 'diagnostics': diagnostics})
        if result.get('status') == 'FILLED_VERIFIED':
            self.activity = 'ORDER FILLED — POSITION VERIFIED'
            try:
                self.account_sync.sync(self.feed); self.last_account_sync = utc_now(); self.account_live = True
            except Exception as exc:
                self.error(f'Post-trade account synchronization failed: {exc}', traceback.format_exc())
        return {**result, 'client_id':client_id, 'sizing':sizing, 'margin_needed':margin_needed, 'exposure_after':exposure_after, 'diagnostics':diagnostics}

    def audit_live_positions(self):
        if not (self.config.live_trading_enabled and self.feed is not None and not self.demo_mode):
            return
        try:
            rows = PositionStore(DB_PATH).list()
            ours = [p for p in rows if int(p.get('magic') or 0) == self.config.magic]
            for p in ours:
                if float(p.get('sl') or 0) <= 0 or float(p.get('tp') or 0) <= 0:
                    self.error(f'Protective SL/TP missing on managed position {p.get("ticket")}')
                    self.log_event('protection_alert', {'ticket':p.get('ticket'), 'symbol':p.get('symbol'), 'sl':p.get('sl'), 'tp':p.get('tp')})
        except Exception as exc:
            self.error(f'Position audit failed: {exc}', traceback.format_exc())

    def _pipeline_begin(self, cycle_id: int, symbol: str = ""):
        with self._pipeline_lock:
            self.pipeline_state = {
                "cycle_id": int(cycle_id), "symbol": str(symbol or self.config.symbol),
                "status": "RUNNING", "started_at": utc_now().isoformat(),
                "updated_at": utc_now().isoformat(), "finished_at": None,
                "decision": "WAIT", "confidence": 0.0, "risk_status": "NOT_EVALUATED",
                "execution_status": "NOT_ATTEMPTED", "live_order_verified": False,
                "stages": [], "blockers": [],
            }

    def _pipeline_stage(self, name: str, status: str, detail: str, **metadata):
        """Publish an auditable step in the selected-symbol decision-to-execution path."""
        state = str(status or "INFO").upper()
        item = {
            "stage": str(name), "status": state, "detail": str(detail or "")[:1200],
            "time": utc_now().isoformat(),
        }
        if metadata:
            item["metadata"] = metadata
        with self._pipeline_lock:
            current = dict(self.pipeline_state or {})
            stages = list(current.get("stages") or [])
            stages.append(item)
            current["stages"] = stages[-40:]
            current["updated_at"] = item["time"]
            current["symbol"] = str(metadata.get("symbol") or current.get("symbol") or self.config.symbol)
            if state in {"BLOCKED", "FAILED"}:
                blockers = list(current.get("blockers") or [])
                blocker = f"{name}: {detail}"
                if blocker not in blockers:
                    blockers.append(blocker)
                current["blockers"] = blockers[-20:]
            if name == "DEBATE_AND_DECISION":
                current["decision"] = str(metadata.get("decision") or "WAIT")
                current["confidence"] = float(metadata.get("confidence") or 0.0)
            elif name == "RISK_GATE":
                current["risk_status"] = str(metadata.get("risk_status") or state)
            elif name == "EXECUTION_RESULT":
                current["execution_status"] = str(metadata.get("execution_status") or "UNKNOWN")
                current["live_order_verified"] = str(metadata.get("execution_status") or "") == "FILLED_VERIFIED"
            if name in {"CYCLE_COMPLETE", "CYCLE_ERROR"}:
                current["status"] = "COMPLETE" if name == "CYCLE_COMPLETE" else "ERROR"
                current["finished_at"] = item["time"]
            self.pipeline_state = current

    def _pipeline_snapshot(self) -> dict:
        with self._pipeline_lock:
            return {
                **dict(self.pipeline_state or {}),
                "stages": [dict(x) for x in (self.pipeline_state or {}).get("stages", [])],
                "blockers": list((self.pipeline_state or {}).get("blockers", [])),
            }

    def _universe_snapshot(self) -> dict:
        """Merge live worker health into the API snapshot instead of serving stale startup state."""
        state = dict(self.universe_state or {})
        worker = dict(self._universe_worker_status or {})
        state["worker"] = worker
        queue_stats = self.scan_queue.stats()
        state["orchestration"] = queue_stats
        # Keep a consistently shaped diagnostic payload even before the first feed-backed
        # scan. Zero must mean "none completed", not "all scanned and no setups".
        prior_progress = dict(state.get("scan_progress") or {})
        total_available = int(state.get("total_available", prior_progress.get("available_symbols", 0)) or 0)
        completed_symbols = int(state.get("analyzed_count", prior_progress.get("completed_symbols", 0)) or 0)
        worker_counts = queue_stats.get("status_counts") or {}
        prior_progress.update({
            "available_symbols": total_available,
            "completed_symbols": completed_symbols,
            "remaining_symbols": max(0, total_available - completed_symbols),
            "failed_symbols": int(prior_progress.get("failed_symbols", 0) or 0),
            "coverage_pct": round(completed_symbols / total_available * 100.0, 2) if total_available else None,
            "jobs_queued": int(queue_stats.get("queued", worker_counts.get("QUEUED", 0)) or 0),
            "jobs_running": int(queue_stats.get("running", worker_counts.get("RUNNING", 0)) or 0),
            "jobs_succeeded": int(queue_stats.get("succeeded", worker_counts.get("SUCCEEDED", 0)) or 0),
            "jobs_failed": int(queue_stats.get("failed", worker_counts.get("FAILED", 0)) or 0),
            "basis": prior_progress.get("basis") or "completed symbol scans only; active-symbol analysis is reported separately",
        })
        state["scan_progress"] = prior_progress
        worker_state = str(worker.get("state") or "UNKNOWN")
        if state.get("status") in {None, "Waiting for first scan"}:
            if worker_state == "WAITING_FOR_FEED":
                state["status"] = "Waiting for a connected MT5 market feed"
            elif worker_state == "DISABLED":
                state["status"] = "Scanner disabled in settings"
            elif worker_state == "PAUSED":
                state["status"] = "Scanner paused"
            elif worker_state in {"READY", "RUNNING", "WAITING_FOR_JOBS"}:
                state["status"] = "Background scanner waiting for due jobs"
        if worker.get("blocked_reason"):
            state["blocked_reason"] = worker.get("blocked_reason")
        return state

    def refresh(self, force=False):
        with self.lock:
            if self.paused and not force:
                return
            cycle_start = time.perf_counter()
            self.last_cycle_started = utc_now()
            cycle_id = int(self.total_cycles) + 1
            self._pipeline_begin(cycle_id, self.config.symbol)
            self._pipeline_stage("MARKET_DATA", "RUNNING", "Fetching MT5 quote and M5/M15/H1 source data", symbol=self.config.symbol)
            self.activity = "FETCHING MARKET DATA"
            try:
                cycle_trace = ProgressTrace()
                cycle_trace.emit("market_data","RUNNING","Fetching M5/M15/H1 market state")
                snap = self.get_snapshot()
                quote_fresh = snap.metadata.get("quote_fresh")
                self._pipeline_stage(
                    "MARKET_DATA", "PASSED" if quote_fresh is True else "WARNING",
                    f"Snapshot captured; quote freshness={quote_fresh}; status={snap.metadata.get('quote_status') or 'unreported'}",
                    symbol=snap.symbol, quote_fresh=quote_fresh,
                    timestamp_integrity=snap.metadata.get("timestamp_integrity_status"),
                    candle_counts={tf: len(snap.candles.get(tf) or []) for tf in ("M5", "M15", "H1")},
                )
                # Refresh public news asynchronously so RSS/GDELT latency cannot block
                # symbol scanning or the MT5 quote/decision loop.
                if self.news_provider.enabled and time.time() - float(self.news_provider.last_fetch_epoch or 0) >= self.news_refresh_seconds:
                    self.news_provider.refresh_async()
                if self.news_provider.last_items:
                    self.news_items = self.news_provider.last_items
                self.news_last_fetch = float(self.news_provider.last_fetch_epoch or self.news_last_fetch or 0)
                # The global News tab retains all stories for human research. The automated
                # analyst context is narrower: a story must have an explicit link to this exact
                # instrument, a usable publisher URL/source label and a plausible timestamp <=6h.
                # Raw unrelated headlines are not passed into the agents/LLM context.
                news_context = assess_news_for_instrument(self.news_items or [], snap.symbol)
                snap.metadata["news_context"] = news_context
                try:
                    news_analysis = self.news_analyst.analyze_instrument(
                        self.news_items or [], snap.symbol, snapshot=snap, now=utc_now()
                    )
                    self.news_analysis_feed_cache = self.news_analyst.analyze_feed(
                        self.news_items or [], now=utc_now(), queue_llm=True
                    )
                except Exception as news_analysis_exc:
                    # News intelligence is additive and must never crash a market-data cycle.
                    news_analysis = {
                        "engine": "instrument_aware_news_impact_v1", "instrument": snap.symbol,
                        "status": "ANALYSIS_UNAVAILABLE", "matched_event_count": 0,
                        "qualified_event_count": 0, "events": [],
                        "risk_review": {"required": False, "status": "ANALYSIS_UNAVAILABLE", "automatic_risk_change": False},
                        "directional_vote_allowed": False, "order_authorization": False,
                        "error_type": type(news_analysis_exc).__name__,
                    }
                snap.metadata["news_analysis"] = news_analysis
                self.last_snapshot = snap
                snap.metadata["news_headlines"] = list(news_context.get("candidate_headlines") or [])[:15]
                snap.metadata["news_headlines_matched_count"] = int(news_context.get("matched_headline_count") or 0)
                snap.metadata["news_headlines_excluded_count"] = int(news_context.get("rejected_unlinked_headline_count") or 0) + int(news_context.get("rejected_provenance_or_stale_count") or 0)
                statuses = self.news_provider.last_status or {}
                source_ok = any(isinstance(v, dict) and v.get("status") == "ok" for v in statuses.values())
                snap.metadata["news_provider_connected"] = bool(
                    self.news_provider.enabled and source_ok and self.news_items
                    and self.news_provider.last_fetch_epoch
                    and time.time() - float(self.news_provider.last_fetch_epoch) <= 30 * 60
                )
                snap.metadata["news_feed_status"] = statuses
                snap.metadata["news_sentiment"] = analyze_news(snap.metadata["news_headlines"][:30])
                cycle_trace.emit("market_data","READY",f"{snap.symbol} @ {snap.price:.5f}",{"demo":self.demo_mode})
                cycle_trace.emit("account","RUNNING","Synchronizing account and positions")
                self.activity = "SYNCHRONIZING ACCOUNT / POSITIONS"
                if self.feed is not None and not self.demo_mode:
                    try:
                        self.account_sync.sync(self.feed)
                        self.account_live = True
                        self.last_account_sync = utc_now()
                    except Exception as sync_exc:
                        self.account_live = False
                        self.error(f"Account synchronization failed: {sync_exc}", traceback.format_exc())
                else:
                    self.account_live = False
                cycle_trace.emit("account","READY","Account/position state synchronized" if self.account_live else "Account state unavailable; analysis continues")
                self._pipeline_stage("ACCOUNT_STATE", "PASSED" if self.account_live else "INFO",
                    "Account and open positions synchronized" if self.account_live else "Account sync unavailable; analysis can continue but live execution cannot be assumed ready",
                    account_live=bool(self.account_live), mt5_connected=bool(self.mt5_connected))
                self.activity = "RUNNING MULTI-AGENT ANALYSIS"
                acct = AccountState(DB_PATH).load() or {
                    "equity": 10000.0,
                    "balance": 10000.0,
                    "daily_pnl": 0.0,
                    "exposure": 0.0,
                    "open_positions": 0,
                    "margin_used": 0.0,
                    "updated": None,
                }
                cycle_trace.emit("analysts","RUNNING","Running specialist agents + research debate")
                self.activity = "RUNNING AI AGENT MESH"
                mesh = self.agent_mesh.analyze(snap, force_ai=force)
                snap.metadata["regime_weighted_council"] = mesh.regime_council or {}
                snap.metadata["regime_council_size_multiplier"] = mesh.risk_size_multiplier
                self.ai_mesh_state = mesh.to_dict()
                self.ai_mesh_state["adaptive_learning"] = dict(self.agent_mesh.last_learning_status)
                floor_context = mesh.debate_floor if isinstance(mesh.debate_floor, dict) else {}
                self._pipeline_stage("DEBATE_AND_DECISION", "COMPLETE",
                    f"Specialist debate completed; final decision={mesh.decision}; consensus strength={float(mesh.confidence or 0):.3f}",
                    symbol=snap.symbol, decision=str(mesh.decision), confidence=float(mesh.confidence or 0),
                    agent_count=len(mesh.reports or []), floor_agreement=floor_context.get("agreement"),
                    debate_rounds=floor_context.get("rounds"), dissent_count=len(floor_context.get("dissent") or []))
                cycle_trace.emit("decision","READY",f"Final {mesh.decision} confidence={mesh.confidence:.2f}",{"provider":mesh.provider})
                try:
                    self._update_research_state()
                    bar_key = self.agent_mesh._bar_key(snap)
                    if force or bar_key != self.last_run_bar_key:
                        self._record_run_card(snap, mesh, "MT5" if not self.demo_mode else "DEMO")
                        self.last_run_bar_key = bar_key
                except Exception as research_exc:
                    self.error(f"Research state update failed: {research_exc}", traceback.format_exc())
                signals, packet = self.agent_mesh.to_legacy(mesh, snap.symbol)
                for sig in signals:
                    self.agent_status[sig.agent] = {
                        "state": "READY",
                        "finished": utc_now().isoformat(),
                        "side": sig.side.value,
                        "confidence": sig.confidence,
                        "source": mesh.provider,
                    }
                self.activity = "CALCULATING FINAL DECISION / PORTFOLIO MANAGER"
                risk = None
                execution = None
                if packet.plan:
                    self._pipeline_stage("TRADE_PLAN", "PASSED",
                        f"Actionable {packet.decision.value} plan formed with entry, SL and TP",
                        symbol=packet.plan.symbol, side=packet.decision.value, confidence=float(packet.confidence or 0),
                        entry=float(packet.plan.entry), stop=float(packet.plan.stop), target=float(packet.plan.target))
                else:
                    plan_reasons = list(getattr(packet, "reasons", []) or [])
                    floor_reasons = list(getattr(mesh, "reasons", []) or [])
                    reason = "; ".join(str(x) for x in (plan_reasons + floor_reasons)[:4]) or "Decision engine withheld a directional plan."
                    self._pipeline_stage("TRADE_PLAN", "BLOCKED", reason,
                        symbol=snap.symbol, side=str(packet.decision.value), confidence=float(packet.confidence or 0))
                # Always attribute agent signals (including WAIT) so Performance OBS/DIR update every cycle
                decision_client_id = self._client_id(snap, packet)
                try:
                    entry_v = float(packet.plan.entry) if packet.plan else float(getattr(snap, "price", 0) or 0)
                    stop_v = float(packet.plan.stop) if packet.plan else 0.0
                    target_v = float(packet.plan.target) if packet.plan else 0.0
                    sig_rows = []
                    for sig in signals:
                        try:
                            d = asdict(sig)
                            side = d.get("side")
                            if side is not None and not isinstance(side, str):
                                d["side"] = getattr(side, "value", str(side))
                            sig_rows.append(d)
                        except Exception:
                            continue
                    self.performance.record_decision(
                        decision_client_id, snap.symbol, packet.decision.value, packet.confidence,
                        entry_v, stop_v, target_v,
                        sig_rows,
                    )
                except Exception as exc:
                    self.error(f"Performance attribution record failed: {exc}")
                if packet.plan:
                    # Persist point-in-time features under the decision ID so only a linked,
                    # completed trade can label this forecast later.
                    try:
                        self.agent_mesh.record_learning_forecast(decision_client_id, snap, mesh)
                        self.ai_mesh_state["adaptive_learning"] = dict(self.agent_mesh.last_learning_status)
                    except Exception as learning_exc:
                        self.error(f"Adaptive learning forecast record failed: {learning_exc}")
                self.activity = "CHECKING RISK GATE"
                cycle_trace.emit("risk","RUNNING","Evaluating deterministic risk and broker gates")
                if packet.plan and not self.market_open:
                    # Closed/offline/unknown session can still produce historical analysis,
                    # but it must never send or paper-fill a new order.
                    execution = {
                        "status": "MARKET_CLOSED",
                        "reason": str((self.session_info or {}).get("reason") or "Market session is not confirmed open"),
                        "blockers": ["MARKET_SESSION_NOT_OPEN"],
                    }
                    self.ai_mesh_state["risk_gate"] = {
                        "approved": False,
                        "reason": execution["reason"],
                        "blockers": execution["blockers"],
                        "checks": [{"code": "MARKET_SESSION", "label": "Market session confirmed open", "passed": False, "detail": execution["reason"]}],
                    }
                    self.last_execution = execution
                    self._pipeline_stage("RISK_GATE", "BLOCKED", execution["reason"], risk_status="MARKET_CLOSED", blockers=execution["blockers"])
                    self.log_event("trade_blocked", execution)
                elif packet.plan and snap.metadata.get("quote_fresh") is False:
                    # Connection state is not quote freshness: stale MT5 ticks must
                    # not authorize an order or be paper-filled as though new.
                    age = snap.metadata.get("tick_age_seconds")
                    reason = f"Source quote is stale/unusable ({snap.metadata.get('quote_status') or 'freshness check failed'}, age={age}s)"
                    execution = {"status": "STALE_MARKET_DATA", "reason": reason, "blockers": ["QUOTE_NOT_FRESH"]}
                    self.ai_mesh_state["risk_gate"] = {
                        "approved": False, "reason": reason, "blockers": execution["blockers"],
                        "checks": [{"code": "QUOTE_NOT_FRESH", "label": "Source bid/ask quote is fresh", "passed": False, "detail": reason}],
                    }
                    self.last_execution = execution
                    self._pipeline_stage("RISK_GATE", "BLOCKED", reason, risk_status="STALE_MARKET_DATA", blockers=execution["blockers"])
                    self.log_event("trade_blocked", execution)
                elif packet.plan:
                    risk = RiskAgent(self.config.risk).evaluate(
                        snap, packet.plan, acct["equity"], acct["daily_pnl"], acct["exposure"], acct["open_positions"]
                    )
                    self.ai_mesh_state["risk_gate"] = {
                        "approved": risk.approved,
                        "reason": risk.reason,
                        "blockers": risk.blockers,
                        "checks": risk.checks,
                    }
                    self._pipeline_stage("RISK_GATE", "PASSED" if risk.approved else "BLOCKED",
                        str(risk.reason or ("Deterministic risk gate approved the plan" if risk.approved else "Risk gate rejected the plan")),
                        risk_status="APPROVED" if risk.approved else "REJECTED", blockers=list(risk.blockers or []),
                        max_loss=float(getattr(risk, "max_loss", 0) or 0))
                    # Full trade path: paper fills or live MT5 when armed
                    if risk and not risk.approved:
                        execution = {"status": "RISK_BLOCKED", "reason": risk.reason, "blockers": risk.blockers}
                        try:
                            self.log_event("execution_veto", execution)
                            self.error(f"[EXECUTION BLOCKED] RISK_BLOCKED: {risk.reason} blockers={list(risk.blockers or [])[:5]}")
                            self.activity = f"BLOCKED — {risk.reason}"
                        except Exception:
                            pass
                    elif getattr(self, "auto_trade", True) and risk and risk.approved:
                        try:
                            if not hasattr(self, "trade_engine"):
                                self.trade_engine = TradeEngine(self)
                            # Mark-to-market paper positions before new decisions
                            try:
                                self.trade_engine.mark_to_market_paper(snap)
                            except Exception:
                                pass
                            execution = self.trade_engine.execute(snap, packet, risk, acct)
                            self.last_execution = execution
                            if execution.get("status") in ("PAPER_FILLED", "FILLED_VERIFIED", "SUBMITTED", "SUBMITTED_UNCONFIRMED"):
                                try:
                                    ticket = execution.get("position_ticket") or execution.get("ticket")
                                    if ticket:
                                        self.performance.attach_position(self._client_id(snap, packet), ticket)
                                except Exception as attribution_exc:
                                    self.error(f"Position-to-forecast link failed: {attribution_exc}")
                                self.activity = f"ORDER {execution.get('status')} — {execution.get('side')} {execution.get('symbol')} vol={execution.get('volume')}"
                                self.log_event("trade_executed", execution)
                            elif execution.get("status") in ("BLOCKED", "DUPLICATE_BLOCKED"):
                                self.log_event("trade_blocked", execution)
                        except Exception as trade_exc:
                            execution = {"status": "EXEC_ERROR", "reason": str(trade_exc)}
                            self.error(f"Trade execution failed: {trade_exc}", traceback.format_exc())
                    else:
                        execution = self.attempt_live_execution(snap, packet, risk, acct)
                        if risk and risk.approved and self.config.paper_trading:
                            execution = {"status": "PAPER_HOLD", "reason": "auto_trade disabled"}
                else:
                    self.ai_mesh_state["risk_gate"] = {"approved": False, "reason": "No actionable plan", "blockers": ["NO_PLAN"], "checks": []}
                    try:
                        self.log_event("execution_veto", {
                            "status": "NO_PLAN",
                            "reason": "Debate floor / trader returned WAIT — no TradePlan for risk/execution",
                            "decision": getattr(packet, "decision", None) if packet else None,
                        })
                        self.error("[EXECUTION BLOCKED] NO_PLAN: agents did not authorize a BUY/SELL plan this cycle")
                    except Exception:
                        pass
                    execution = {"status": "NO_PLAN", "reason": "Decision engine returned no actionable plan"}
                    self._pipeline_stage("RISK_GATE", "SKIPPED", "Risk sizing skipped because no actionable plan exists", risk_status="SKIPPED_NO_PLAN", blockers=["NO_PLAN"])
                exec_status = str((execution or {}).get("status") or "NOT_ATTEMPTED")
                for broker_check in ((execution or {}).get("diagnostics") or []):
                    if not isinstance(broker_check, dict):
                        continue
                    self._pipeline_stage(
                        "BROKER_CHECK_" + str(broker_check.get("code") or "UNKNOWN"),
                        "PASSED" if broker_check.get("passed") is True else "BLOCKED",
                        str(broker_check.get("detail") or broker_check.get("label") or "Broker execution check"),
                        check_code=str(broker_check.get("code") or "UNKNOWN"),
                        label=str(broker_check.get("label") or "Broker execution check"),
                    )
                exec_failed = exec_status in {"EXEC_ERROR", "BROKER_EXCEPTION", "BROKER_PREFLIGHT_REJECTED", "BROKER_RISK_BLOCKED", "RISK_BLOCKED", "STALE_MARKET_DATA", "SOURCE_CLOCK_INTEGRITY_BLOCKED", "MARKET_CLOSED", "LIVE_DISABLED", "LIVE_GATE_BLOCKED", "LIVE_NOT_ARMED", "KILL_SWITCH_ACTIVE", "NO_LIVE_MT5"}
                exec_stage_status = "PASSED" if exec_status == "FILLED_VERIFIED" else ("PAPER_ONLY" if exec_status == "PAPER_FILLED" else ("BLOCKED" if exec_failed or exec_status == "NO_PLAN" else "INFO"))
                self._pipeline_stage("EXECUTION_RESULT", exec_stage_status,
                    str((execution or {}).get("reason") or (execution or {}).get("message") or exec_status),
                    execution_status=exec_status, symbol=snap.symbol, decision=packet.decision.value,
                    ticket=(execution or {}).get("ticket") or (execution or {}).get("position_ticket"),
                    order_authorized=False if exec_status != "FILLED_VERIFIED" else True,
                    paper_fill=exec_status == "PAPER_FILLED")
                self.audit_live_positions()
                if self.feed is not None and not self.demo_mode and self.account_live:
                    try:
                        self.lifecycle_state = self.lifecycle.reconcile(self.feed, self.config.symbol)
                        if self.lifecycle_state.get("alerts"):
                            self.kill_switch = True
                            self.live_armed = False
                            self.error("Managed-position protection fault; live execution disarmed", str(self.lifecycle_state.get("alerts")))
                    except Exception as lifecycle_exc:
                        self.error(f"Trade lifecycle reconciliation failed: {lifecycle_exc}", traceback.format_exc())
                else:
                    self.lifecycle_state = {"managed_open": [], "alerts": [], "events": [], "lifecycle_events": []}

                # Persist outcomes for closed positions and run adaptive management only when fully live-armed.
                for ev in self.lifecycle_state.get("events", []):
                    if ev.get("event") == "POSITION_CLOSED":
                        ticket = ev.get("ticket")
                        realized = ev.get("realized_pnl")
                        try:
                            old = self.lifecycle.store.get(str(ticket))
                            order = self.management._order_for_position(ticket)
                            if old and order:
                                dist = abs(float(order.get("entry") or old["price_open"]) - float(order.get("stop") or 0))
                                exit_price = float(old.get("last_price") or 0)
                                side = str(order.get("side") or old.get("side") or "BUY")
                                r_mult = 0.0
                                if dist > 0 and exit_price > 0:
                                    r_mult = ((exit_price - float(order.get("entry") or old["price_open"]))/dist if side == "BUY" else (float(order.get("entry") or old["price_open"])-exit_price)/dist)
                                outcome = "WIN" if float(realized or 0) > 0 else "LOSS" if float(realized or 0) < 0 else "FLAT"
                                self.performance.close_by_ticket(ticket, float(realized or 0), r_mult, outcome)
                                self.finalize_trade_learning(ticket, float(realized or 0))
                        except Exception as exc:
                            self.error(f"Performance close attribution failed: {exc}")

                self.activity = "MANAGING OPEN POSITIONS"
                try:
                    if not hasattr(self, "trade_engine"):
                        self.trade_engine = TradeEngine(self)
                    # Always mark paper + agent-driven management
                    self.trade_engine.mark_to_market_paper(snap)
                    # Paper positions may be managed from the paper ledger. This
                    # does not invoke the live manager or send broker mutations.
                    mgmt = self.trade_engine.manage_from_agents(snap, packet, allow_live=False)
                    self.management_state = mgmt
                    for action in mgmt.get("actions", []):
                        self.log_event("position_management", action)
                    # Existing bot-owned MT5 positions need exit supervision even when
                    # new entries are disarmed or the entry kill switch is on. Each
                    # position gets its own symbol-specific candles and fresh-quote gate.
                    if self.management_ready():
                        position_snapshots = {str(snap.symbol).upper(): snap}
                        main_broker = str((snap.metadata or {}).get("broker_symbol") or snap.symbol).upper()
                        position_snapshots[main_broker] = snap
                        position_decisions = {
                            str(snap.symbol).upper(): {"decision": mesh.decision, "confidence": mesh.confidence},
                            main_broker: {"decision": mesh.decision, "confidence": mesh.confidence},
                        }
                        try:
                            live_positions = MT5ExecutionAdapter(self.feed, DB_PATH, self.config.magic, self.config.deviation).positions()
                        except Exception:
                            live_positions = []
                        for pos in live_positions:
                            if int(getattr(pos, "magic", 0) or 0) != self.config.magic:
                                continue
                            pos_sym = str(getattr(pos, "symbol", "") or "").upper()
                            if not pos_sym or pos_sym in position_snapshots:
                                continue
                            try:
                                pair_snapshot = MultiTimeframeFeed(self.feed).snapshot(pos_sym, ("M5", "M15", "H1"), self.config.lookback)
                                position_snapshots[pos_sym] = pair_snapshot
                                # Thesis invalidation is always symbol-specific. Reuse
                                # the pair's cached mesh if available, but never borrow
                                # the active chart's direction for a different position.
                                if self.management.cfg.ai_invalidation_enabled:
                                    if self._position_mesh is None:
                                        self._position_mesh = AgentMesh()
                                    pair_result = self._position_mesh.analyze(pair_snapshot, force_ai=False)
                                    position_decisions[pos_sym] = {"decision": pair_result.decision, "confidence": pair_result.confidence}
                            except Exception as data_exc:
                                self.log_event("position_snapshot_unavailable", {"symbol": pos_sym, "reason": str(data_exc)})
                        live_mgmt = self.management.manage(
                            self.feed, None, snap, snapshots=position_snapshots,
                            decisions=position_decisions,
                            allow_live=True,
                        )
                        self.management_state = {**mgmt, "live": live_mgmt}
                        for action in live_mgmt.get("actions", []):
                            self.log_event("position_management", action)
                        for alert in live_mgmt.get("alerts", []):
                            self.log_event("position_management_alert", alert)
                except Exception as management_exc:
                    self.error(f"Position management failed: {management_exc}", traceback.format_exc())
                    self.management_state = {"actions": [], "alerts": [], "managed": []}

                structure = analyze_structure(
                    snap.candles.get("M15") or snap.candles.get("H1") or snap.candles.get("M5", [])
                )
                structure_dict = structure.as_dict()
                result = {
                    "snapshot": snap,
                    "signals": signals,
                    "packet": packet,
                    "risk": risk,
                    "execution": execution,
                    "account": acct,
                    "structure": structure_dict,
                }
                self.last = result
                try:
                    self._refresh_universe_analysis(snap, mesh)
                except Exception as universe_exc:
                    self.error(f"All-symbol analysis update failed: {universe_exc}", traceback.format_exc())
                source_tick_epoch = float(snap.metadata.get("tick_time_msc_normalized") or 0) / 1000.0
                if source_tick_epoch <= 0:
                    source_tick_epoch = float(snap.metadata.get("tick_time_normalized") or 0)
                if source_tick_epoch <= 0:
                    # Backward compatibility for older/non-MT5 snapshots; in a live
                    # snapshot prefer an explicitly normalized source timestamp.
                    source_tick_epoch = float(snap.metadata.get("tick_time_msc") or 0) / 1000.0
                if source_tick_epoch <= 0:
                    source_tick_epoch = float(snap.metadata.get("tick_time") or 0)
                source_tick_time = (datetime.fromtimestamp(source_tick_epoch, timezone.utc).isoformat()
                                    if source_tick_epoch > 0 else None)
                hist_point = {
                    # Prefer source event time; polling time would manufacture a
                    # fresh-looking history point from the same cached quote.
                    "time": source_tick_time or (snap.timestamp.isoformat() if hasattr(snap.timestamp, "isoformat") else str(snap.timestamp)),
                    "price": snap.price,
                    "decision": packet.decision.value,
                    "confidence": packet.confidence,
                    "risk": risk.approved if risk else False,
                    "spread": snap.spread,
                    "market_open": self.market_open,
                }
                # When market is closed / frozen, only keep one point per unique bar time
                if self.history:
                    same_source_event = self.history[-1].get("time") == hist_point["time"]
                    if same_source_event:
                        self.history[-1] = hist_point
                    elif snap.metadata.get("quote_fresh") is False:
                        # A stale quote is observable as status, not a new price point.
                        pass
                    else:
                        self.history.append(hist_point)
                elif snap.metadata.get("quote_fresh") is not False:
                    self.history.append(hist_point)
                self.history = self.history[-180:]
                # OHLC candle series for advanced chart (MT5-style)
                def _ser_candles(arr, limit=120):
                    out = []
                    for c in (arr or [])[-limit:]:
                        out.append({
                            "t": c.timestamp.isoformat() if hasattr(c.timestamp, "isoformat") else str(c.timestamp),
                            "o": float(c.open),
                            "h": float(c.high),
                            "l": float(c.low),
                            "c": float(c.close),
                            "v": float(c.volume or 0),
                        })
                    return out
                self.candle_series = {
                    "M5": _ser_candles(snap.candles.get("M5"), 150),
                    "M15": _ser_candles(snap.candles.get("M15"), 120),
                    "H1": _ser_candles(snap.candles.get("H1"), 100),
                }
                cycle_trace.emit("cycle","READY","Cycle completed")
                self.progress_state = cycle_trace.events
                self._pipeline_stage("CYCLE_COMPLETE", "COMPLETE", "Analysis, decision, risk and execution-path audit finished",
                    symbol=snap.symbol, decision=packet.decision.value, execution_status=str((execution or {}).get("status") or "NOT_ATTEMPTED"))
                self.last_cycle_finished = utc_now()
                self.last_cycle_ms = round((time.perf_counter() - cycle_start) * 1000, 2)
                self.total_cycles += 1
                try:
                    self._maybe_persist_settings()
                except Exception:
                    pass
                self.consecutive_errors = 0
                self.next_retry_at = None
                self.activity = ("MARKET CLOSED — MONITORING" if not self.market_open else "MONITORING — WAITING FOR NEXT CYCLE")
                self.log_event(
                    "analysis_cycle",
                    {
                        "symbol": snap.symbol,
                        "decision": packet.decision.value,
                        "confidence": packet.confidence,
                        "risk": asdict(risk) if risk else None,
                        "execution": execution,
                        "demo": self.demo_mode,
                        "latency_ms": self.last_cycle_ms,
                    },
                )
            except Exception as exc:
                self.last_cycle_finished = utc_now()
                self.last_cycle_ms = round((time.perf_counter() - cycle_start) * 1000, 2)
                self.consecutive_errors += 1
                self.last_error_time = utc_now()
                backoff = min(self.max_backoff, max(2, self.interval) * (2 ** min(self.consecutive_errors - 1, 5)))
                self.next_retry_at = utc_now().timestamp() + backoff
                self.activity = "ERROR — AUTO-RECOVERY WAIT"
                message = f"Analysis cycle failed: {type(exc).__name__}: {exc}"
                self._pipeline_stage("CYCLE_ERROR", "FAILED", message, symbol=self.config.symbol, error_type=type(exc).__name__)
                self.error(message, traceback.format_exc())
                self.notifier.send("AI Trading Team error", message)

    def _sample_portfolio_account_snapshot(self):
        """Persist a low-frequency live account snapshot independently of UI visits.

        The analysis loop calls this at a bounded interval; MT5 bridge methods share
        the bridge IPC lock, and the SQLite store has account-aware throttling.
        """
        now = time.time()
        try:
            interval = max(15.0, min(900.0, float(os.getenv("PORTFOLIO_SNAPSHOT_INTERVAL_SECONDS", "60"))))
        except (TypeError, ValueError):
            interval = 60.0
        if now - float(getattr(self, "_portfolio_snapshot_last_attempt", 0.0) or 0.0) < interval:
            return False
        self._portfolio_snapshot_last_attempt = now
        try:
            if not self._connect_mt5():
                return False
            account = self.feed.get_bridge().account_info()
            from trading_team.storage.portfolio_history import PortfolioSnapshotStore
            store = PortfolioSnapshotStore(DB_PATH)
            return store.record(account, min_interval_seconds=interval, now=now)
        except Exception as exc:
            # Avoid logging the same offline/database error every analysis cycle.
            if now - float(getattr(self, "_portfolio_snapshot_last_error_log", 0.0) or 0.0) >= 300.0:
                self._portfolio_snapshot_last_error_log = now
                try:
                    self.error(f"Portfolio account sampling unavailable: {type(exc).__name__}: {exc}")
                except Exception:
                    pass
            return False

    def _ensure_universe_worker(self):
        """Maintain the configured number of bounded broker-universe workers."""
        with self._thread_start_lock:
            alive = [t for t in self._universe_worker_threads if t.is_alive()]
            self._universe_worker_threads = alive
            active_indexes = {int(getattr(t, "worker_index", 0) or 0) for t in alive}
            started = False
            for worker_index in range(1, self.universe_worker_count + 1):
                if worker_index in active_indexes:
                    continue
                thread = threading.Thread(
                    target=self._universe_worker_loop, args=(worker_index,), daemon=True,
                    name=f"broker-universe-scanner-{worker_index}"
                )
                thread.worker_index = worker_index
                self._universe_worker_threads.append(thread)
                thread.start()
                started = True
            self._universe_worker_thread = next((t for t in self._universe_worker_threads if t.is_alive()), None)
            return started

    def start(self):
        def loop():
            while self.running:
                try:
                    # Self-heal any universe worker that exited unexpectedly. The pool stays
                    # bounded and defaults to two workers to preserve CPU headroom.
                    self._ensure_universe_worker()
                    # Portfolio equity history must not depend on the user opening the Portfolio page.
                    self._sample_portfolio_account_snapshot()
                    if not self.paused:
                        now = time.time()
                        if self.next_retry_at is None or now >= self.next_retry_at:
                            # Honour monitor interval between full analysis cycles
                            if (now - self._last_refresh_ts) >= max(2, self.interval) or self._last_refresh_ts == 0:
                                self.refresh()
                                self._last_refresh_ts = time.time()
                                try:
                                    self.refresh_watchlist_quotes()
                                except Exception as wq:
                                    self.error(f"Watchlist quote refresh failed: {wq}")
                        else:
                            remaining = max(0, int(self.next_retry_at - now))
                            self.activity = f"AUTO-RECOVERY — RETRY IN {remaining}s"
                    time.sleep(0.5)
                except Exception as exc:
                    self.error(f"Monitor loop failure: {exc}", traceback.format_exc())
                    time.sleep(min(self.max_backoff, 5))

        with self._thread_start_lock:
            self.running = True
            # Keep one dormant worker ready even if all-symbol analysis is currently off.
            self._ensure_universe_worker()
            if self._analysis_thread is None or not self._analysis_thread.is_alive():
                self._analysis_thread = threading.Thread(target=loop, daemon=True, name="analysis-loop")
                self._analysis_thread.start()
        self.activity = "ENGINE LOOP STARTED"
        self.log_event("engine_started", {"interval": self.interval, "symbol": self.config.symbol})



    def _continuous_state_safe(self) -> dict:
        try:
            from trading_team.ai_mesh.continuous_flow import continuous_status
            return continuous_status()
        except Exception as exc:
            return {"enabled": False, "error": str(exc)}

    def _settings_public(self) -> dict:
        """Non-secret runtime settings for UI and persistence echo."""
        risk = self.config.risk
        return {
            "symbol": self.config.symbol,
            "symbols": [x for x in self.watchlist if str(x).upper() != "GBXUSD"],
            "interval": self.interval,
            "lookback": self.config.lookback,
            "timeframe": self.config.timeframe,
            "mode": "PAPER" if self.config.paper_trading else "LIVE",
            "live_execution_enabled": self.config.live_trading_enabled,
            "live_armed": self.live_armed,
            "kill_switch": bool(self.kill_switch),
            "analyze_all_symbols_enabled": self.analyze_all_symbols_enabled,
            "all_symbol_scan_limit": self.all_symbol_scan_limit,
            "all_symbol_scan_batch_size": self.all_symbol_scan_batch_size,
            "all_symbol_rescan_seconds": self.all_symbol_rescan_seconds,
            "market_desks_enabled": list(self.market_desks_enabled),
            "exit_management": asdict(self.management.cfg),
            "position_manager_enabled": self.management.cfg.enabled,
            "debate_consensus_threshold": float(getattr(self, "debate_consensus_threshold", 0.55)),
            "strong_research_override": float(getattr(self, "strong_research_override", 0.72)),
            "ai_analysis_interval_seconds": int(getattr(self.agent_mesh, "ai_min_interval", getattr(self, "ai_min_interval", 60))),
            "auto_trade": bool(getattr(self, "auto_trade", True)),
            "trade_universe_enabled": bool(getattr(self, "trade_universe_enabled", True)),
            "trade_universe_max_per_cycle": int(getattr(self, "trade_universe_max_per_cycle", 15)),
            "local_ai_enabled": bool(getattr(self, "local_ai_enabled", False)),
            "local_ai_tiered": bool(getattr(self, "local_ai_tiered", True)),
            "local_ai_can_influence_decision": bool(getattr(self, "local_ai_can_influence", False)),
            "local_ai_base_url": str(getattr(self, "local_ai_base_url", "http://127.0.0.1:11434/v1")),
            "local_ai_gate_model": str(getattr(self, "local_ai_gate_model", "qwen2.5:1.5b")),
            "local_ai_deep_model": str(getattr(self, "local_ai_deep_model", "qwen2.5:3b")),
            "bt_symbol": str(getattr(self, "bt_symbol", "XAUUSD")),
            "bt_strategy": str(getattr(self, "bt_strategy", "trend_following")),
            "bt_bars": int(getattr(self, "bt_bars", 3000)),
            "bt_equity": float(getattr(self, "bt_equity", 10000)),
            "bt_risk_pct": float(getattr(self, "bt_risk_pct", 0.5)),
            "bt_execution_model": str(getattr(self, "bt_execution_model", "ohlc")),
            "bt_objective": str(getattr(self, "bt_objective", "composite")),
            "bt_max_trials": int(getattr(self, "bt_max_trials", 40)),
            "bt_folds": int(getattr(self, "bt_folds", 4)),
            "bt_is_ratio": float(getattr(self, "bt_is_ratio", 0.7)),
            "bt_commission_bps": float(getattr(self, "bt_commission_bps", 0.0)),
            "trade_cooldown_seconds": int(self.config.trade_cooldown_seconds),
            "deviation": int(self.config.deviation),
            "lifecycle_reconcile_seconds": int(self.config.lifecycle_reconcile_seconds),
            "one_position_per_symbol": bool(self.config.one_position_per_symbol),
            "risk_fraction": float(self.config.risk_fraction),
            "intelligence_mode": "classical_only",
            "risk": {
                "max_daily_loss_pct": float(risk.max_daily_loss_pct),
                "max_position_risk_pct": float(risk.max_position_risk_pct),
                "max_account_exposure_pct": float(risk.max_account_exposure_pct),
                "min_reward_risk": float(risk.min_reward_risk),
                "min_decision_confidence": float(risk.min_decision_confidence),
                "max_spread": float(risk.max_spread),
                "max_volatility": float(risk.max_volatility),
                "max_positions": int(risk.max_positions),
            },
            **asdict(self.management.cfg),
        }


    def _maybe_persist_settings(self, force: bool = False):
        """Debounced settings mirror so restarts do not look like a fresh install."""
        import time as _t
        now = _t.time()
        last = float(getattr(self, "_last_settings_persist_ts", 0) or 0)
        if not force and (now - last) < 30.0:
            return
        try:
            self._persist_runtime_settings()
            self._last_settings_persist_ts = now
        except Exception as exc:
            try:
                self.error(f"settings persist failed: {exc}")
            except Exception:
                pass

    def update_settings(self, payload: dict):
        payload = dict(payload or {})
        def _as_int(value, default, lo=None, hi=None):
            if value is None or value is False or value == "":
                v = int(default)
            else:
                try:
                    v = int(float(value))
                except (TypeError, ValueError):
                    v = int(default)
            if lo is not None:
                v = max(lo, v)
            if hi is not None:
                v = min(hi, v)
            return v
        def _as_float(value, default, lo=None, hi=None):
            if value is None or value is False or value == "":
                v = float(default)
            else:
                try:
                    v = float(value)
                except (TypeError, ValueError):
                    v = float(default)
            if lo is not None:
                v = max(lo, v)
            if hi is not None:
                v = min(hi, v)
            return v
        # Settings apply uses its own short critical section. The long-running analysis
        # cycle must not reject configuration saves with a misleading "runtime busy" error.
        self._settings_lock.acquire()
        acquired = True
        try:
            symbol = str(payload.get("symbol", self.config.symbol) or self.config.symbol).strip().upper()
            if symbol == "GBXUSD":
                raise ValueError("This instrument has been removed from this installation.")
            symbols_payload = payload.get("symbols")
            if symbols_payload is not None:
                if isinstance(symbols_payload, str):
                    wl = [x for x in parse_watchlist(symbols_payload) if x.upper() != "GBXUSD"]
                elif isinstance(symbols_payload, (list, tuple)):
                    wl = [str(s).strip().upper() for s in symbols_payload if str(s).strip() and str(s).strip().upper() != "GBXUSD"]
                else:
                    wl = list(self.watchlist)
                if wl:
                    self.watchlist = wl
            if symbol and symbol not in self.watchlist:
                self.watchlist = [symbol] + [s for s in self.watchlist if s != symbol]
            new_lookback = _as_int(payload.get("lookback", self.config.lookback), self.config.lookback, 100, 2000)
            if symbol:
                self.config = AppConfig(
                    paper_trading=self.config.paper_trading, symbol=symbol,
                    symbols=tuple(self.watchlist), timeframe=self.config.timeframe,
                    lookback=new_lookback, live_trading_enabled=self.config.live_trading_enabled,
                    magic=self.config.magic, deviation=self.config.deviation,
                    trade_cooldown_seconds=self.config.trade_cooldown_seconds,
                    one_position_per_symbol=self.config.one_position_per_symbol, risk=self.config.risk,
                )
                self.lifecycle = TradeLifecycleManager(DB_PATH, self.config.magic, self.config.deviation, auto_stop_repair=False)
                self._last_refresh_ts = 0.0
            new_interval = payload.get("interval")
            if new_interval is not None:
                self.interval = _as_int(new_interval, self.interval, 1, 60)

            def flag(name, current):
                if name not in payload:
                    return bool(current)
                value = payload.get(name)
                if isinstance(value, str):
                    return value.strip().lower() in {"1", "true", "yes", "on"}
                return bool(value)

            if "analyze_all_symbols_enabled" in payload:
                self.analyze_all_symbols_enabled = flag("analyze_all_symbols_enabled", self.analyze_all_symbols_enabled)
            if "all_symbol_scan_limit" in payload:
                self.all_symbol_scan_limit = _as_int(payload.get("all_symbol_scan_limit", self.all_symbol_scan_limit), self.all_symbol_scan_limit, 0, 1_000_000)
            if "all_symbol_scan_batch_size" in payload:
                self.all_symbol_scan_batch_size = _as_int(payload.get("all_symbol_scan_batch_size", self.all_symbol_scan_batch_size), self.all_symbol_scan_batch_size, 1, 100)
            if "all_symbol_rescan_seconds" in payload:
                self.all_symbol_rescan_seconds = _as_int(payload.get("all_symbol_rescan_seconds", self.all_symbol_rescan_seconds), self.all_symbol_rescan_seconds, 10, 3600)
            if "market_desks_enabled" in payload:
                raw_desks = payload.get("market_desks_enabled")
                if isinstance(raw_desks, str):
                    raw_desks = [x.strip().lower() for x in raw_desks.split(",") if x.strip()]
                if not isinstance(raw_desks, (list, tuple, set)):
                    raw_desks = self.market_desks_enabled
                selected_desks = sorted({str(x).lower() for x in raw_desks if str(x).lower() in DESK_IDS})
                if not selected_desks:
                    # Keep previous selection instead of failing Apply (UI may omit desks)
                    selected_desks = list(self.market_desks_enabled) or list(DESK_IDS)
                self.market_desks_enabled = selected_desks
                self.market_directors.configure_enabled(selected_desks)
                self.scan_queue.state_set("market_director_stats", self.market_directors.stats_snapshot())
            self.universe_state["enabled"] = self.analyze_all_symbols_enabled
            self.universe_state["scan_limit"] = self.all_symbol_scan_limit
            if self.analyze_all_symbols_enabled:
                self._universe_cursor = 0


            # Classical consensus / risk / execution knobs (non-secret)
            if "debate_consensus_threshold" in payload:
                self.debate_consensus_threshold = _as_float(payload.get("debate_consensus_threshold", 0.55), 0.55, 0.40, 0.85)
                os.environ["DEBATE_CONSENSUS_THRESHOLD"] = str(self.debate_consensus_threshold)
            if "strong_research_override" in payload:
                self.strong_research_override = _as_float(payload.get("strong_research_override", 0.72), 0.72, 0.60, 0.95)
                os.environ["STRONG_RESEARCH_OVERRIDE"] = str(self.strong_research_override)
            if "ai_analysis_interval_seconds" in payload:
                self.ai_min_interval = _as_int(payload.get("ai_analysis_interval_seconds", 30), 30, 10, 3600)
                try:
                    self.agent_mesh.ai_min_interval = self.ai_min_interval
                except Exception:
                    pass
            if "trade_universe_enabled" in payload:
                self.trade_universe_enabled = flag("trade_universe_enabled", self.trade_universe_enabled)
            if "trade_universe_max_per_cycle" in payload:
                self.trade_universe_max_per_cycle = _as_int(
                    payload.get("trade_universe_max_per_cycle", self.trade_universe_max_per_cycle),
                    self.trade_universe_max_per_cycle, 1, 200,
                )
            if "auto_trade" in payload:
                self.auto_trade = flag("auto_trade", self.auto_trade)
            if "bt_symbol" in payload and payload.get("bt_symbol"):
                self.bt_symbol = str(payload.get("bt_symbol")).strip().upper()
            if "bt_strategy" in payload and payload.get("bt_strategy"):
                self.bt_strategy = str(payload.get("bt_strategy")).strip()
            if "bt_bars" in payload:
                self.bt_bars = _as_int(payload.get("bt_bars"), getattr(self, "bt_bars", 3000), 500, 50000)
            if "bt_equity" in payload:
                self.bt_equity = _as_float(payload.get("bt_equity"), getattr(self, "bt_equity", 10000), 100, 10_000_000)
            if "bt_risk_pct" in payload:
                self.bt_risk_pct = _as_float(payload.get("bt_risk_pct"), getattr(self, "bt_risk_pct", 0.5), 0.05, 5.0)
            if "bt_execution_model" in payload and payload.get("bt_execution_model"):
                self.bt_execution_model = str(payload.get("bt_execution_model")).strip().lower()
            if "bt_objective" in payload and payload.get("bt_objective"):
                self.bt_objective = str(payload.get("bt_objective")).strip()
            if "bt_max_trials" in payload:
                self.bt_max_trials = _as_int(payload.get("bt_max_trials"), getattr(self, "bt_max_trials", 40), 5, 200)
            if "bt_folds" in payload:
                self.bt_folds = _as_int(payload.get("bt_folds"), getattr(self, "bt_folds", 4), 2, 12)
            if "bt_is_ratio" in payload:
                self.bt_is_ratio = _as_float(payload.get("bt_is_ratio"), getattr(self, "bt_is_ratio", 0.7), 0.5, 0.9)
            if "bt_commission_bps" in payload:
                self.bt_commission_bps = _as_float(payload.get("bt_commission_bps"), getattr(self, "bt_commission_bps", 0.0), 0.0, 100.0)
            # Local LLM (Ollama)
            if "local_ai_enabled" in payload:
                self.local_ai_enabled = flag("local_ai_enabled", getattr(self, "local_ai_enabled", False))
            if "local_ai_tiered" in payload:
                self.local_ai_tiered = flag("local_ai_tiered", getattr(self, "local_ai_tiered", True))
            if "local_ai_can_influence_decision" in payload:
                self.local_ai_can_influence = flag(
                    "local_ai_can_influence_decision", getattr(self, "local_ai_can_influence", False)
                )
            if "local_ai_base_url" in payload and payload.get("local_ai_base_url"):
                self.local_ai_base_url = str(payload.get("local_ai_base_url")).strip()
            if "local_ai_gate_model" in payload and payload.get("local_ai_gate_model"):
                self.local_ai_gate_model = str(payload.get("local_ai_gate_model")).strip()
            if "local_ai_deep_model" in payload and payload.get("local_ai_deep_model"):
                self.local_ai_deep_model = str(payload.get("local_ai_deep_model")).strip()
            # Push AI env + rebuild provider so settings apply without restart
            if any(k in payload for k in (
                "local_ai_enabled", "local_ai_tiered", "local_ai_can_influence_decision",
                "local_ai_base_url", "local_ai_gate_model", "local_ai_deep_model",
            )):
                os.environ["LOCAL_AI_ENABLED"] = "true" if getattr(self, "local_ai_enabled", False) else "false"
                os.environ["LOCAL_AI_TIERED"] = "true" if getattr(self, "local_ai_tiered", True) else "false"
                os.environ["LOCAL_AI_CAN_INFLUENCE_DECISION"] = (
                    "true" if getattr(self, "local_ai_can_influence", False) else "false"
                )
                os.environ["LOCAL_AI_BASE_URL"] = str(getattr(self, "local_ai_base_url", "http://127.0.0.1:11434/v1"))
                os.environ["LOCAL_AI_GATE_MODEL"] = str(getattr(self, "local_ai_gate_model", "qwen2.5:1.5b"))
                os.environ["LOCAL_AI_DEEP_MODEL"] = str(getattr(self, "local_ai_deep_model", "qwen2.5:3b"))
                os.environ["LOCAL_AI_MODEL"] = str(getattr(self, "local_ai_deep_model", "qwen2.5:3b"))
                os.environ["HYBRID_LLM_ENRICH"] = "true" if getattr(self, "local_ai_enabled", False) else "false"
                try:
                    _desk_limit = max(1, min(4, int(os.getenv("HYBRID_LLM_MAX_DESKS", "1") or 1)))
                except (TypeError, ValueError):
                    _desk_limit = 1
                os.environ["HYBRID_LLM_MAX_DESKS"] = str(_desk_limit)
                os.environ["HYBRID_LLM_INCLUDE_NON_VOTING"] = "false"
                os.environ["CONTINUOUS_GUARDIAN_LLM"] = "false"
                os.environ["CONTINUOUS_CHAIR_LLM"] = "false"
                os.environ["LOCAL_AI_SYNC_RESEARCH"] = "false"
                try:
                    from trading_team.ai_mesh.provider import create_provider
                    new_p = create_provider()
                    self.agent_mesh.provider = new_p
                    try:
                        self.copilot = ResearchCopilot(new_p)
                    except Exception:
                        pass
                    self.log_event("provider_reloaded", new_p.status())
                except Exception as exc:
                    self.error(f"Provider reload failed: {exc}")

            risk_payload = payload.get("risk") if isinstance(payload.get("risk"), dict) else payload
            risk = self.config.risk
            def _rf(name, lo, hi, default):
                if name not in risk_payload and name not in payload:
                    return default
                raw = risk_payload.get(name, payload.get(name, default))
                try:
                    return max(lo, min(hi, float(raw)))
                except (TypeError, ValueError):
                    return default
            def _ri(name, lo, hi, default):
                return int(_rf(name, lo, hi, default))

            new_risk = RiskConfig(
                max_daily_loss_pct=_rf("max_daily_loss_pct", 0.1, 25.0, risk.max_daily_loss_pct),
                max_position_risk_pct=_rf("max_position_risk_pct", 0.05, 5.0, risk.max_position_risk_pct),
                max_account_exposure_pct=_rf("max_account_exposure_pct", 1.0, 50.0, risk.max_account_exposure_pct),
                min_reward_risk=_rf("min_reward_risk", 1.0, 5.0, risk.min_reward_risk),
                min_decision_confidence=_rf("min_decision_confidence", 0.40, 0.95, risk.min_decision_confidence),
                max_spread=_rf("max_spread", 0.1, 500.0, risk.max_spread),
                max_volatility=_rf("max_volatility", 0.0, 1.0, risk.max_volatility),
                max_positions=_ri("max_positions", 1, 200, risk.max_positions),
            )
            new_tf = str(payload.get("timeframe", self.config.timeframe) or self.config.timeframe).upper()
            if new_tf not in {"M1", "M5", "M15", "M30", "H1", "H4", "D1"}:
                new_tf = self.config.timeframe
            new_cooldown = _as_int(payload.get("trade_cooldown_seconds", self.config.trade_cooldown_seconds), self.config.trade_cooldown_seconds, 0, 3600)
            new_dev = _as_int(payload.get("deviation", self.config.deviation), self.config.deviation, 1, 500)
            new_life = _as_int(payload.get("lifecycle_reconcile_seconds", self.config.lifecycle_reconcile_seconds), self.config.lifecycle_reconcile_seconds, 2, 60)
            new_one = flag("one_position_per_symbol", self.config.one_position_per_symbol) if "one_position_per_symbol" in payload else self.config.one_position_per_symbol
            new_rfraction = _as_float(payload.get("risk_fraction", self.config.risk_fraction), self.config.risk_fraction, 0.001, 0.05)
            # Mode controls (user-editable from Settings)
            mode_raw = str(payload.get("mode") or payload.get("trading_mode") or "").strip().upper()
            if mode_raw in {"PAPER", "LIVE"}:
                want_paper = mode_raw == "PAPER"
            elif "paper_trading" in payload:
                want_paper = flag("paper_trading", self.config.paper_trading)
            else:
                want_paper = self.config.paper_trading
            if "live_trading_enabled" in payload:
                want_live_en = flag("live_trading_enabled", self.config.live_trading_enabled)
            else:
                want_live_en = self.config.live_trading_enabled
            # LIVE mode implies live_trading_enabled True; PAPER keeps current live flag unless explicitly set
            if mode_raw == "LIVE":
                want_paper = False
                want_live_en = True
            elif mode_raw == "PAPER":
                want_paper = True
            if "live_armed" in payload:
                self.live_armed = flag("live_armed", self.live_armed)
            if "kill_switch" in payload:
                self.kill_switch = flag("kill_switch", self.kill_switch)
            # Safety coupling: kill ON always disarms; arm requires live enabled and not paper
            if self.kill_switch:
                self.live_armed = False
            if want_paper:
                self.live_armed = False
            if self.live_armed and (want_paper or not want_live_en):
                self.live_armed = False
            self.config = AppConfig(
                paper_trading=bool(want_paper),
                symbol=self.config.symbol,
                symbols=tuple(self.watchlist),
                timeframe=new_tf,
                lookback=self.config.lookback,
                live_trading_enabled=bool(want_live_en),
                magic=self.config.magic,
                deviation=new_dev,
                trade_cooldown_seconds=new_cooldown,
                one_position_per_symbol=bool(new_one),
                risk_fraction=new_rfraction,
                lifecycle_reconcile_seconds=new_life,
                risk=new_risk,
            )
            try:
                self.log_event("mode_settings_updated", {
                    "mode": "PAPER" if self.config.paper_trading else "LIVE",
                    "live_trading_enabled": self.config.live_trading_enabled,
                    "live_armed": self.live_armed,
                    "kill_switch": self.kill_switch,
                })
            except Exception:
                pass

            old = self.management.cfg
            cfg_values = {k: getattr(old, k) for k in ManagementConfig.__dataclass_fields__}
            bool_fields = {
                "enabled": "position_manager_enabled",
                "break_even_enabled": "break_even_enabled",
                "trailing_enabled": "trailing_enabled",
                "trailing_use_structure": "trailing_use_structure",
                "partial_exit_enabled": "partial_exit_enabled",
                "second_partial_exit_enabled": "second_partial_exit_enabled",
                "peak_giveback_enabled": "peak_giveback_enabled",
                "time_stop_enabled": "time_stop_enabled",
                "close_if_unprotected": "close_if_unprotected",
                "ai_invalidation_enabled": "ai_invalidation_enabled",
            }
            for config_name, payload_name in bool_fields.items():
                cfg_values[config_name] = flag(payload_name, cfg_values[config_name])
            numeric_fields = {
                "break_even_r": ("break_even_r", 0.2, 10.0, float),
                "break_even_offset_r": ("break_even_offset_r", 0.0, 0.5, float),
                "trailing_start_r": ("trailing_start_r", 0.2, 20.0, float),
                "trailing_atr_mult": ("trailing_atr_mult", 0.3, 6.0, float),
                "partial_exit_r": ("partial_exit_r", 0.5, 20.0, float),
                "partial_exit_percent": ("partial_exit_percent", 5.0, 90.0, float),
                "second_partial_exit_r": ("second_partial_exit_r", 0.5, 30.0, float),
                "second_partial_exit_percent": ("second_partial_exit_percent", 5.0, 90.0, float),
                "peak_giveback_trigger_r": ("peak_giveback_trigger_r", 0.5, 30.0, float),
                "peak_giveback_r": ("peak_giveback_r", 0.1, 15.0, float),
                "max_hold_minutes": ("max_hold_minutes", 5, 10080, int),
                "time_stop_min_progress_r": ("time_stop_min_progress_r", 0.0, 10.0, float),
                "ai_invalidation_confidence": ("ai_invalidation_confidence", 0.0, 1.0, float),
                "ai_invalidation_cycles": ("ai_invalidation_cycles", 1, 10, int),
            }
            for name, (payload_name, low, high, cast) in numeric_fields.items():
                if payload_name in payload:
                    try:
                        cfg_values[name] = max(low, min(high, cast(payload[payload_name])))
                    except (TypeError, ValueError, OverflowError):
                        pass
            # Stage 2 must be beyond stage 1; otherwise it would be immediately
            # eligible on the next poll with an indistinguishable trigger.
            cfg_values["second_partial_exit_r"] = max(float(cfg_values["partial_exit_r"]), float(cfg_values["second_partial_exit_r"]))
            try:
                self.management.cfg = ManagementConfig(**cfg_values)
            except (TypeError, ValueError) as exc:
                # Do not fail the whole Settings Apply for one bad exit-manager field
                try:
                    self.error(f"Position-management settings partial reject: {exc}")
                except Exception:
                    pass

            self.log_event("settings_changed", {
                "symbol": self.config.symbol, "symbols": list(self.watchlist),
                "interval": self.interval, "lookback": self.config.lookback,
                "all_symbol_analysis_enabled": self.analyze_all_symbols_enabled,
                "all_symbol_scan_limit": self.all_symbol_scan_limit,
                "all_symbol_scan_batch_size": self.all_symbol_scan_batch_size,
                "all_symbol_rescan_seconds": self.all_symbol_rescan_seconds,
                "market_desks_enabled": list(self.market_desks_enabled),
                "position_manager_enabled": self.management.cfg.enabled,
            })
            if not self._persist_runtime_settings():
                raise OSError("Settings were applied in memory but persistence failed; inspect the system error log.")
        finally:
            if acquired:
                self._settings_lock.release()

    def guardian_status(self):
        path = Path(os.getenv("GUARDIAN_STATE_FILE", str(Path(".runtime") / "guardian.json")))
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {"status": "UNKNOWN", "message": "Guardian state unavailable"}

    def serialize(self):
        """Return dashboard state without stalling the UI if analysis holds the lock."""
        try:
            acquired = False
            try:
                acquired = self.lock.acquire(timeout=0.35)
            except Exception:
                acquired = False
            try:
                if acquired:
                    payload = self._serialize_inner()
                    try:
                        self._last_serialize_snapshot = payload
                    except Exception:
                        pass
                    return payload
                # Analysis cycle in progress — serve last good snapshot so the UI does not "reset"
                snap = getattr(self, "_last_serialize_snapshot", None)
                if isinstance(snap, dict) and snap:
                    out = dict(snap)
                    sys = dict(out.get("system") or {})
                    sys["activity"] = self.activity or sys.get("activity") or "CYCLE IN PROGRESS"
                    sys["heartbeat"] = utc_now().isoformat()
                    sys["settings_locked"] = True
                    out["system"] = sys
                    try:
                        out["settings"] = self._settings_public()
                    except Exception:
                        pass
                    return out
                return self._serialize_inner()
            finally:
                if acquired:
                    try:
                        self.lock.release()
                    except Exception:
                        pass
        except Exception as exc:
            try:
                settings = self._settings_public()
            except Exception:
                settings = {
                    "symbol": getattr(getattr(self, "config", None), "symbol", "XAUUSD"),
                    "symbols": list(getattr(self, "watchlist", []) or []),
                    "interval": getattr(self, "interval", 5),
                    "lookback": getattr(getattr(self, "config", None), "lookback", 300),
                    "mode": "PAPER",
                }
            return {
                "version": VERSION,
                "system": {
                    "status": "error",
                    "activity": f"Serialize error: {exc}",
                    "continuous": True,
                    "paused": bool(getattr(self, "paused", False)),
                    "demo_mode": bool(getattr(self, "demo_mode", False)),
                    "mt5_connected": bool(getattr(self, "mt5_connected", False)),
                    "connection_detail": str(exc),
                    "total_cycles": getattr(self, "total_cycles", 0),
                    "consecutive_errors": getattr(self, "consecutive_errors", 0),
                    "heartbeat": utc_now().isoformat(),
                    "paper_trading": bool(getattr(getattr(self, "config", None), "paper_trading", True)),
                    "kill_switch": bool(getattr(self, "kill_switch", True)),
                },
                "market": {
                    "symbol": settings.get("symbol", "XAUUSD"),
                    "price": 0, "spread": 0,
                    "timestamp": utc_now().isoformat(),
                    "candles": {"M5": [], "M15": [], "H1": []},
                    "prices": {}, "structure": {},
                },
                "decision": {"decision": "WAIT", "confidence": 0, "reasons": [str(exc)], "plan": None},
                "risk": None, "agents": [], "account": {}, "history": [],
                "errors": list(getattr(self, "errors", []))[:20],
                "events": list(getattr(self, "events", []))[:20],
                "settings": settings,
                "watchlist": {},
            }

    def _serialize_inner(self):
        # Caller holds self.lock when possible
        r = self.last
        # Always return a full payload so the dashboard never breaks on first paint
        if not r:
            return {
                "version": VERSION,
                "system": {
                    "status": "starting" if self.running else "stopped",
                    "paused": self.paused,
                    "kill_switch": self.kill_switch,
                    "paper_trading": self.config.paper_trading,
                    "live_execution_enabled": self.config.live_trading_enabled,
                    "live_armed": self.live_armed,
                    "live_ready": False,
                    "demo_mode": self.demo_mode,
                    "mt5_connected": self.mt5_connected,
                    "connection_detail": self.connection_detail,
                    "market_open": getattr(self, "market_open", False),
                    "session": getattr(self, "session_info", {}),
                    "interval": self.interval,
                    "uptime_seconds": int((utc_now() - self.started).total_seconds()),
                    "activity": self.activity or "INITIALIZING",
                    "continuous": True,
                    "total_cycles": self.total_cycles,
                    "consecutive_errors": self.consecutive_errors,
                    "cycle_latency_ms": self.last_cycle_ms,
                    "account_sync": "LIVE" if self.account_live else ("MT5" if self.mt5_connected else "OFFLINE"),
                    "guardian": self.guardian_status(),
                    "heartbeat": utc_now().isoformat(),
                },
                "market": {
                    "symbol": self.config.symbol,
                    "price": 0,
                    "spread": 0,
                    "volume": 0,
                    "timestamp": utc_now().isoformat(),
                    "timeframes": ["M5", "M15", "H1"],
                    "prices": {},
                    "structure": {},
                    "candles": self.candle_series,
                },
                "decision": {"decision": "WAIT", "confidence": 0, "reasons": ["Engine warming up — first cycle in progress"], "plan": None},
                "risk": None,
                "ai_mesh": {},
                "agents": [],
                "agent_status": self.agent_status,
                "account": {"equity": 10000, "daily_pnl": 0, "exposure": 0, "open_positions": 0},
                "positions": [],
                "history": self.history,
                "errors": self.errors,
                "events": self.events[:80],
                "last_execution": None,
                "execution_diagnostics": [],
                "pipeline_state": self._pipeline_snapshot(),
                "orders": [],
                "lifecycle": self.lifecycle_state,
                "management": self.management_state,
                "universe": {**universe_snapshot, "results": (universe_snapshot.get("results", []) or [])[:25]},
                "market_desks": self.market_directors.summary(self.scan_queue.stats()),
                "performance": {"summary": {}, "agents": [], "calibration": [], "recent": []},
                "research": self.research_state,
                "settings": self._settings_public(),
                "watchlist": self.watchlist_quotes or {s: {"symbol": s, "price": 0} for s in self.watchlist},
                # Catalogues are static-ish and fetched on demand via /api/symbol-catalog.
            }
        snap = r["snapshot"]
        packet = r["packet"]
        risk = r["risk"]
        universe_snapshot = self._universe_snapshot()
        candles = getattr(self, "candle_series", {"M5": [], "M15": [], "H1": []}) or {}
        return {
            "version": VERSION,
            "system": {
                "status": "running" if self.running else "stopped",
                "paused": self.paused,
                "kill_switch": self.kill_switch,
                "paper_trading": self.config.paper_trading,
                "live_execution_enabled": self.config.live_trading_enabled,
                "live_armed": self.live_armed,
                "live_ready": self.live_ready(),
                "demo_mode": self.demo_mode,
                "mt5_connected": self.mt5_connected,
                "connection_detail": self.connection_detail,
                "market_open": self.market_open,
                "session": self.session_info,
                "interval": self.interval,
                "uptime_seconds": int((utc_now() - self.started).total_seconds()),
                "activity": self.activity,
                "last_cycle_started": self.last_cycle_started.isoformat() if self.last_cycle_started else None,
                "last_cycle_finished": self.last_cycle_finished.isoformat() if self.last_cycle_finished else None,
                "cycle_latency_ms": self.last_cycle_ms,
                "continuous": True,
                "auto_recover": self.auto_recover,
                "total_cycles": self.total_cycles,
                "consecutive_errors": self.consecutive_errors,
                "last_error_time": self.last_error_time.isoformat() if self.last_error_time else None,
                "next_retry_at": datetime.fromtimestamp(self.next_retry_at, timezone.utc).isoformat() if self.next_retry_at else None,
                "heartbeat": utc_now().isoformat(),
                "guardian": self.guardian_status(),
                "account_sync": "LIVE" if self.account_live else ("MT5" if self.mt5_connected else "OFFLINE"),
                "trade_attempts": self.trade_attempts,
                "last_order_client_id": self.last_order_client_id,
                "last_account_sync": self.last_account_sync.isoformat() if self.last_account_sync else None,
            },
            "market": {
                "symbol": snap.symbol,
                "price": snap.price,
                "spread": snap.spread,
                "volume": snap.volume,
                "timestamp": snap.timestamp.isoformat() if hasattr(snap.timestamp, "isoformat") else str(snap.timestamp),
                "timeframes": snap.metadata.get("timeframes", []),
                "prices": snap.metadata.get("mtf_prices", {}),
                "structure": r["structure"],
                "candles": {"M5": list(candles.get("M5", []) or [])[-80:],
                            "M15": list(candles.get("M15", []) or [])[-60:],
                            "H1": list(candles.get("H1", []) or [])[-50:]},
                "bid": snap.metadata.get("bid"),
                "ask": snap.metadata.get("ask"),
                "last": snap.metadata.get("last"),
                "realtime": bool(snap.metadata.get("realtime")),
                "quote_fresh": snap.metadata.get("quote_fresh"),
                "quote_status": snap.metadata.get("quote_status"),
                "tick_age_seconds": snap.metadata.get("tick_age_seconds"),
                "tick_raw_age_seconds": snap.metadata.get("tick_raw_age_seconds"),
                "tick_timestamp_offset_seconds": snap.metadata.get("tick_timestamp_offset_seconds", 0),
                "tick_timestamp_offset_candidate_seconds": snap.metadata.get("tick_timestamp_offset_candidate_seconds"),
                "tick_timestamp_candidate_residual_age_seconds": snap.metadata.get("tick_timestamp_candidate_residual_age_seconds"),
                "tick_clock_diagnostic": snap.metadata.get("tick_clock_diagnostic"),
                "timestamp_integrity_status": snap.metadata.get("timestamp_integrity_status"),
                "system_clock_utc": snap.metadata.get("system_clock_utc"),
                "latest_bar_time_utc": snap.metadata.get("latest_bar_time_utc"),
                "latest_bar_age_seconds": snap.metadata.get("latest_bar_age_seconds"),
                "bar_timestamp_in_future": snap.metadata.get("bar_timestamp_in_future"),
                "tick_time_fields_consistent": snap.metadata.get("tick_time_fields_consistent"),
                "tick_time_fields_delta_seconds": snap.metadata.get("tick_time_fields_delta_seconds"),
                "timestamp_offset_configuration_conflict": snap.metadata.get("timestamp_offset_configuration_conflict"),
                "tick_time": snap.metadata.get("tick_time"),
                "tick_time_msc": snap.metadata.get("tick_time_msc"),
                "tick_time_normalized": snap.metadata.get("tick_time_normalized"),
                "tick_time_msc_normalized": snap.metadata.get("tick_time_msc_normalized"),
                "broker_symbol": snap.metadata.get("broker_symbol"),
                "market_open": self.market_open,
                "session": self.session_info,
            },
            "decision": {
                "decision": packet.decision.value,
                "confidence": packet.confidence,
                "reasons": packet.reasons,
                "plan": asdict(packet.plan) if packet.plan else None,
            },
            "risk": asdict(risk) if risk else None,
            "ai_mesh": self.ai_mesh_state,
            "continuous": self._continuous_state_safe(),
            "agents": [asdict(s) for s in r["signals"]],
            "agent_status": self.agent_status,
            "account": r["account"],
            # Large panels are lazy-loaded from paginated endpoints when their page is active.
            "positions": [],
            "history": self.history[-20:],
            "errors": list(self.errors[-12:]),
            "events": list(self.events[-12:]),
            "last_execution": r["execution"],
            "execution_diagnostics": self.execution_diagnostics,
            "pipeline_state": self._pipeline_snapshot(),
            "orders": [],
            "lifecycle": self.lifecycle_state,
            "management": self.management_state,
            "universe": {**universe_snapshot, "results": (universe_snapshot.get("results", []) or [])[:25]},
            "market_desks": self.market_directors.summary(self.scan_queue.stats()),
            "watchlist": self.watchlist_quotes or {s: {"symbol": s, "price": 0} for s in self.watchlist if s.upper() != "GBXUSD"},
            # Catalogue and performance detail are demand-loaded and independently cached.
            "performance": _RESPONSE_CACHE.get_or_create(
                "performance_snapshot", 3.0,
                lambda: {"summary": self.performance.summary(), "agents": self.performance.agent_summary(),
                         "calibration": self.performance.calibration(), "recent": self.performance.recent(30)},
                stale_if_error=True, stale_ttl=15.0, wait_timeout=2.0, ttl_jitter=0.1,
            ),
            "research": self.research_state,
            "shadow": self.shadow_state,
            "research_memory": self.research_memory.recent(10),
            "run_card": self.last_run_card,
            "run_cards": list_run_cards(limit=8),
            "strategies": self.strategy_registry.list(20),
            "progress": self.progress_state[-20:],
            "settings": self._settings_public(),
        }


def _symbol_catalog_page(page: int = 1, page_size: int = 5000, query: str = "") -> dict:
    rows = catalog_list()
    query = str(query or "").strip().upper()
    if query:
        rows = [row for row in rows if query in str(row.get("symbol", "")).upper() or query in str(row.get("name", "")).upper()]
    total = len(rows)
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(max(1, int(page)), pages)
    offset = (page - 1) * page_size
    items = rows[offset:offset + page_size]
    return {"symbols": items, "items": items, "page": page, "page_size": page_size, "total": total, "pages": pages,
            "has_previous": page > 1, "has_next": page < pages}

RUNTIME = Runtime()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def send_bytes(self, raw: bytes, content_type: str, code=200):
        try:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            # Client navigated away / aborted the poll — not a server fault.
            return

    def send_json(self, obj, code=200):
        # Mutating POST handlers invalidate their tiny server snapshot caches only
        # when the handler reaches its response path (after the action has run).
        if getattr(self, "command", "GET") == "POST":
            _RESPONSE_CACHE.invalidate("api_state_snapshot")
            if "registry" in str(getattr(self, "path", "")).lower():
                _RESPONSE_CACHE.invalidate("registry:")
        raw = json.dumps(obj, default=str).encode("utf-8")
        self.send_bytes(raw, "application/json; charset=utf-8", code)

    def read_json(self):
        """Read the request body once or reuse the parsed POST payload."""
        from trading_team.web.request_utils import read_json_body
        return read_json_body(self)

    def do_GET(self):
        p = urlparse(self.path)
        # Normalize trailing slash so /api/mt5/bridge/ matches /api/mt5/bridge
        norm = p.path.rstrip("/") or "/"
        p = p._replace(path=norm)
        if p.path in ("/", "/index.html"):
            self.send_bytes((STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            return
        if p.path == "/api/finance/status":
            try:
                from trading_team.research.finance_integration import toolkit_status
                self.send_json(toolkit_status())
            except Exception as exc:
                self.send_json({"ok": False, "error": f"Finance toolkit status unavailable: {type(exc).__name__}: {exc}"}, 503)
            return
        if p.path == "/api/state":
            try:
                force_snapshot = str((parse_qs(p.query).get("refresh") or ["0"])[0]).lower() in {"1", "true", "yes"}
                if force_snapshot:
                    snapshot = RUNTIME.serialize()
                else:
                    # Short server-side single-flight cache collapses duplicate serializations
                    # when several dashboard users poll the same live snapshot together.
                    snapshot = _RESPONSE_CACHE.get_or_create(
                        "api_state_snapshot", 0.25, RUNTIME.serialize,
                        stale_if_error=False, stale_ttl=0.0, wait_timeout=5.0, ttl_jitter=0.1,
                    )
                self.send_json(snapshot)
            except Exception as exc:
                self.send_json({"status": "error", "error": f"State snapshot unavailable: {type(exc).__name__}: {exc}"[:300]}, 500)
            return
        if p.path in ("/metrics", "/api/metrics"):
            try:
                from trading_team.core.observability import get_metrics
                body = get_metrics().render_prometheus()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(body.encode("utf-8"))))
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            except Exception as exc:
                self.send_json({"error": str(exc)}, 500)
            return
        if p.path == "/api/health":
            self.send_json({"status": "ok", "service": "ai-trading-team-web", "version": VERSION})
            return
        if p.path == "/api/regime":
            try:
                from trading_team.monitor.dashboard import get_regime_state
                self.send_json(get_regime_state())
            except Exception as exc:
                self.send_json({"status": "error", "error": str(exc)[:200]})
            return
        if p.path in ("/api/hedge", "/api/greeks"):
            try:
                from trading_team.monitor.dashboard import get_hedge_state
                self.send_json(get_hedge_state())
            except Exception as exc:
                self.send_json({"status": "error", "error": str(exc)[:200]})
            return
        if p.path == "/api/regime/dashboard":
            try:
                from trading_team.monitor.dashboard import get_combined_dashboard
                self.send_json(get_combined_dashboard())
            except Exception as exc:
                self.send_json({"status": "error", "error": str(exc)[:200]})
            return
        if p.path == "/regime":
            try:
                from trading_team.monitor.dashboard import _REGIME_HTML
                self.send_bytes(_REGIME_HTML.encode("utf-8"), "text/html; charset=utf-8")
            except Exception as exc:
                self.send_json({"status": "error", "error": str(exc)[:200]})
            return
        if p.path in ("/api/stream", "/stream"):
            qs = parse_qs(p.query or "")
            topics = qs.get("topics", ["regime,hedge,alert,feed_health,greeks"])
            topic_list = [t.strip() for t in topics[0].split(",") if t.strip()]
            try:
                from trading_team.stream.sse import SSEBroker
                broker = SSEBroker(topics=topic_list)
            except Exception as exc:
                self.send_json({"status": "error", "error": str(exc)[:200]})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                for chunk in broker.stream(max_seconds=300.0):
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            finally:
                broker.close()
            return
        if p.path == "/api/surface":
            qs = parse_qs(p.query or "")
            try:
                from trading_team.risk.vol_surface import get_surface
                und = (qs.get("underlying") or ["default"])[0]
                self.send_json(get_surface(und).to_dict())
            except Exception as exc:
                self.send_json({"status": "error", "error": str(exc)[:200]})
            return
        if p.path == "/api/alerts":
            try:
                from trading_team.monitor.alerts import get_alert_engine
                self.send_json({"alerts": get_alert_engine().recent(40)})
            except Exception as exc:
                self.send_json({"status": "error", "error": str(exc)[:200]})
            return
        if p.path == "/api/feed/health":
            try:
                from trading_team.data.realtime import get_feed_hub
                hub = get_feed_hub()
                rows = hub.health()
                self.send_json({"feeds": [h.to_dict() for h in (rows if isinstance(rows, list) else [rows])]})
            except Exception as exc:
                self.send_json({"status": "error", "error": str(exc)[:200]})
            return
        if p.path == "/api/feed/snapshot":
            try:
                from trading_team.data.realtime import get_feed_hub
                self.send_json(get_feed_hub().snapshot_for_hedge())
            except Exception as exc:
                self.send_json({"status": "error", "error": str(exc)[:200]})
            return
        if p.path == "/api/market/registry":
            values = parse_qs(p.query)
            try:
                force = str((values.get("refresh") or ["0"])[0]).lower() in {"1", "true", "yes"}
                limit = max(1, min(5000, int((values.get("page_size") or values.get("limit") or ["500"])[0])))
                page = max(1, int((values.get("page") or ["1"])[0]))
                offset = max(0, int((values.get("offset") or [str((page - 1) * limit)])[0]))
                status = str((values.get("status") or [""])[0]).lower() or None
                desk = str((values.get("desk") or [""])[0]).lower() or None
                search = str((values.get("q") or [""])[0]).strip() or None
                cache_key = f"registry:{status or '*'}:{desk or '*'}:{search or '*'}:{offset}:{limit}"
                if force:
                    _RESPONSE_CACHE.invalidate("registry:")
                else:
                    cached_payload = _RESPONSE_CACHE.get(cache_key)
                    if cached_payload is not None:
                        self.send_json(cached_payload)
                        return
                sync_result = RUNTIME._sync_instrument_registry(force=True) if force else {"status": "cached_or_not_requested"}
                rows = RUNTIME.instrument_registry.list(limit=limit, offset=offset, status=status, desk=desk, search=search, active_only=True)
                total = RUNTIME.instrument_registry.count(status=status, desk=desk, search=search, active_only=True)
                pages = max(1, (total + limit - 1) // limit)
                page = offset // limit + 1
                if page > pages:
                    page = pages
                    offset = (page - 1) * limit
                    rows = RUNTIME.instrument_registry.list(limit=limit, offset=offset, status=status, desk=desk, search=search, active_only=True)
                if search and not any(str(r.get("broker_symbol", "")).upper() == search.upper() for r in rows):
                    provisional = RUNTIME.instrument_registry.get(search)
                    if provisional and not provisional.get("raw_metadata"):
                        rows = [provisional, *rows][:limit]
                bridge = RUNTIME.feed.get_bridge() if RUNTIME.feed is not None and RUNTIME.mt5_connected else None
                try:
                    source_total = bridge.symbols_total() if bridge else None
                except Exception:
                    source_total = None
                summary = RUNTIME.instrument_registry.summary()
                payload = {"version": VERSION, "summary": summary, "source_total": source_total,
                           "source_complete": bool(summary.get("snapshot_complete")), "last_sync": sync_result,
                           "symbols": rows, "count": len(rows), "total": total, "page": page,
                           "page_size": limit, "pages": pages, "offset": offset,
                           "has_previous": offset > 0, "has_next": offset + len(rows) < total,
                           "next_offset": offset + len(rows) if offset + len(rows) < total else None,
                           "note": "Registry classification is not order authorization; live order preflight rechecks exact broker metadata and quote freshness."}
                if not force:
                    _RESPONSE_CACHE.set(cache_key, payload, ttl=3.0, stale_ttl=15.0, ttl_jitter=0.1)
                self.send_json(payload)
            except Exception as exc:
                self.send_json({"error": str(exc), "summary": RUNTIME.instrument_registry.summary()}, 500)
            return
        if p.path == "/api/diagnostics/pipeline":
            try:
                self.send_json({"version": VERSION, "pipeline": RUNTIME._pipeline_snapshot(),
                                "execution_diagnostics": list(RUNTIME.execution_diagnostics),
                                "last_execution": RUNTIME.last_execution,
                                "note": "A cycle conclusion is distinct from a broker-verified order fill."})
            except Exception as exc:
                self.send_json({"error": str(exc)}, 500)
            return
        if p.path == "/api/market/orchestration":
            try:
                self.send_json({"version": VERSION, "queue": RUNTIME.scan_queue.queue_summary(),
                                "worker": dict(RUNTIME._universe_worker_status),
                                "universe": {k: v for k, v in RUNTIME._universe_snapshot().items() if k != "results"},
                                "pipeline_state": RUNTIME._pipeline_snapshot(),
                                "market_desks": RUNTIME.market_directors.summary(RUNTIME.scan_queue.stats()),
                                "enabled": bool(RUNTIME.analyze_all_symbols_enabled),
                                "scan_limit": RUNTIME.all_symbol_scan_limit or "ALL",
                                "batch_size": RUNTIME.all_symbol_scan_batch_size,
                                "rescan_seconds": RUNTIME.all_symbol_rescan_seconds,
                                "note": "Market directors perform numerical discovery; only the shared specialist review provides deeper research, and all universe results remain non-authorizing."})
            except Exception as exc:
                self.send_json({"error": str(exc)}, 500)
            return
        if p.path == "/api/market/desks":
            try:
                self.send_json({"version": VERSION,
                                "market_desks": RUNTIME.market_directors.summary(RUNTIME.scan_queue.stats()),
                                "enabled": bool(RUNTIME.analyze_all_symbols_enabled),
                                "all_symbol_scan_limit": RUNTIME.all_symbol_scan_limit,
                                "all_symbol_scan_batch_size": RUNTIME.all_symbol_scan_batch_size,
                                "note": "Desk triggers are screening rules, not calibrated win probabilities. CFD contract supervision and underlying-market reports are correlated context and are not double-counted as independent alpha."})
            except Exception as exc:
                self.send_json({"error": str(exc)}, 500)
            return
        if p.path == "/api/providers":
            provider = RUNTIME.agent_mesh.provider
            self.send_json({
                "version": VERSION,
                "llm": provider.status(),
                "intelligence": {"mode": "classical_only", "language_model_calls_enabled": False,
                                 "decision_engine": "numerical specialist analysts + statistical research + deterministic rule gates",
                                 "reinforcement_learning": "offline research only; non-voting; no trade authorization"},
                "news": {"configured_feeds": RUNTIME.news_provider.configured_count, "last_fetch": RUNTIME.news_provider.last_fetch_at, "items": len(RUNTIME.news_provider.last_items or RUNTIME.news_items), "refreshing": RUNTIME.news_provider.refreshing, "gdelt_enabled": RUNTIME.news_provider.gdelt_enabled, "newsapi_enabled": bool(getattr(RUNTIME.news_provider, "newsapi_enabled", False)), "newsapi_key_configured": bool(getattr(RUNTIME.news_provider, "newsapi_key_configured", False)), "newsapi_mode": getattr(RUNTIME.news_provider, "newsapi_mode", "everything"), "tradingnews_press": RUNTIME.news_provider.tradingnews_press.status(), "free_news_sources": RUNTIME.news_provider.free_news_sources.status(), "feeds": RUNTIME.news_provider.last_status, "analyst": getattr(getattr(RUNTIME, "news_analyst", None), "ollama_status", {"status": "unavailable"})},
                "sentiment": {"status": "local_rule_based_context_only", "source": "news_sentiment.py", "is_model_sentiment": False, "fresh_items": analyze_news(RUNTIME.news_items[:30])["fresh_headline_count"]},
                "live_execution": {"enabled_by_config": RUNTIME.config.live_trading_enabled, "armed": RUNTIME.live_armed, "kill_switch": RUNTIME.kill_switch}
            })
            return
        if p.path == "/api/news/analysis":
            provider = RUNTIME.news_provider
            if provider.last_items:
                RUNTIME.news_items = provider.last_items
            qs = parse_qs(p.query or "")
            requested_symbol = str((qs.get("symbol") or [getattr(getattr(RUNTIME, "config", None), "symbol", "XAUUSD")])[0]).strip().upper()[:32]
            engine = getattr(RUNTIME, "news_analyst", None)
            if engine is None:
                self.send_json({"status": "ANALYST_UNAVAILABLE", "instrument": requested_symbol, "events": [], "directional_vote_allowed": False}, 503)
                return
            try:
                snapshot = getattr(RUNTIME, "last_snapshot", None)
                if snapshot is not None and str(getattr(snapshot, "symbol", "")).upper() != requested_symbol:
                    snapshot = None  # never measure a different instrument's candles as this symbol's reaction
                report = engine.analyze_instrument(RUNTIME.news_items or [], requested_symbol, snapshot=snapshot, now=utc_now())
                self.send_json({"status": "ok", "report": report, "provider_status": provider.status()})
            except Exception as analysis_exc:
                self.send_json({"status": "error", "instrument": requested_symbol, "error_type": type(analysis_exc).__name__,
                                "directional_vote_allowed": False, "order_authorization": False}, 500)
            return
        if p.path == "/api/news":
            provider = RUNTIME.news_provider
            if provider.last_items:
                RUNTIME.news_items = provider.last_items
            qs = parse_qs(p.query or "")
            category = str((qs.get("category") or ["all"])[0]).strip().lower()
            query = str((qs.get("q") or [""])[0]).strip().lower()[:120]
            try:
                limit = max(1, min(100, int((qs.get("limit") or ["100"])[0])))
            except (TypeError, ValueError):
                limit = 100
            rows = list(RUNTIME.news_items or [])
            if category not in {"", "all"}:
                rows = [row for row in rows if category in (row.get("categories") or [])]
            if query:
                rows = [row for row in rows if query in " ".join(str(row.get(k) or "") for k in ("title", "summary", "source", "provider")).lower()]
            rows.sort(key=lambda row: str(row.get("published") or ""), reverse=True)
            total_count = len(rows)
            # Enrich the bounded newsroom sample with an explainable, deterministic research
            # priority and event/asset context. This is analyst triage metadata, not a signal
            # to trade and not a forecast of asset-price direction.
            rows = enrich_news_items(rows[:limit])
            intelligence = build_news_intelligence(rows)
            status = provider.status()
            analysis_engine = getattr(RUNTIME, "news_analyst", None)
            try:
                impact_analysis = analysis_engine.analyze_feed(RUNTIME.news_items or [], queue_llm=False) if analysis_engine else {"status": "ANALYST_UNAVAILABLE", "events": [], "directional_vote_allowed": False}
            except Exception as analysis_exc:
                impact_analysis = {"status": "ANALYSIS_UNAVAILABLE", "error_type": type(analysis_exc).__name__, "events": [], "directional_vote_allowed": False}
            # Attach the same structured analysis to story detail cards, keyed by exact publisher URL
            # first and normalized headline second. Missing matches remain unannotated, never guessed.
            analyst_events = impact_analysis.get("events") if isinstance(impact_analysis, dict) else []
            analyst_by_url = {str(event.get("url") or ""): event for event in analyst_events if isinstance(event, dict) and event.get("url")}
            analyst_by_title = {str(event.get("headline") or "").strip().casefold(): event for event in analyst_events if isinstance(event, dict) and event.get("headline")}
            for row in rows:
                match = analyst_by_url.get(str(row.get("url") or "")) or analyst_by_title.get(str(row.get("title") or "").strip().casefold())
                if match:
                    row["news_analyst"] = match
            last_snapshot = getattr(RUNTIME, "last_snapshot", None)
            snapshot_metadata = getattr(last_snapshot, "metadata", {}) or {}
            instrument_analysis = snapshot_metadata.get("news_analysis") if isinstance(snapshot_metadata, dict) else None
            if not isinstance(instrument_analysis, dict) and analysis_engine:
                try:
                    current_symbol = str(getattr(getattr(RUNTIME, "config", None), "symbol", "XAUUSD") or "XAUUSD").upper()
                    fallback_snapshot = last_snapshot if str(getattr(last_snapshot, "symbol", "")).upper() == current_symbol else None
                    instrument_analysis = analysis_engine.analyze_instrument(RUNTIME.news_items or [], current_symbol, snapshot=fallback_snapshot, now=utc_now())
                except Exception:
                    instrument_analysis = {"status": "ANALYSIS_UNAVAILABLE", "instrument": "UNKNOWN", "events": [], "directional_vote_allowed": False, "order_authorization": False}
            if not isinstance(instrument_analysis, dict):
                instrument_analysis = {"status": "WAITING_FOR_MARKET_SNAPSHOT", "instrument": str(getattr(getattr(RUNTIME, "config", None), "symbol", "XAUUSD") or "XAUUSD").upper(), "events": [], "directional_vote_allowed": False, "order_authorization": False}
            self.send_json({
                "items": rows, "count": total_count, "returned": len(rows),
                "intelligence": intelligence, "impact_analysis": impact_analysis, "instrument_analysis": instrument_analysis,
                "feeds": status["feeds"], "configured": bool(provider.enabled and provider.configured_count),
                "status": status, "last_fetch": provider.last_fetch_at,
                "refreshing": provider.refreshing, "open_data": "GDELT DOC API plus public RSS/Atom feeds and optional NewsAPI; headline metadata and publisher links only.",
                "caveat": "Headlines can be delayed, duplicated, syndicated, unverified or unavailable. Research priority is an ordinal queue score, not a percentage/probability or expected return. Country/person mentions do not create asset exposure. Automated analyst context requires explicit instrument linkage, plausible timestamps, publisher references and a matching event cluster across at least two distinct publisher domains; even then, news is context-only and cannot authorize a trade."
            })
            return
        if p.path == "/api/news/sentiment":
            self.send_json(analyze_news(RUNTIME.news_items[:30]))
            return
        if p.path == "/api/guardian":
            self.send_json(RUNTIME.guardian_status())
            return
        if p.path == "/api/conflict-map":
            try:
                from trading_team.ai_mesh.agent_schema import build_conflict_map
                reports = []
                mesh = getattr(RUNTIME, "last_mesh", None) or getattr(RUNTIME, "agent_mesh", None)
                last = getattr(mesh, "last_result", None) if mesh else None
                if last is not None and getattr(last, "reports", None):
                    reports = last.reports
                self.send_json(build_conflict_map(reports) if reports else {"status": "EMPTY", "table": "No reports yet"})
            except Exception as exc:
                self.send_json({"error": str(exc)}, 500)
            return
        if p.path in ("/api/backtest/lab", "/api/backtest/templates"):
            try:
                from trading_team.backtest.strategy_factory import list_templates, run_strategy_lab, StrategySpec, run_multi_method_backtest
                if p.path.endswith("/templates"):
                    self.send_json({"templates": list_templates()})
                    return
                closes = [4000 + i * 0.05 for i in range(180)]
                highs = [c + 0.3 for c in closes]
                lows = [c - 0.3 for c in closes]
                try:
                    snap = getattr(RUNTIME, "last_snapshot", None)
                    if snap is not None:
                        bars = ((snap.candles or {}).get("M5") or [])
                        if len(bars) >= 80:
                            closes = [float(b.close) for b in bars]
                            highs = [float(getattr(b, "high", b.close)) for b in bars]
                            lows = [float(getattr(b, "low", b.close)) for b in bars]
                except Exception:
                    pass
                self.send_json(run_strategy_lab(closes, highs=highs, lows=lows))
            except Exception as exc:
                self.send_json({"error": str(exc)}, 500)
            return
        if p.path == "/api/backtest/custom":
            try:
                from trading_team.backtest.strategy_factory import StrategySpec, run_multi_method_backtest
                # GET with query is limited; accept defaults for smoke
                q = {k: v[0] if isinstance(v, list) else v for k, v in __import__("urllib.parse", fromlist=["parse_qs"]).parse_qs(p.query).items()}
                spec = StrategySpec(
                    strategy_id=str(q.get("id") or "custom"),
                    name=str(q.get("name") or "Custom"),
                    method=str(q.get("method") or "factor"),
                    engine=str(q.get("engine") or "bar_ohlc"),
                    params={
                        "lookback": int(q.get("lookback") or 20),
                        "entry_z": float(q.get("entry_z") or 2.0),
                        "atr_stop": float(q.get("atr_stop") or 1.5),
                        "atr_target": float(q.get("atr_target") or 2.5),
                    },
                )
                closes = [4000 + i * 0.05 for i in range(150)]
                self.send_json(run_multi_method_backtest(closes, spec).to_dict())
            except Exception as exc:
                self.send_json({"error": str(exc)}, 500)
            return
        if p.path in ("/api/features/engine", "/api/feature-engine"):
            try:
                from trading_team.research.feature_engineering import FeatureEngineeringEngine, get_feature_registry
                closes = []
                highs = lows = opens = None
                sym = "XAUUSD"
                try:
                    snap = getattr(RUNTIME, "last_snapshot", None)
                    if snap is not None:
                        sym = str(getattr(snap, "symbol", sym) or sym)
                        bars = ((snap.candles or {}).get("M5") or (snap.candles or {}).get("M15") or [])
                        closes = [float(b.close) for b in bars if getattr(b, "close", None)]
                        highs = [float(getattr(b, "high", b.close)) for b in bars]
                        lows = [float(getattr(b, "low", b.close)) for b in bars]
                except Exception:
                    pass
                if len(closes) < 30:
                    closes = [4000 + i * 0.05 for i in range(100)]
                    highs = [c + 0.3 for c in closes]
                    lows = [c - 0.3 for c in closes]
                eng = FeatureEngineeringEngine()
                vec = eng.transform(
                    closes, symbol=sym, highs=highs, lows=lows,
                    include_interactions=False, scale="winsor", persist=False,
                )
                self.send_json({
                    "vector": vec.to_dict(),
                    "registry": get_feature_registry().keys(),
                })
            except Exception as exc:
                self.send_json({"error": str(exc)}, 500)
            return
        if p.path in ("/api/data-lake", "/api/alt-data"):
            try:
                from trading_team.data.alt_data_layer import snapshot_alt_data
                from trading_team.data.data_lake import get_data_lake
                news = getattr(RUNTIME, "news_items", None) or []
                # optional multi-asset from watchlist quotes — synthetic if empty
                multi = {}
                try:
                    # use last snapshot single series as minimal graph node
                    snap = getattr(RUNTIME, "last_snapshot", None)
                    if snap is not None and getattr(snap, "candles", None):
                        bars = (snap.candles or {}).get("M5") or []
                        closes = [float(b.close) for b in bars if getattr(b, "close", None)]
                        if closes:
                            multi[str(getattr(snap, "symbol", "XAUUSD"))] = closes
                except Exception:
                    pass
                if len(multi) < 2:
                    import random
                    rng = random.Random(2)
                    for s in ("XAUUSD", "EURUSD", "US500", "DXY"):
                        multi[s] = [100 + i * 0.01 + rng.gauss(0, 0.2) for i in range(120)]
                sym = "XAUUSD"
                try:
                    sym = str(getattr(getattr(RUNTIME, "config", None), "symbol", None) or "XAUUSD")
                except Exception:
                    pass
                out = snapshot_alt_data(symbol=sym, news_items=news, multi_asset_closes=multi)
                out["lake_stats"] = {
                    "ticks": len(get_data_lake().ticks(sym, limit=10)),
                    "features": len(get_data_lake().features(sym, limit=10)),
                }
                self.send_json(out)
            except Exception as exc:
                self.send_json({"error": str(exc)}, 500)
            return
        if p.path == "/api/hardening":
            try:
                from trading_team.core.production_hardening import (
                    current_environment, live_trading_flag, get_audit_log,
                    get_chaos, check_failover_status, write_heartbeat, load_failover_config,
                )
                write_heartbeat()
                self.send_json({
                    "environment": current_environment().value,
                    "live_trading_enabled": live_trading_flag(),
                    "audit": get_audit_log().verify_chain(),
                    "chaos": get_chaos().snapshot(),
                    "failover": check_failover_status(),
                    "failover_config": load_failover_config().to_dict(),
                })
            except Exception as exc:
                self.send_json({"error": str(exc)}, 500)
            return
        if p.path == "/api/hardening/audit/verify":
            try:
                from trading_team.core.production_hardening import get_audit_log
                self.send_json(get_audit_log().verify_chain())
            except Exception as exc:
                self.send_json({"error": str(exc)}, 500)
            return
        if p.path == "/api/execution/package":
            try:
                from trading_team.adapters.microstructure_exec import build_execution_package
                q = {k: v[0] if isinstance(v, list) else v for k, v in __import__("urllib.parse", fromlist=["parse_qs"]).parse_qs(p.query).items()}
                vol = float(q.get("volume") or 0.1)
                side = str(q.get("side") or "BUY")
                mid = float(q.get("mid") or 0)
                if mid <= 0:
                    try:
                        mid = float(getattr(getattr(RUNTIME, "last_snapshot", None), "price", 0) or 0) or 4000.0
                    except Exception:
                        mid = 4000.0
                spread = float(q.get("spread_bps") or 1.0)
                pkg = build_execution_package(
                    volume=vol, side=side, mid=mid, spread_bps=spread,
                    volatility=float(q.get("vol") or 0.001),
                    adv=float(q.get("adv") or 500.0),
                    urgency=float(q.get("urgency") or 0.5),
                )
                self.send_json(pkg)
            except Exception as exc:
                self.send_json({"error": str(exc)}, 500)
            return
        if p.path == "/api/portfolio/live":
            try:
                from trading_team.research.portfolio_suite import summarize_live_book
                if not RUNTIME._connect_mt5():
                    self.send_json({"ok": False, "error": "A connected MT5 terminal is required for the live portfolio book.",
                                    "code": "MT5_UNAVAILABLE", "detail": str(RUNTIME.connection_detail)}, 503); return
                bridge = RUNTIME.feed.get_bridge()
                account = bridge.account_info()
                positions = bridge.positions()
                orders = bridge.orders()
                # Save a bounded, once-per-minute sample so the Portfolio page can
                # show observed account history. This is not a full-time ledger/TWR.
                account_history = []
                try:
                    from trading_team.storage.portfolio_history import PortfolioSnapshotStore
                    snapshots = PortfolioSnapshotStore(DB_PATH)
                    snapshots.record(account, min_interval_seconds=max(15.0, float(os.getenv("PORTFOLIO_SNAPSHOT_INTERVAL_SECONDS", "60"))))
                    account_history = snapshots.list(days=365, limit=600, account_login=account.get("login"), account_server=account.get("server"))
                except Exception as history_exc:
                    RUNTIME.error(f"Portfolio snapshot history unavailable: {history_exc}", traceback.format_exc())
                enriched = []
                for raw_pos in positions:
                    row = dict(raw_pos)
                    row["stop_risk_cash"] = None
                    row["stop_pnl_at_sl"] = None
                    sl = safe_number(row.get("sl"))
                    entry = safe_number(row.get("price_open"))
                    if sl > 0 and entry > 0:
                        try:
                            pnl_at_sl = bridge.order_calc_profit(str(row.get("symbol") or ""), str(row.get("side") or "BUY"),
                                                                 safe_number(row.get("volume")), entry, sl)
                            row["stop_pnl_at_sl"] = float(pnl_at_sl)
                            row["stop_risk_cash"] = max(0.0, -float(pnl_at_sl))
                        except Exception as risk_exc:
                            row["stop_risk_cash_error"] = str(risk_exc)[:180]
                    enriched.append(row)
                payload = summarize_live_book(account, enriched, orders)
                payload["connection"] = {"terminal_connected": True, "server": account.get("server"),
                                         "account_currency": account.get("currency")}
                payload["account_history"] = account_history
                self.send_json(payload)
            except Exception as exc:
                self.send_json({"ok": False, "error": f"Live portfolio snapshot failed: {type(exc).__name__}: {exc}",
                                "code": "PORTFOLIO_LIVE_FAILED"}, 503)
            return
        if p.path == "/api/trade-learning/summary":
            try:
                from trading_team.research.trade_history_learning import TradeHistoryLearningStore
                qs = parse_qs(p.query or "")
                days = max(1, min(3650, int((qs.get("days") or ["365"])[0])))
                limit = max(1, min(200, int((qs.get("limit") or ["50"])[0])))
                offset = max(0, min(1_000_000_000, int((qs.get("offset") or ["0"])[0])))
                self.send_json(TradeHistoryLearningStore(DB_PATH).summary(days=days, limit=limit, offset=offset))
            except Exception as exc:
                self.send_json({"ok": False, "error": f"Trade-learning summary unavailable: {type(exc).__name__}: {str(exc)[:180]}",
                                "code": "TRADE_LEARNING_SUMMARY_FAILED"}, 500)
            return
        if p.path == "/api/portfolio/performance":
            try:
                from trading_team.research.portfolio_suite import summarize_closed_deals
                if not RUNTIME._connect_mt5():
                    self.send_json({"ok": False, "error": "A connected MT5 terminal is required for broker trade performance.",
                                    "code": "MT5_UNAVAILABLE", "detail": str(RUNTIME.connection_detail)}, 503); return
                qs = parse_qs(p.query or "")
                days = max(1, min(3650, int((qs.get("days") or ["90"])[0])))
                cache_key = f"portfolio_perf:{days}"
                payload = _RESPONSE_CACHE.get_or_create(
                    cache_key, 10.0,
                    lambda: summarize_closed_deals(RUNTIME.feed.get_bridge().history_deals(days=days), days=days),
                    stale_if_error=True, stale_ttl=20.0, wait_timeout=8.0,
                )
                self.send_json(payload)
            except Exception as exc:
                self.send_json({"ok": False, "error": f"Portfolio performance unavailable: {type(exc).__name__}: {exc}",
                                "code": "PORTFOLIO_PERFORMANCE_FAILED"}, 503)
            return
        if p.path == "/api/portfolio/analytics":
            try:
                from trading_team.research.portfolio_analytics import analyze_account_performance
                from trading_team.storage.portfolio_history import PortfolioSnapshotStore
                if not RUNTIME._connect_mt5():
                    self.send_json({"ok": False, "error": "A connected MT5 terminal is required for account performance analytics.",
                                    "code": "MT5_UNAVAILABLE", "detail": str(RUNTIME.connection_detail)}, 503); return
                qs = parse_qs(p.query or "")
                days = max(7, min(3650, int((qs.get("days") or ["90"])[0])))
                benchmark = str((qs.get("benchmark") or [""])[0]).strip().upper()[:64]
                factors_raw = str((qs.get("factors") or [""])[0])
                factors = []
                for value in factors_raw.split(","):
                    sym = value.strip().upper()[:64]
                    if sym and sym != benchmark and sym not in factors:
                        factors.append(sym)
                factors = factors[:5]
                bridge = RUNTIME.feed.get_bridge()
                account = bridge.account_info()
                snapshots = PortfolioSnapshotStore(DB_PATH).list(
                    days=days, limit=50000, account_login=account.get("login"), account_server=account.get("server"))
                deals = bridge.history_deals(days=min(3650, days + 3))
                market_rates = {}
                market_errors = {}
                requested = ([benchmark] if benchmark else []) + factors
                for symbol in dict.fromkeys(requested):
                    try:
                        market_rates[symbol] = bridge.rates(symbol, "D1", count=min(5000, days + 10), start_pos=1)
                    except Exception as market_exc:
                        market_errors[symbol] = f"{type(market_exc).__name__}: {market_exc}"
                payload = analyze_account_performance(
                    snapshots, deals, days=days, benchmark_symbol=benchmark, market_rates=market_rates)
                payload["account"] = {"login": str(account.get("login") or ""), "server": account.get("server"),
                                      "currency": account.get("currency")}
                payload["requested_market_symbols"] = list(dict.fromkeys(requested))
                payload["market_data_errors"] = market_errors
                if market_errors:
                    payload.setdefault("notes", []).append("Some benchmark/factor proxy symbols were unavailable from MT5; see market_data_errors.")
                self.send_json(payload)
            except Exception as exc:
                self.send_json({"ok": False, "error": f"Portfolio account analytics unavailable: {type(exc).__name__}: {exc}",
                                "code": "PORTFOLIO_ANALYTICS_FAILED"}, 503)
            return
        if p.path == "/api/portfolio/institutional":
            try:
                from trading_team.research.institutional_portfolio import run_institutional_portfolio
                # synthetic multi-asset if no returns store
                import random
                rng = random.Random(1)
                returns = {
                    "XAUUSD": [rng.gauss(0.0001, 0.005) for _ in range(80)],
                    "EURUSD": [rng.gauss(0.0, 0.004) for _ in range(80)],
                    "GBPUSD": [rng.gauss(0.0, 0.004) for _ in range(80)],
                    "US500": [rng.gauss(0.0002, 0.008) for _ in range(80)],
                }
                views = {}
                try:
                    mesh = getattr(RUNTIME, "ai_mesh_state", None) or {}
                    dec = str(mesh.get("decision") or "WAIT").upper()
                    conf = float(mesh.get("confidence") or 0)
                    if dec == "BUY":
                        views["XAUUSD"] = 0.01 * conf
                    elif dec == "SELL":
                        views["XAUUSD"] = -0.01 * conf
                except Exception:
                    pass
                self.send_json(run_institutional_portfolio(returns, mesh_views=views))
            except Exception as exc:
                self.send_json({"error": str(exc)}, 500)
            return
        if p.path == "/api/alpha-lab":
            try:
                from trading_team.research.alpha_lab import run_alpha_research
                # Use last snapshot closes if available
                closes = []
                try:
                    snap = getattr(RUNTIME, "last_snapshot", None)
                    if snap is not None:
                        bars = (snap.candles or {}).get("M5") or []
                        closes = [float(b.close) for b in bars if getattr(b, "close", None)]
                except Exception:
                    closes = []
                if len(closes) < 50:
                    closes = [4000 + (i % 7) * 0.1 for i in range(120)]
                sym = str(getattr(getattr(RUNTIME, "config", None), "symbol", None) or "XAUUSD")
                self.send_json(run_alpha_research(closes, symbol=sym))
            except Exception as exc:
                self.send_json({"error": str(exc)}, 500)
            return
        if p.path == "/api/continuous":
            try:
                from trading_team.ai_mesh.continuous_flow import continuous_status
                self.send_json(continuous_status())
            except Exception as exc:
                self.send_json({"error": str(exc)}, 500)
            return
        if p.path == "/api/continuous/inject":
            if self.command != "POST":
                self.send_json({"error": "POST required"}, 405)
                return
            try:
                body = self.read_json() if hasattr(self, "read_json") else {}
                from trading_team.ai_mesh.continuous_flow import get_evidence_bus
                entry = get_evidence_bus().inject_human(body or {})
                # Optional: force affected desk re-run on next cycle by bumping bus version
                self.send_json({"ok": True, "injection": entry, "bus_version": get_evidence_bus().snapshot().get("version")})
            except Exception as exc:
                self.send_json({"error": str(exc)}, 400)
            return
        if p.path == "/api/continuous/tracking":
            try:
                from trading_team.ai_mesh.continuous_flow import get_decision_tracker
                self.send_json({"recent": get_decision_tracker().recent(50)})
            except Exception as exc:
                self.send_json({"error": str(exc)}, 500)
            return
        if p.path == "/api/symbol-catalog":
            qs = parse_qs(p.query or "")
            try:
                page = max(1, int((qs.get("page") or ["1"])[0]))
                page_size = max(1, min(10000, int((qs.get("page_size") or qs.get("limit") or ["5000"])[0])))
                query = str((qs.get("q") or [""])[0]).strip().upper()
                key = f"symbol_catalog:{page}:{page_size}:{query or '*'}"
                payload = _RESPONSE_CACHE.get_or_create(key, 60.0, lambda: _symbol_catalog_page(page, page_size, query), stale_if_error=True, stale_ttl=300.0)
                self.send_json(payload)
            except Exception as exc:
                self.send_json({"error": str(exc), "symbols": [], "items": []}, 500)
            return
        if p.path == "/api/universe/page":
            qs = parse_qs(p.query or "")
            try:
                page = max(1, int((qs.get("page") or ["1"])[0]))
                page_size = max(1, min(250, int((qs.get("page_size") or ["50"])[0])))
                query = str((qs.get("q") or [""])[0]).strip()
                cache_key = f"universe_page:{page}:{page_size}:{query.upper()}"
                payload = _RESPONSE_CACHE.get_or_create(cache_key, 1.5, lambda: RUNTIME.scan_queue.candidate_results_page(page, page_size, query), stale_if_error=True, stale_ttl=5.0)
                self.send_json(payload)
            except Exception as exc:
                self.send_json({"error": str(exc), "items": [], "page": 1, "page_size": 50, "total": 0, "pages": 1}, 500)
            return
        if p.path == "/api/events":
            qs = parse_qs(p.query or "")
            page = max(1, int((qs.get("page") or ["1"])[0]))
            page_size = max(1, min(200, int((qs.get("page_size") or ["50"])[0])))
            events = list(RUNTIME.events)
            errors = list(RUNTIME.errors)
            offset = (page - 1) * page_size
            self.send_json({"events": events[offset:offset+page_size], "errors": errors[offset:offset+page_size],
                            "page": page, "page_size": page_size, "total": len(events),
                            "pages": max(1, (len(events)+page_size-1)//page_size), "error_total": len(errors)})
            return
        if p.path == "/api/positions":
            qs = parse_qs(p.query or "")
            page = max(1, int((qs.get("page") or ["1"])[0]))
            page_size = max(1, min(500, int((qs.get("page_size") or ["50"])[0])))
            store = PositionStore(DB_PATH)
            total = store.count()
            pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, pages)
            rows = store.list(page_size, (page - 1) * page_size)
            self.send_json({"positions": rows, "page": page, "page_size": page_size, "total": total, "pages": pages,
                            "has_previous": page > 1, "has_next": page < pages})
            return
        if p.path == "/api/orders":
            qs = parse_qs(p.query or "")
            limit = max(1, min(500, int((qs.get("limit") or ["50"])[0])))
            offset = max(0, int((qs.get("offset") or ["0"])[0]))
            store = OrderStore(DB_PATH)
            total = store.count()
            self.send_json({"orders": store.list(limit, offset), "limit": limit, "offset": offset, "total": total,
                            "has_more": offset + limit < total})
            return
        if p.path == "/api/lifecycle":
            self.send_json(RUNTIME.lifecycle_state)
            return
        if p.path == "/api/management":
            self.send_json(RUNTIME.management_state)
            return
        if p.path == "/api/performance":
            payload = _RESPONSE_CACHE.get_or_create(
                "performance_snapshot", 3.0,
                lambda: {"summary": RUNTIME.performance.summary(), "agents": RUNTIME.performance.agent_summary(),
                         "calibration": RUNTIME.performance.calibration(), "recent": RUNTIME.performance.recent(30)},
                stale_if_error=True, stale_ttl=15.0, wait_timeout=2.0, ttl_jitter=0.1,
            )
            self.send_json(payload)
            return


        if p.path == "/api/mt5/bridge":
            try:
                if RUNTIME.feed is None or not RUNTIME.mt5_connected:
                    RUNTIME._connect_mt5()
                if RUNTIME.feed is None:
                    self.send_json({"ok": False, "error": "MT5 feed not connected"}, 400); return
                bridge = RUNTIME.feed.get_bridge()
                self.send_json(bridge.summary()); return
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400); return
        if p.path == "/api/mt5/account":
            try:
                if not RUNTIME._connect_mt5():
                    self.send_json({"error": RUNTIME.connection_detail}, 400); return
                self.send_json(RUNTIME.feed.get_bridge().account_info()); return
            except Exception as exc:
                self.send_json({"error": str(exc)}, 400); return
        if p.path == "/api/mt5/terminal":
            try:
                if not RUNTIME._connect_mt5():
                    self.send_json({"error": RUNTIME.connection_detail}, 400); return
                self.send_json({
                    "terminal": RUNTIME.feed.get_bridge().terminal_info(),
                    "version": RUNTIME.feed.get_bridge().version(),
                    "capabilities": RUNTIME.feed.get_bridge().capabilities(),
                }); return
            except Exception as exc:
                self.send_json({"error": str(exc)}, 400); return
        if p.path == "/api/mt5/tick":
            try:
                if not RUNTIME._connect_mt5():
                    self.send_json({"error": RUNTIME.connection_detail}, 400); return
                qs = parse_qs(p.query)
                sym = (qs.get("symbol") or [RUNTIME.config.symbol])[0]
                self.send_json(RUNTIME.feed.get_bridge().tick(sym)); return
            except Exception as exc:
                self.send_json({"error": str(exc)}, 400); return
        if p.path == "/api/mt5/rates":
            try:
                if not RUNTIME._connect_mt5() or RUNTIME.feed is None:
                    detail = getattr(RUNTIME, "connection_detail", None) or "MT5 not connected — start MetaTrader 5, log in, then retry"
                    self.send_json({"error": detail, "bars": [], "symbol": "", "timeframe": ""}, 503); return
                qs = parse_qs(p.query)
                sym = (qs.get("symbol") or [RUNTIME.config.symbol])[0]
                tf = (qs.get("timeframe") or ["M5"])[0]
                count = int((qs.get("count") or ["300"])[0])
                bridge = RUNTIME.feed.get_bridge()
                if bridge is None:
                    self.send_json({"error": "MT5 bridge not attached", "bars": []}, 503); return
                bars = bridge.rates(sym, tf, count)
                # Include both the UI-requested alias and resolved broker name so the chart
                # can diagnose mismatches without ever rendering another symbol's candles.
                try:
                    broker_symbol = bridge.resolve(sym)
                except Exception:
                    broker_symbol = sym
                self.send_json({"symbol": sym, "request_symbol": sym, "broker_symbol": broker_symbol,
                                "timeframe": str(tf).upper(), "bars": bars or []}); return
            except Exception as e:
                self.send_json({"error": str(e), "bars": []}, 400); return
        if p.path == "/api/mt5/positions":
            try:
                if not RUNTIME._connect_mt5():
                    self.send_json({"error": RUNTIME.connection_detail}, 400); return
                qs = parse_qs(p.query)
                sym = (qs.get("symbol") or [None])[0]
                magic = qs.get("magic")
                magic_i = int(magic[0]) if magic else RUNTIME.config.magic
                self.send_json({"positions": RUNTIME.feed.get_bridge().positions(sym, magic=magic_i)}); return
            except Exception as exc:
                self.send_json({"error": str(exc)}, 400); return
        if p.path == "/api/mt5/orders":
            try:
                if not RUNTIME._connect_mt5():
                    self.send_json({"error": RUNTIME.connection_detail}, 400); return
                qs = parse_qs(p.query)
                sym = (qs.get("symbol") or [None])[0]
                self.send_json({"orders": RUNTIME.feed.get_bridge().orders(sym)}); return
            except Exception as exc:
                self.send_json({"error": str(exc)}, 400); return
        if p.path == "/api/mt5/history/deals":
            try:
                if not RUNTIME._connect_mt5():
                    self.send_json({"error": RUNTIME.connection_detail}, 400); return
                qs = parse_qs(p.query)
                days = int((qs.get("days") or ["7"])[0])
                sym = (qs.get("symbol") or [None])[0]
                self.send_json({"deals": RUNTIME.feed.get_bridge().history_deals(days=days, symbol=sym)}); return
            except Exception as exc:
                self.send_json({"error": str(exc)}, 400); return
        if p.path == "/api/mt5/history/orders":
            try:
                if not RUNTIME._connect_mt5():
                    self.send_json({"error": RUNTIME.connection_detail}, 400); return
                qs = parse_qs(p.query)
                days = int((qs.get("days") or ["7"])[0])
                self.send_json({"orders": RUNTIME.feed.get_bridge().history_orders(days=days)}); return
            except Exception as exc:
                self.send_json({"error": str(exc)}, 400); return
        if p.path == "/api/mt5/symbol":
            try:
                if not RUNTIME._connect_mt5():
                    self.send_json({"error": RUNTIME.connection_detail}, 400); return
                qs = parse_qs(p.query)
                sym = (qs.get("symbol") or [RUNTIME.config.symbol])[0]
                self.send_json(RUNTIME.feed.get_bridge().symbol_info(sym)); return
            except Exception as exc:
                self.send_json({"error": str(exc)}, 400); return
        if p.path == "/api/mt5/symbols":
            try:
                if not RUNTIME._connect_mt5():
                    self.send_json({"error": RUNTIME.connection_detail}, 400); return
                qs = parse_qs(p.query)
                group = (qs.get("group") or [None])[0]
                limit = int((qs.get("limit") or ["500"])[0])
                self.send_json({"total": RUNTIME.feed.get_bridge().symbols_total(), "symbols": RUNTIME.feed.get_bridge().symbols_get(group, limit)}); return
            except Exception as exc:
                self.send_json({"error": str(exc)}, 400); return
        if p.path == "/api/mt5/book":
            try:
                if not RUNTIME._connect_mt5():
                    self.send_json({"error": RUNTIME.connection_detail}, 400); return
                qs = parse_qs(p.query)
                sym = (qs.get("symbol") or [RUNTIME.config.symbol])[0]
                self.send_json(RUNTIME.feed.get_bridge().market_book(sym)); return
            except Exception as exc:
                self.send_json({"error": str(exc)}, 400); return
        if p.path == "/api/mt5/margin":
            try:
                if not RUNTIME._connect_mt5():
                    self.send_json({"error": RUNTIME.connection_detail}, 400); return
                qs = parse_qs(p.query)
                sym = (qs.get("symbol") or [RUNTIME.config.symbol])[0]
                side = (qs.get("side") or ["BUY"])[0]
                volume = float((qs.get("volume") or ["0.01"])[0])
                margin = RUNTIME.feed.get_bridge().order_calc_margin(sym, side, volume)
                self.send_json({"symbol": sym, "side": side, "volume": volume, "margin": margin}); return
            except Exception as exc:
                self.send_json({"error": str(exc)}, 400); return

        if p.path == "/api/mt5":
            st = {}
            try:
                if RUNTIME.feed is not None:
                    st = RUNTIME.feed.connection_status()
                else:
                    st = {"connected": False, "last_error": "Feed not initialized"}
            except Exception as exc:
                st = {"connected": False, "last_error": str(exc)}
            self.send_json({
                "mt5_connected": RUNTIME.mt5_connected,
                "demo_mode": RUNTIME.demo_mode,
                "connection_detail": RUNTIME.connection_detail,
                "reconnect_attempts": RUNTIME.reconnect_attempts,
                "status": st,
            })
            return
        if p.path == "/api/mt5/reconnect":
            try:
                ok = RUNTIME._connect_mt5()
                self.send_json({"ok": ok, "detail": RUNTIME.connection_detail, "mt5_connected": RUNTIME.mt5_connected})
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
            return
        if p.path == "/api/scouts/fleets":
            try:
                from trading_team.market.specialized_scouts import fleet_summary
                self.send_json({
                    "fleets": fleet_summary(),
                    "note": (
                        "Asset-class specialized scouts detect setups and notify the specialist mesh. "
                        "They do not authorize trades by themselves."
                    ),
                })
            except Exception as exc:
                self.send_json({"error": str(exc)}, 400)
            return
        if p.path == "/api/universe/results":
            values = parse_qs(p.query)
            try:
                page = max(1, int((values.get("page") or ["1"])[0]))
                page_size = max(1, min(500, int((values.get("page_size") or values.get("limit") or ["100"])[0])))
                cache_key = f"universe_results:{page}:{page_size}"
                result_page = _RESPONSE_CACHE.get_or_create(
                    cache_key, 1.0, lambda: RUNTIME.scan_queue.results_page(page=page, page_size=page_size),
                    stale_if_error=False, wait_timeout=4.0, ttl_jitter=0.1,
                )
                self.send_json({**result_page, "status": "ok", "note": "Durable scan results are paginated; each saved result remains research-only until all standard execution and risk gates pass."})
            except (TypeError, ValueError, OverflowError) as exc:
                self.send_json({"status": "error", "error": f"Invalid pagination parameters: {exc}"}, 400)
            except Exception as exc:
                self.send_json({"status": "error", "error": f"Could not read scan results: {exc}"}, 500)
            return
        if p.path == "/api/universe/detail":
            values = parse_qs(p.query)
            requested = str((values.get("symbol") or [""])[0]).strip().upper()
            detail = RUNTIME._universe_results.get(requested)
            if detail is None:
                detail = next((row for row in RUNTIME._universe_results.values()
                               if str(row.get("broker_symbol", "")).upper() == requested
                               or str(row.get("symbol", "")).upper() == requested), None)
            if detail is None:
                try:
                    detail = RUNTIME.scan_queue.result(requested)
                except Exception:
                    detail = None
            if detail is None:
                self.send_json({"error": "No completed analysis exists for this symbol yet; it may still be waiting for its scan turn."}, 404)
            else:
                self.send_json({"symbol": requested, "detail": detail})
            return
        if p.path == "/api/symbols":
            broker = []
            try:
                if RUNTIME.feed is not None:
                    broker = list_broker_symbols(RUNTIME.feed.mt5, 400)
            except Exception:
                broker = []
            self.send_json({
                "catalog": catalog_list(),
                "watchlist": list(RUNTIME.watchlist),
                "active": RUNTIME.config.symbol,
                "quotes": RUNTIME.watchlist_quotes,
                "broker_symbols": broker,
                "mt5_connected": RUNTIME.mt5_connected,
                "connection_detail": RUNTIME.connection_detail,
            })
            return
        if p.path == "/api/settings":
            self.send_json(RUNTIME._settings_public())
            return
        if p.path == "/api/quantlib/status":
            try:
                from trading_team.research.quant_lab.quantlib_adapter import quantlib_status
                self.send_json(quantlib_status())
            except Exception as exc:
                self.send_json({"available": False, "installed": False,
                                "code": "QUANTLIB_STATUS_ERROR", "error": str(exc)}, 500)
            return



        if p.path in ("/api/macro", "/api/macro/analyze"):
            try:
                body = self.read_json() if self.command == "POST" else {}
                if not isinstance(body, dict):
                    body = {}
                from trading_team.research.macro_geo import combined_macro_geo_analysis
                symbol = str(body.get("symbol") or RUNTIME.config.symbol)
                closes = body.get("closes")
                if not closes:
                    series = getattr(RUNTIME, "candle_series", {}) or {}
                    bars = series.get("M5") or series.get("M15") or series.get("H1") or []
                    closes = []
                    for b in bars:
                        if isinstance(b, dict):
                            closes.append(float(b.get("close") or b.get("c") or 0))
                        else:
                            closes.append(float(getattr(b, "close", 0) or 0))
                    closes = [x for x in closes if x > 0]
                if not closes and RUNTIME.last and isinstance(RUNTIME.last, dict):
                    # try history field
                    hist = RUNTIME.last.get("history") or []
                    for pt in hist:
                        if isinstance(pt, (int, float)):
                            closes.append(float(pt))
                        elif isinstance(pt, dict) and pt.get("close"):
                            closes.append(float(pt["close"]))
                try_fetch = bool(body.get("try_fetch", True))
                result = combined_macro_geo_analysis(
                    symbol=symbol,
                    closes=closes,
                    headlines=body.get("headlines") if isinstance(body.get("headlines"), list) else None,
                    try_fetch=try_fetch,
                )
                self.send_json(result)
            except Exception as exc:
                RUNTIME.error(f"macro analyze failed: {exc}", traceback.format_exc())
                self.send_json({"error": str(exc)}, 400)
            return

        if p.path == "/api/ice/evaluate":
            try:
                body = self.read_json() if self.command == "POST" else {}
                if not isinstance(body, dict):
                    body = {}
                from trading_team.strategies.ice_grid.bridge import IceGridBridge, GRID_TYPE_NAMES, native_available
                symbol = str(body.get("symbol") or RUNTIME.config.symbol).upper()
                timeframe = str(body.get("timeframe") or "M5").upper()
                grid_profile = str(body.get("grid") or "SmartRegime")
                if grid_profile not in GRID_TYPE_NAMES:
                    grid_profile = "ATRAdaptive"
                bars = body.get("bars")
                data_source = "provided_bars" if bars else "unavailable"
                if not bars and RUNTIME.feed is not None and RUNTIME.mt5_connected:
                    try:
                        from trading_team.data.mtf import MultiTimeframeFeed
                        snap = MultiTimeframeFeed(RUNTIME.feed).snapshot(symbol, (timeframe,), 400)
                        bars = list((getattr(snap, "candles", {}) or {}).get(timeframe) or [])
                        if len(bars) >= 30:
                            data_source = "mt5"
                        else:
                            bars = []
                    except Exception as feed_exc:
                        RUNTIME.error(f"ICE bars {symbol}/{timeframe}: {feed_exc}")
                if not bars and symbol == str(getattr(RUNTIME.config, "symbol", "")).upper():
                    series = getattr(RUNTIME, "candle_series", {}) or {}
                    candidate = series.get(timeframe) or []
                    if len(candidate) >= 30:
                        bars = candidate
                        data_source = "cached_series"
                bridge = IceGridBridge(
                    ice_config={"equity": float(body.get("equity") or 10000), "atr_risk_frac": float(body.get("risk_frac") or 0.01)},
                    default_grid=grid_profile,
                )
                if bars and len(bars) >= 30:
                    result = bridge.evaluate_bars(bars)
                else:
                    reason = ("No verified candle history was returned by MT5 for this request. "
                              "Confirm the symbol exists on this broker, select it in Market Watch, "
                              "and increase MT5 'Max bars in chart' if necessary.")
                    result = {"error": f"ICE / Grid requires at least 30 real {timeframe} candles for {symbol}. {reason}",
                              "code": "MARKET_HISTORY_UNAVAILABLE", "backend": bridge.backend,
                              "symbol": symbol, "timeframe": timeframe,
                              "mt5_connected": bool(RUNTIME.mt5_connected),
                              "connection_detail": str(RUNTIME.connection_detail)}
                    self.send_json(result, 503)
                    return
                result["symbol"] = symbol
                result["timeframe"] = timeframe
                result["data_source"] = data_source
                result["native"] = native_available()
                result["grids"] = GRID_TYPE_NAMES
                self.send_json(result)
            except Exception as exc:
                RUNTIME.error(f"ICE evaluate failed: {exc}", traceback.format_exc())
                self.send_json({"error": str(exc)}, 400)
            return

        if p.path == "/api/quant/lab":
            try:
                body = self.read_json() if self.command == "POST" else {}
                if not isinstance(body, dict):
                    body = {}
                candles = None
                closes = body.get("closes")
                symbol = str(body.get("symbol") or RUNTIME.config.symbol).upper()
                tf = str(body.get("timeframe") or "M5").upper()
                requested_tf = tf
                active_symbol = str(getattr(RUNTIME.config, "symbol", "") or "").upper()
                data_source = "provided_closes" if closes is not None else "unavailable"
                stages = body.get("stages")
                if isinstance(stages, str):
                    stages = [x.strip() for x in stages.split(",") if x.strip()]
                if closes is None:
                    candles = []
                    bars_n = max(30, min(5000, int(body.get("bars") or RUNTIME.config.lookback or 400)))
                    # Only reuse the in-memory snapshot when both symbol and timeframe match.
                    snap = None
                    if symbol == active_symbol and RUNTIME.last and isinstance(RUNTIME.last, dict):
                        snap = RUNTIME.last.get("snapshot")
                    if snap is not None and getattr(snap, "candles", None):
                        exact = list(snap.candles.get(requested_tf) or [])
                        if len(exact) >= 30:
                            candles, data_source = exact, "runtime_snapshot"
                    # Prefer a direct broker pull for the exact requested symbol/timeframe.
                    if not candles and RUNTIME.feed is not None and RUNTIME.mt5_connected:
                        try:
                            from trading_team.data.mtf import MultiTimeframeFeed
                            s2 = MultiTimeframeFeed(RUNTIME.feed).snapshot(symbol, (requested_tf,), bars_n)
                            exact = list((getattr(s2, "candles", {}) or {}).get(requested_tf) or [])
                            if len(exact) >= 30:
                                candles, data_source = exact, "mt5"
                        except Exception as feed_exc:
                            RUNTIME.error(f"quant lab feed {symbol}/{requested_tf}: {feed_exc}")
                    # Cached series have no per-symbol key, so use only for the active symbol and exact TF.
                    if not candles and symbol == active_symbol:
                        series = getattr(RUNTIME, "candle_series", {}) or {}
                        exact = list(series.get(requested_tf) or [])
                        if len(exact) >= 30:
                            candles, data_source = exact, "cached_series"
                    # Never synthesize a price path and present it as a usable market
                    # analytics response. Quant/ICE are data-dependent tools; an explicit
                    # failure is safer and more actionable than a convincing fake chart.
                    if not candles:
                        self.send_json({
                            "error": f"Quant analytics requires at least 30 real candles for {symbol}/{requested_tf}; MT5 returned no verified history.",
                            "code": "MARKET_HISTORY_UNAVAILABLE",
                            "symbol": symbol,
                            "timeframe": requested_tf,
                            "requested_bars": bars_n,
                            "mt5_connected": bool(RUNTIME.mt5_connected),
                            "connection_detail": str(RUNTIME.connection_detail),
                            "data_source": "unavailable",
                            "help": [
                                "Confirm MT5 is connected and logged into the intended broker account.",
                                "Search/select the exact broker-listed symbol in Market Watch.",
                                "Open a chart for this symbol/timeframe and increase MT5 Tools > Options > Charts > Max bars in chart.",
                                "Wait for the terminal to download history, then retry the analysis."
                            ]
                        }, 503)
                        return
                multi = body.get("multi_returns") if isinstance(body.get("multi_returns"), dict) else None
                from trading_team.research.quant_lab.service import run_quant_lab
                result = run_quant_lab(
                    candles=candles,
                    closes=closes,
                    multi_returns=multi,
                    option=body.get("option") if isinstance(body.get("option"), dict) else None,
                    stages=stages,
                    timeframe=requested_tf,
                )
                result["symbol"] = symbol
                result["timeframe"] = requested_tf
                result["data_source"] = data_source
                self.send_json(result)
            except Exception as exc:
                RUNTIME.error(f"Quant lab failed: {exc}", traceback.format_exc())
                self.send_json({"error": str(exc)}, 400)
            return

        if p.path == "/api/research":
            self.send_json(RUNTIME.research_state)
            return
        if p.path == "/api/research/ask":
            try:
                body = self.read_json() if self.command == "POST" else {}
                answer=RUNTIME.copilot.ask(RUNTIME.last["snapshot"],str(body.get("question","")),RUNTIME.research_memory.recent(20)) if RUNTIME.last else "No market snapshot is available yet."
                self.send_json({"answer":answer}); return
            except Exception as exc:
                RUNTIME.error(f"Research copilot failed: {exc}",traceback.format_exc()); self.send_json({"error":str(exc)},400); return
        
        if p.path == "/api/backtest/strategy":
            try:
                from trading_team.backtest.engine import run_strategy, run_all_strategies
                from trading_team.backtest.io import resolve_backtest_candles, resolve_backtest_ticks
                body = self.read_json() if self.command == "POST" else {}
                csv_path = body.get("csv")
                strategy_id = body.get("strategy_id")
                use_all = bool(body.get("all"))
                symbol = str(body.get("symbol") or RUNTIME.config.symbol or "XAUUSD").upper()
                timeframe = str(body.get("timeframe") or "M5").upper()
                if timeframe != "M5":
                    raise ValueError("Strategy backtests require M5 base candles; M15/H1 context is aggregated internally from those M5 bars.")
                bars = int(body.get("bars") or 2000)
                candles, data_source = resolve_backtest_candles(
                    symbol=symbol, timeframe=timeframe, bars=bars, csv_path=csv_path,
                    feed=RUNTIME.feed if RUNTIME.mt5_connected else None,
                )
                risk = float(body.get("risk_fraction") or 0.005)
                equity = float(body.get("equity") or 10000)
                execution_model = str(body.get("execution_model") or "ohlc").lower()
                if execution_model not in {"ohlc", "ticks"}:
                    raise ValueError("execution_model must be 'ohlc' or 'ticks'")
                tick_quotes = None
                if execution_model == "ticks":
                    if body.get("spread_price") not in (None, ""):
                        raise ValueError("Tick execution uses historical bid/ask spread; leave the spread field blank")
                    tick_quotes, tick_source = resolve_backtest_ticks(
                        candles, symbol=symbol, csv_path=body.get("ticks_csv"),
                        feed=RUNTIME.feed if RUNTIME.mt5_connected else None,
                        max_ticks=int(body.get("max_ticks") or 2_000_000),
                    )
                    data_source = f"{data_source} + {tick_source}"
                cost_kwargs = {
                    "spread_price": float(body["spread_price"]) if body.get("spread_price") not in (None, "") else None,
                    "slippage_price": float(body["slippage_price"]) if body.get("slippage_price") not in (None, "") else None,
                    "commission_bps": float(body.get("commission_bps") or 0.0),
                    "tick_quotes": tick_quotes,
                }
                if use_all:
                    results = run_all_strategies(candles, symbol=symbol, initial_equity=equity, risk_fraction=risk, **cost_kwargs)
                    payload = {
                        "data_source": data_source,
                        "bars": len(candles),
                        "results": {sid: {k: v for k, v in m.to_dict().items() if k not in ("equity_curve", "trade_log")} for sid, m in results.items()},
                    }
                else:
                    m = run_strategy(candles, strategy_id=strategy_id, symbol=symbol, initial_equity=equity, risk_fraction=risk, **cost_kwargs)
                    payload = {k: v for k, v in m.to_dict().items() if k != "equity_curve"}
                    payload["data_source"] = data_source
                    payload["bars"] = len(candles)
                    if body.get("include_trades"):
                        payload["trade_log"] = m.trade_log[:200]
                payload["execution_model"] = execution_model
                try:
                    from trading_team.research.experiment_ledger import ExperimentLedger
                    ledger_config = {k: v for k, v in body.items() if k not in {"csv", "ticks_csv", "folder"}}
                    if body.get("csv"):
                        ledger_config["csv"] = body.get("csv")
                    if body.get("ticks_csv"):
                        ledger_config["ticks_csv"] = body.get("ticks_csv")
                    ledger_config["application_version"] = VERSION
                    exp = ExperimentLedger().record(
                        kind="all_strategies" if use_all else "strategy_backtest", symbol=symbol,
                        timeframe=timeframe, strategy_id="all_strategies" if use_all else str(strategy_id or "router"),
                        data_source=data_source, candles=candles, config=ledger_config, result=payload,
                        mode="all" if use_all else "single", execution_model=execution_model,
                        execution_quotes=tick_quotes,
                    )
                    payload["experiment"] = exp
                    if not use_all:
                        try:
                            RUNTIME.strategy_registry.attach_research_experiment(str(strategy_id or "router"), {
                                "experiment_id": exp["experiment_id"], "created_at": exp["created_at"],
                                "validation_tier": exp["validation_tier"], "data_fingerprint": exp["data_fingerprint"],
                                "bars": exp["bars"], "research_only": True, "automatic_promotion": False,
                            })
                        except Exception:
                            pass
                except Exception as ledger_exc:
                    payload["experiment_ledger_warning"] = f"Result completed but could not be saved to experiment ledger: {type(ledger_exc).__name__}: {str(ledger_exc)[:160]}"
                self.send_json(payload); return
            except Exception as exc:
                import traceback
                RUNTIME.error(f"Strategy backtest failed: {exc}", traceback.format_exc())
                self.send_json({"error": str(exc)}, 400); return

        if p.path == "/api/backtest/optimize":
            try:
                from trading_team.backtest.optimizer import grid_search, walk_forward, optimize_all_strategies
                from trading_team.backtest.io import resolve_backtest_candles, resolve_backtest_ticks
                body = self.read_json() if self.command == "POST" else {}
                csv_path = body.get("csv")
                symbol = str(body.get("symbol") or RUNTIME.config.symbol or "XAUUSD").upper()
                timeframe = str(body.get("timeframe") or "M5").upper()
                if timeframe != "M5":
                    raise ValueError("Strategy optimization requires M5 base candles; M15/H1 context is aggregated internally from those M5 bars.")
                bars = int(body.get("bars") or 2000)
                candles, data_source = resolve_backtest_candles(
                    symbol=symbol, timeframe=timeframe, bars=bars, csv_path=csv_path,
                    feed=RUNTIME.feed if RUNTIME.mt5_connected else None,
                )
                mode = str(body.get("mode", "grid") or "grid").strip().lower().replace("_", "").replace("-", "")
                if mode in {"walkforward", "wf", "walkforwardvalidation"}:
                    mode = "walkforward"
                elif mode in {"all", "allstrategies", "screen"}:
                    mode = "all"
                else:
                    mode = "grid"
                strategy_id = body.get("strategy_id", "trend_following")
                objective = body.get("objective", "composite")
                execution_model = str(body.get("execution_model") or "ohlc").lower()
                if execution_model not in {"ohlc", "ticks"}:
                    raise ValueError("execution_model must be 'ohlc' or 'ticks'")
                tick_quotes = None
                if execution_model == "ticks":
                    if body.get("spread_price") not in (None, ""):
                        raise ValueError("Tick execution uses historical bid/ask spread; leave the spread field blank")
                    tick_quotes, tick_source = resolve_backtest_ticks(
                        candles, symbol=symbol, csv_path=body.get("ticks_csv"),
                        feed=RUNTIME.feed if RUNTIME.mt5_connected else None,
                        max_ticks=int(body.get("max_ticks") or 2_000_000),
                    )
                    data_source = f"{data_source} + {tick_source}"
                cost_kwargs = {
                    "spread_price": float(body["spread_price"]) if body.get("spread_price") not in (None, "") else None,
                    "slippage_price": float(body["slippage_price"]) if body.get("slippage_price") not in (None, "") else None,
                    "commission_bps": float(body.get("commission_bps") or 0.0),
                    "tick_quotes": tick_quotes,
                }
                if mode == "all":
                    payload = optimize_all_strategies(candles, objective=objective, n_folds=int(body.get("folds") or 3),
                                                       symbol=symbol, initial_equity=float(body.get("equity") or 10000), **cost_kwargs)
                elif mode == "walkforward":
                    payload = walk_forward(candles, strategy_id=strategy_id, objective=objective,
                                           n_folds=int(body.get("folds") or 4),
                                           is_ratio=float(body.get("is_ratio") or 0.7),
                                           symbol=symbol,
                                           initial_equity=float(body.get("equity") or 10000), **cost_kwargs).to_dict()
                else:
                    payload = grid_search(candles, strategy_id=strategy_id, objective=objective,
                                          max_trials=int(body.get("max_trials") or 40), symbol=symbol,
                                          initial_equity=float(body.get("equity") or 10000), **cost_kwargs).to_dict()
                if isinstance(payload, dict):
                    payload.setdefault("data_source", data_source)
                    payload.setdefault("bars", len(candles))
                    payload.setdefault("execution_model", execution_model)
                    try:
                        from trading_team.research.experiment_ledger import ExperimentLedger
                        ledger_config = {k: v for k, v in body.items() if k not in {"csv", "ticks_csv", "folder"}}
                        if body.get("csv"):
                            ledger_config["csv"] = body.get("csv")
                        if body.get("ticks_csv"):
                            ledger_config["ticks_csv"] = body.get("ticks_csv")
                        ledger_config["application_version"] = VERSION
                        exp = ExperimentLedger().record(
                            kind="strategy_optimization", symbol=symbol, timeframe=timeframe,
                            strategy_id="all_strategies" if mode == "all" else str(strategy_id or "router"),
                            data_source=data_source, candles=candles, config=ledger_config, result=payload,
                            mode=mode, execution_model=execution_model, execution_quotes=tick_quotes,
                        )
                        payload["experiment"] = exp
                        if mode != "all":
                            try:
                                RUNTIME.strategy_registry.attach_research_experiment(str(strategy_id or "router"), {
                                    "experiment_id": exp["experiment_id"], "created_at": exp["created_at"],
                                    "validation_tier": exp["validation_tier"], "data_fingerprint": exp["data_fingerprint"],
                                    "bars": exp["bars"], "research_only": True, "automatic_promotion": False,
                                })
                            except Exception:
                                pass
                    except Exception as ledger_exc:
                        payload["experiment_ledger_warning"] = f"Result completed but could not be saved to experiment ledger: {type(ledger_exc).__name__}: {str(ledger_exc)[:160]}"
                self.send_json(payload); return
            except Exception as exc:
                import traceback
                RUNTIME.error(f"Optimize failed: {exc}", traceback.format_exc())
                self.send_json({"error": str(exc)}, 400); return

        if p.path == "/api/backtest/cross-market":
            try:
                body = self.read_json() if self.command == "POST" else {}
                folder=Path(str(body.get("folder", PROJECT / "data" / "backtest")))
                result=run_directory(folder, float(body.get("initial_equity",10000)))
                card=make_run_card(symbol="MULTI",timeframes=("M5","M15","H1"),data_source="CSV-CROSS-MARKET",strategy_id=RUNTIME.strategy_id,metrics=result["aggregate"],evidence={"markets":result["markets"]},config={"folder":str(folder)})
                save_run_card(card)
                RUNTIME.last_run_card=__import__("dataclasses").asdict(card)
                self.send_json(result); return
            except Exception as exc:
                RUNTIME.error(f"Cross-market backtest failed: {exc}",traceback.format_exc()); self.send_json({"error":str(exc)},400); return
        if p.path == "/api/memory":
            self.send_json({"items": RUNTIME.research_memory.recent(100)})
            return
        if p.path == "/api/research/experiments":
            try:
                from trading_team.research.experiment_ledger import ExperimentLedger
                qs = parse_qs(p.query or "")
                limit = max(1, min(200, int((qs.get("limit") or [50])[0])))
                offset = max(0, min(1_000_000_000, int((qs.get("offset") or [0])[0])))
                symbol = (qs.get("symbol") or [None])[0]
                strategy_id = (qs.get("strategy_id") or [None])[0]
                ledger = ExperimentLedger()
                items = ledger.list(limit=limit, offset=offset, symbol=symbol, strategy_id=strategy_id)
                total = ledger.count(symbol=symbol, strategy_id=strategy_id)
                self.send_json({"items": items, "count": len(items), "total": total, "limit": limit,
                                "offset": offset, "has_previous": offset > 0,
                                "has_next": offset + len(items) < total, "research_only": True})
            except Exception as exc:
                self.send_json({"error": f"Research experiment ledger unavailable: {type(exc).__name__}: {str(exc)[:200]}"}, 500)
            return
        if p.path.startswith("/api/research/experiments/"):
            try:
                from trading_team.research.experiment_ledger import ExperimentLedger
                experiment_id = p.path.rsplit("/", 1)[-1]
                item = ExperimentLedger().get(experiment_id)
                if item is None:
                    self.send_json({"error": "Experiment not found", "experiment_id": experiment_id}, 404)
                else:
                    self.send_json({"item": item, "research_only": True})
            except Exception as exc:
                self.send_json({"error": f"Research experiment detail unavailable: {type(exc).__name__}: {str(exc)[:200]}"}, 500)
            return
        if p.path == "/api/run-cards":
            self.send_json({"items": list_run_cards(limit=100)})
            return
        if p.path == "/api/shadow":
            self.send_json(RUNTIME.shadow_state)
            return
        if p.path == "/api/strategies":
            self.send_json({"items": RUNTIME.strategy_registry.list(100)})
            return
        if p.path.startswith("/static/"):
            rel = p.path.split("/static/", 1)[1]
            f = STATIC / rel
            if f.exists() and f.is_file():
                c = "application/octet-stream"
                if f.suffix == ".css": c = "text/css; charset=utf-8"
                elif f.suffix == ".js": c = "application/javascript; charset=utf-8"
                elif f.suffix == ".svg": c = "image/svg+xml"
                self.send_bytes(f.read_bytes(), c)
                return
        # Helpful 404 for MT5 bridge callers (wrong path / trailing slash / typo)
        if p.path.startswith("/api/mt5"):
            self.send_json({
                "error": "not found",
                "path": p.path,
                "hint": "Use one of the documented MT5 bridge endpoints",
                "endpoints": [
                    "/api/mt5",
                    "/api/mt5/bridge",
                    "/api/mt5/account",
                    "/api/mt5/terminal",
                    "/api/mt5/tick?symbol=",
                    "/api/mt5/rates?symbol=&timeframe=&count=",
                    "/api/mt5/positions",
                    "/api/mt5/orders",
                    "/api/mt5/history/deals?days=",
                    "/api/mt5/history/orders?days=",
                    "/api/mt5/symbol?symbol=",
                    "/api/mt5/symbols",
                    "/api/mt5/book?symbol=",
                    "/api/mt5/margin?symbol=&side=&volume=",
                    "/api/mt5/reconnect",
                ],
            }, 404)
            return
        self.send_json({"error": "not found", "path": p.path}, 404)

    def do_POST(self):
        p = urlparse(self.path)
        p = p._replace(path=(p.path.rstrip("/") or "/"))
        body = {}
        if self.headers.get("Content-Length"):
            try:
                raw_body = self.rfile.read(int(self.headers["Content-Length"])) or b"{}"
                body = json.loads(raw_body)
            except Exception:
                body = {}
        # Preserve the parsed object for routes that delegate to do_GET below.
        self._parsed_json_body = body if isinstance(body, dict) else {}
        self._parsed_json_body_available = True
        if p.path == "/api/news/refresh":
            provider = RUNTIME.news_provider
            if not provider.enabled:
                self.send_json({"ok": False, "error": "News ingestion is disabled by NEWS_RSS_ENABLED=0.", "code": "NEWS_RSS_DISABLED", "status": provider.status()}, 503)
                return
            started = provider.refresh_async(force=True)
            if provider.last_items:
                RUNTIME.news_items = provider.last_items
            self.send_json({"ok": True, "started": bool(started), "refreshing": provider.refreshing,
                            "message": "News refresh started in the background." if started else "A refresh is already running.",
                            "status": provider.status()}, 202 if started else 200)
            return
        if p.path == "/api/trade-learning/sync":
            try:
                if not RUNTIME._connect_mt5():
                    self.send_json({"ok": False, "error": "A connected MT5 terminal is required to import trade history.",
                                    "code": "MT5_UNAVAILABLE", "detail": str(RUNTIME.connection_detail)}, 503)
                    return
                from trading_team.research.trade_history_learning import TradeHistoryLearningStore
                bridge = RUNTIME.feed.get_bridge()
                account = bridge.account_info()
                try:
                    days = max(1, min(3650, int(body.get("days") or 3650)))
                except (TypeError, ValueError):
                    days = 3650
                deals = bridge.history_deals(days=days)
                open_positions = bridge.positions()
                open_ids = []
                for position in open_positions:
                    identifier = position.get("identifier") or position.get("ticket")
                    if identifier:
                        open_ids.append(identifier)
                store = TradeHistoryLearningStore(DB_PATH)
                result = store.sync(deals, account_login=account.get("login"), account_server=account.get("server"),
                                    requested_days=days, open_position_ids=open_ids)
                try:
                    from trading_team.research.quant_context import clear_quant_context_cache
                    clear_quant_context_cache()
                except Exception:
                    pass
                result["summary"] = store.summary(days=days, limit=50, offset=0)
                result["note"] = (result.get("note") or "") + " Research-only import; no orders were sent."
                self.send_json(result)
            except Exception as exc:
                try: RUNTIME.error(f"Trade history learning sync failed: {exc}", traceback.format_exc())
                except Exception: pass
                self.send_json({"ok": False, "error": f"Trade-history synchronization failed: {type(exc).__name__}: {str(exc)[:200]}",
                                "code": "TRADE_HISTORY_SYNC_FAILED"}, 503)
            return
        # Finance research toolkit routes use only verified MT5 history supplied by the
        # existing bridge. They are research-only and never call order_send.
        finance_public_routes = {
            "/api/finance/company-snapshot", "/api/finance/company-statements",
            "/api/finance/company-news", "/api/finance/dcf",
        }
        finance_market_routes = {
            "/api/finance/indicators", "/api/finance/screen", "/api/finance/statistics",
            "/api/finance/forecast", "/api/finance/strategy-test", "/api/finance/portfolio-simulation",
        }
        finance_routes = finance_public_routes | finance_market_routes
        if p.path in finance_routes:
            from trading_team.research.finance_integration import (
                analyze_indicators, technical_screen_row, statistical_research,
                validate_forecast, run_strategy_backtest, simulate_portfolio, _frame,
                fetch_company_snapshot, fetch_company_statement, fetch_company_headline_sentiment,
                discounted_cash_flow_scenarios,
            )
            # Public-equity research is explicitly requested and independent of the MT5
            # runtime. It never polls in the market hot path or authorizes trading.
            if p.path in finance_public_routes:
                try:
                    ticker = str(body.get("ticker") or "").strip()
                    if p.path == "/api/finance/company-snapshot":
                        result = fetch_company_snapshot(ticker)
                    elif p.path == "/api/finance/company-statements":
                        result = fetch_company_statement(ticker, kind=str(body.get("kind") or "income"),
                                                         frequency=str(body.get("frequency") or "annual"))
                    elif p.path == "/api/finance/company-news":
                        result = fetch_company_headline_sentiment(ticker, limit=int(body.get("limit") or 20))
                    else:
                        flows = body.get("cash_flows")
                        if not isinstance(flows, list):
                            raise ValueError("cash_flows must be a list of annual free-cash-flow projections.")
                        try:
                            discount_rate = float(body.get("discount_rate"))
                            terminal_growth = float(body.get("terminal_growth"))
                            cash_value = float(body.get("cash") or 0)
                            debt_value = float(body.get("debt") or 0)
                            shares_value = float(body.get("shares") or 1)
                        except (TypeError, ValueError) as exc:
                            raise ValueError("discount_rate and terminal_growth must be finite numeric assumptions.") from exc
                        result = discounted_cash_flow_scenarios(
                            cash_flows=flows, discount_rate=discount_rate, terminal_growth=terminal_growth,
                            cash=cash_value, debt=debt_value, shares=shares_value,
                        )
                    self.send_json(result)
                except ValueError as exc:
                    self.send_json({"error": str(exc), "code": "INVALID_FINANCE_REQUEST"}, 400)
                except ImportError as exc:
                    self.send_json({"ok": False, "error": f"Optional public-finance dependency is unavailable: {exc}",
                                    "code": "FINANCE_OPTIONAL_DEPENDENCY_MISSING"}, 503)
                except Exception as exc:
                    RUNTIME.error(f"Public finance route {p.path} failed: {exc}", traceback.format_exc())
                    self.send_json({"ok": False, "error": f"Public finance request failed: {type(exc).__name__}: {str(exc)[:220]}",
                                    "code": "PUBLIC_FINANCE_PROVIDER_FAILED"}, 502)
                return
            if RUNTIME.feed is None or not RUNTIME.mt5_connected:
                self.send_json({"ok": False, "error": "Finance research requires a connected MT5 terminal and verified broker history. No synthetic candles are used.",
                                "code": "MT5_UNAVAILABLE", "mt5_connected": bool(RUNTIME.mt5_connected),
                                "connection_detail": str(RUNTIME.connection_detail)}, 503)
                return
            bridge = RUNTIME.feed.get_bridge()
            def finance_symbol_list(raw, min_count=1, max_count=12):
                if isinstance(raw, str): raw = raw.split(",")
                rows, seen = [], set()
                for item in (raw or []):
                    name = str(item or "").strip().upper()
                    if name and name not in seen:
                        seen.add(name); rows.append(name)
                if not min_count <= len(rows) <= max_count:
                    raise ValueError(f"Enter between {min_count} and {max_count} distinct broker symbols.")
                return rows
            def finance_bars(symbol, timeframe, count):
                # MT5Bridge handles alias resolution, per-terminal IPC locking, retries and history diagnostics.
                return bridge.rates(symbol, timeframe, count)
            try:
                if p.path == "/api/finance/indicators":
                    symbol = str(body.get("symbol") or RUNTIME.config.symbol or "XAUUSD").strip().upper()
                    timeframe = str(body.get("timeframe") or "M5").strip().upper()
                    bars_n = max(80, min(2500, int(body.get("bars") or 500)))
                    rows = finance_bars(symbol, timeframe, bars_n)
                    result = analyze_indicators(rows, symbol=symbol, timeframe=timeframe)
                    self.send_json(result)
                    return
                if p.path == "/api/finance/screen":
                    symbols = finance_symbol_list(body.get("symbols") or [], min_count=1, max_count=15)
                    timeframe = str(body.get("timeframe") or "H1").strip().upper()
                    bars_n = max(80, min(800, int(body.get("bars") or 300)))
                    results, failures = [], {}
                    for symbol in symbols:
                        try:
                            rows = finance_bars(symbol, timeframe, bars_n)
                            results.append(technical_screen_row(rows, symbol=symbol, timeframe=timeframe))
                        except Exception as symbol_exc:
                            failures[symbol] = f"{type(symbol_exc).__name__}: {str(symbol_exc)[:180]}"
                    self.send_json({"timeframe": timeframe, "requested_symbols": symbols, "results": results,
                                    "failures": failures, "scanned": len(results), "failed": len(failures),
                                    "data_source": "verified MT5 OHLCV", "research_only": True,
                                    "note": "Screen labels are descriptive rules, not probability forecasts or order instructions."})
                    return
                if p.path == "/api/finance/statistics":
                    symbols = finance_symbol_list(body.get("symbols") or [], min_count=2, max_count=8)
                    timeframe = str(body.get("timeframe") or "D1").strip().upper()
                    bars_n = max(80, min(2000, int(body.get("bars") or 500)))
                    frames, failures = {}, {}
                    for symbol in symbols:
                        try:
                            frames[symbol] = _frame(finance_bars(symbol, timeframe, bars_n))
                        except Exception as symbol_exc:
                            failures[symbol] = f"{type(symbol_exc).__name__}: {str(symbol_exc)[:180]}"
                    if len(frames) < 2:
                        self.send_json({"error": "Fewer than two instruments returned usable aligned price history.",
                                        "code": "INSUFFICIENT_MULTI_ASSET_HISTORY", "failures": failures}, 422)
                        return
                    result = statistical_research(frames, timeframe=timeframe)
                    result["failures"] = failures
                    self.send_json(result)
                    return
                if p.path == "/api/finance/forecast":
                    symbol = str(body.get("symbol") or RUNTIME.config.symbol or "XAUUSD").strip().upper()
                    timeframe = str(body.get("timeframe") or "D1").strip().upper()
                    model = str(body.get("model") or "ridge").strip().lower()
                    if model not in {"ridge", "forest", "boosting", "svr", "mlp"}:
                        raise ValueError("model must be ridge, forest, boosting, svr or mlp")
                    bars_n = max(160, min(1800, int(body.get("bars") or 800)))
                    train_fraction = max(0.55, min(0.85, float(body.get("train_fraction") or 0.75)))
                    result = validate_forecast(finance_bars(symbol, timeframe, bars_n), symbol=symbol,
                                               timeframe=timeframe, model=model, train_fraction=train_fraction)
                    self.send_json(result)
                    return
                if p.path == "/api/finance/strategy-test":
                    symbol = str(body.get("symbol") or RUNTIME.config.symbol or "XAUUSD").strip().upper()
                    timeframe = str(body.get("timeframe") or "M5").strip().upper()
                    bars_n = max(100, min(5000, int(body.get("bars") or 2000)))
                    strategy_bars = finance_bars(symbol, timeframe, bars_n)
                    result = run_strategy_backtest(strategy_bars, symbol=symbol,
                        timeframe=timeframe, strategy=str(body.get("strategy") or "ma_crossover"),
                        initial_cash=float(body.get("initial_cash") or 10000),
                        commission_bps=float(body.get("commission_bps") if body.get("commission_bps") is not None else 1.0),
                        slippage_bps=float(body.get("slippage_bps") if body.get("slippage_bps") is not None else 1.0),
                        fast=int(body.get("fast") or 20), slow=int(body.get("slow") or 50),
                        rsi_window=int(body.get("rsi_window") or 14), breakout_window=int(body.get("breakout_window") or 20))
                    try:
                        from trading_team.research.experiment_ledger import ExperimentLedger
                        exp = ExperimentLedger().record(
                            kind="finance_strategy_research", symbol=symbol, timeframe=timeframe,
                            strategy_id=str(body.get("strategy") or "ma_crossover"),
                            data_source="verified MT5 OHLCV", candles=strategy_bars,
                            config={**{k: v for k, v in body.items() if k not in {"csv", "path"}}, "application_version": VERSION},
                            result=result, mode="single", execution_model="fractional_units_research",
                        )
                        result["experiment"] = exp
                    except Exception as ledger_exc:
                        result["experiment_ledger_warning"] = f"Result completed but could not be saved to experiment ledger: {type(ledger_exc).__name__}: {str(ledger_exc)[:160]}"
                    self.send_json(result)
                    return
                if p.path == "/api/finance/portfolio-simulation":
                    symbols = finance_symbol_list(body.get("symbols") or [], min_count=1, max_count=8)
                    timeframe = "D1"
                    bars_n = max(80, min(2500, int(body.get("bars") or 500)))
                    price_series, failures = {}, {}
                    for symbol in symbols:
                        try:
                            frame = _frame(finance_bars(symbol, timeframe, bars_n))
                            price_series[symbol] = frame.close
                        except Exception as symbol_exc:
                            failures[symbol] = f"{type(symbol_exc).__name__}: {str(symbol_exc)[:180]}"
                    if not price_series:
                        self.send_json({"error": "No valid daily history could be obtained for simulation.",
                                        "code": "NO_VALID_D1_HISTORY", "failures": failures}, 422)
                        return
                    import pandas as pd
                    prices = pd.concat(price_series, axis=1, join="inner").dropna()
                    result = simulate_portfolio(prices, capital=float(body.get("capital") or 10000),
                        years=float(body.get("years") or 1), paths=int(body.get("paths") or 500),
                        seed=int(body.get("seed") or 17), method=str(body.get("method") or "monte_carlo"),
                        installments=int(body.get("installments") or 12))
                    result["failures"] = failures
                    result["data_source"] = "verified MT5 D1 closes"
                    self.send_json(result)
                    return
            except ValueError as exc:
                self.send_json({"error": str(exc), "code": "INVALID_FINANCE_REQUEST"}, 400)
            except Exception as exc:
                RUNTIME.error(f"Finance toolkit route {p.path} failed: {exc}", traceback.format_exc())
                self.send_json({"error": f"Finance toolkit request failed: {type(exc).__name__}: {str(exc)[:240]}",
                                "code": "FINANCE_TOOLKIT_REQUEST_FAILED"}, 500)
            return

        # Allow POST /api/mt5/reconnect (same as GET)
        if p.path == "/api/mt5/reconnect":
            try:
                ok = RUNTIME._connect_mt5()
                self.send_json({"ok": ok, "detail": RUNTIME.connection_detail, "mt5_connected": RUNTIME.mt5_connected})
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
            return
        if p.path == "/api/mt5/bridge":
            try:
                if RUNTIME.feed is None or not RUNTIME.mt5_connected:
                    RUNTIME._connect_mt5()
                if RUNTIME.feed is None:
                    self.send_json({"ok": False, "error": "MT5 feed not connected"}, 400); return
                bridge = RUNTIME.feed.get_bridge()
                self.send_json(bridge.summary()); return
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400); return
        if p.path == "/api/market/registry/override":
            try:
                requested = str(body.get("broker_symbol") or "").strip()
                if not requested:
                    self.send_json({"error": "broker_symbol is required"}, 400); return
                if RUNTIME.feed is None or not RUNTIME.mt5_connected:
                    self.send_json({"error": "A connected MT5 terminal is required to confirm a broker-symbol mapping."}, 409); return
                # Require exact membership: this endpoint must not turn an alias or
                # ambiguous prefix into an editable contract identity.
                bridge = RUNTIME.feed.get_bridge()
                metadata = bridge.symbol_info(requested)
                exact_name = str(metadata.get("broker_symbol") or metadata.get("name") or "").strip()
                if not exact_name or exact_name.upper() != requested.upper():
                    self.send_json({"error": "Enter the exact broker-listed symbol name, not an alias."}, 400); return
                RUNTIME.instrument_registry.sync([metadata], complete_snapshot=False, source_id="operator_mapping_review")
                desk = str(body.get("market_desk") or "").strip().lower()
                underlying = str(body.get("underlying_market") or "").strip().lower()
                product = str(body.get("product_type") or "").strip().lower()
                reason = str(body.get("reason") or "").strip()
                if underlying == "unknown" or product == "unknown" or not underlying or not product:
                    self.send_json({"error": "An explicit, reviewed underlying market and product type are required; unknown mappings cannot be approved.",
                                    "symbol": exact_name, "instrument": RUNTIME.instrument_registry.get(exact_name)}, 400); return
                confirmation = str(body.get("confirmation") or "")
                if confirmation != f"MAP {exact_name}":
                    self.send_json({"error": f"Exact confirmation required to save this approved mapping: MAP {exact_name}"}, 400); return
                row = RUNTIME.instrument_registry.set_override(exact_name, desk, underlying, product, reason, approved=True)
                RUNTIME.log_event("instrument_mapping_approved", {"symbol": exact_name, "market_desk": desk,
                                    "underlying_market": underlying, "product_type": product, "reason": reason,
                                    "entry_eligible": bool(row.get("entry_eligible"))})
                self.send_json({"status": "mapping_saved", "symbol": exact_name, "instrument": row,
                                "entry_check": RUNTIME.instrument_registry.entry_check(exact_name, "BUY"),
                                "note": "A saved mapping does not bypass quote freshness, live authorization, risk checks or broker preflight."})
            except Exception as exc:
                self.send_json({"error": str(exc)}, 400)
            return
        if p.path == "/api/memory":
            topic=str(body.get("topic","manual"))
            content=str(body.get("content",""))
            if not content.strip(): self.send_json({"error":"content is required"},400); return
            RUNTIME.research_memory.remember(topic,content,"dashboard",float(body.get("importance",0.5)))
            self.send_json({"status":"saved"}); return
        if p.path == "/api/shadow/upload":
            import base64, os
            try:
                name=os.path.basename(str(body.get("name","trades.csv")))
                raw=base64.b64decode(str(body.get("content_base64","")),validate=True)
                up=PROJECT / "data" / "uploads"; up.mkdir(parents=True,exist_ok=True)
                out=up / name; out.write_bytes(raw)
                trades=load_trade_csv(out)
                profile=profile_trades(trades)
                rules=extract_behavior_rules(trades)
                RUNTIME.shadow_state={"profile":profile,"rules":rules,"source":str(out)}
                RUNTIME.research_memory.remember("shadow_account",f"Loaded {len(trades)} trades from {name}","shadow-account",0.8)
                self.send_json(RUNTIME.shadow_state); return
            except Exception as exc:
                RUNTIME.error(f"Shadow upload failed: {exc}", traceback.format_exc())
                self.send_json({"error":str(exc)},400); return
        if p.path == "/api/control":
            action = body.get("action")
            confirmation = str(body.get('confirmation',''))
            try:
                with RUNTIME.lock:
                    if action == "accept_improvement_for_test":
                        RUNTIME.log_event("improvement_test_approved", {"proposal": RUNTIME.research_state.get("improvement")})
                        RUNTIME.research_state["improvement_test_status"]="QUEUED_FOR_BACKTEST_ONLY"
                    elif action == "pause": RUNTIME.paused = True
                    elif action == "resume": RUNTIME.paused = False
                    elif action == "place_pending":
                        if confirmation != f"PLACE PENDING {str(body.get('symbol') or RUNTIME.config.symbol).strip()}":
                            self.send_json({"error": f"Exact confirmation required: PLACE PENDING {str(body.get('symbol') or RUNTIME.config.symbol).strip()}"}, 400); return
                        result = RUNTIME.attempt_live_pending_order(
                            str(body.get("symbol") or RUNTIME.config.symbol),
                            str(body.get("side") or "BUY"),
                            float(body.get("volume") or 0.01),
                            float(body.get("price") or 0),
                            str(body.get("order_kind") or "LIMIT"),
                            float(body.get("sl") or 0),
                            float(body.get("tp") or 0),
                        )
                        code = 200 if result.get("status") == "PLACED" else 400
                        self.send_json(result, code); return
                    elif action == "cancel_pending":
                        if not RUNTIME.live_ready():
                            self.send_json({"error": "Live not armed"}, 400); return
                        adapter = MT5ExecutionAdapter(RUNTIME.feed, DB_PATH, RUNTIME.config.magic, RUNTIME.config.deviation)
                        self.send_json(adapter.cancel_pending(int(body.get("ticket") or 0))); return
                    elif action == "reconnect_mt5":
                        ok = RUNTIME._connect_mt5()
                        RUNTIME.log_event("mt5_reconnect", {"ok": ok, "detail": RUNTIME.connection_detail})
                        self.send_json({"ok": ok, "detail": RUNTIME.connection_detail}); return
                    elif action == "kill_on":
                        RUNTIME.kill_switch = True
                        RUNTIME.live_armed = False
                    elif action == "kill_off":
                        if confirmation != 'DISABLE KILL SWITCH':
                            self.send_json({"error":"Exact confirmation required: DISABLE KILL SWITCH"},400); return
                        RUNTIME.kill_switch = False
                    elif action == "arm_live":
                        if confirmation != 'ARM LIVE TRADING':
                            self.send_json({"error":"Exact confirmation required: ARM LIVE TRADING"},400); return
                        if RUNTIME.config.paper_trading or not RUNTIME.config.live_trading_enabled:
                            self.send_json({"error":"Live mode is not configured. Set TRADING_MODE=live and LIVE_TRADING_ENABLED=true."},400); return
                        if RUNTIME.feed is None or RUNTIME.demo_mode or not RUNTIME.account_live:
                            self.send_json({"error":"Live MT5 feed and successful account synchronization are required before arming."},400); return
                        RUNTIME.live_armed = True
                    elif action == "disarm_live":
                        RUNTIME.live_armed = False
                        RUNTIME.kill_switch = True
                    elif action == "scan": RUNTIME.refresh(force=True)
                    elif action == "clear_errors": RUNTIME.errors = []
                    elif action == "clear_events": RUNTIME.events = []
                    elif action == 'close_position':
                        ticket=int(body.get('ticket',0))
                        if confirmation != f'CLOSE {ticket}':
                            self.send_json({"error":f"Exact confirmation required: CLOSE {ticket}"},400); return
                        if not (RUNTIME.config.live_trading_enabled and not RUNTIME.config.paper_trading and RUNTIME.feed is not None and RUNTIME.account_live and not RUNTIME.demo_mode):
                            self.send_json({"error":"Live position management requires live MT5 configuration and account synchronization."},400); return
                        result=MT5ExecutionAdapter(RUNTIME.feed,DB_PATH,RUNTIME.config.magic,RUNTIME.config.deviation).close_position(ticket)
                        RUNTIME.log_event('position_close', {'ticket':ticket,'result':result})
                    elif action == 'modify_position':
                        ticket=int(body.get('ticket',0)); sl=safe_number(body.get('sl')); tp=safe_number(body.get('tp'))
                        if confirmation != f"MODIFY {ticket}":
                            self.send_json({"error":f"Exact confirmation required: MODIFY {ticket}"},400); return
                        if sl <= 0 or tp <= 0:
                            self.send_json({"error":"SL and TP must both be positive prices."},400); return
                        if not (RUNTIME.config.live_trading_enabled and not RUNTIME.config.paper_trading and RUNTIME.feed is not None and RUNTIME.account_live and not RUNTIME.demo_mode):
                            self.send_json({"error":"Live position management requires live MT5 configuration and account synchronization."},400); return
                        result=MT5ExecutionAdapter(RUNTIME.feed,DB_PATH,RUNTIME.config.magic,RUNTIME.config.deviation).modify_position(ticket, sl, tp)
                        RUNTIME.log_event('position_modify', {'ticket':ticket,'sl':sl,'tp':tp,'result':result})
                    elif action == 'reconcile':
                        if RUNTIME.feed is None or RUNTIME.demo_mode:
                            self.send_json({"error":"MT5 live feed unavailable."},400); return
                        RUNTIME.lifecycle_state = RUNTIME.lifecycle.reconcile(RUNTIME.feed, RUNTIME.config.symbol)
                        RUNTIME.log_event('manual_reconcile', {'alerts':len(RUNTIME.lifecycle_state.get('alerts',[]))})
                    elif action == 'toggle_position_manager':
                        enabled = bool(body.get('enabled', True))
                        RUNTIME.management.cfg = replace(RUNTIME.management.cfg, enabled=enabled)
                        RUNTIME.log_event('position_manager', {'enabled': enabled})
                        RUNTIME._persist_runtime_settings()
                    else:
                        self.send_json({"error": "unknown action"}, 400); return
                RUNTIME.log_event("control", {"action": action})
                self.send_json({"ok": True, "action": action})
            except Exception as exc:
                RUNTIME.error(f'Control action failed: {exc}', traceback.format_exc())
                self.send_json({"error": str(exc)}, 400)
            return
        if p.path == "/api/settings":
            try:
                RUNTIME.update_settings(body if isinstance(body, dict) else {})
                self.send_json({"ok": True, "settings": RUNTIME._settings_public()})
            except Exception as exc:
                RUNTIME.error(f"Settings update failed: {exc}", traceback.format_exc())
                self.send_json({"ok": False, "error": str(exc)}, 400)
            return
        if p.path == "/api/quantlib/option":
            try:
                from trading_team.research.quant_lab.quantlib_adapter import (
                    QuantLibUnavailable, price_option_with_scenarios,
                )
                result = price_option_with_scenarios(body if isinstance(body, dict) else {})
                self.send_json(result)
            except Exception as exc:
                from trading_team.research.quant_lab.quantlib_adapter import QuantLibUnavailable
                if isinstance(exc, QuantLibUnavailable):
                    self.send_json({"error": str(exc), "code": exc.code,
                                    "install_command": ".venv\\Scripts\\python.exe -m pip install -e .[quantlib]"}, 503)
                elif isinstance(exc, ValueError):
                    self.send_json({"error": str(exc), "code": "INVALID_QUANTLIB_REQUEST"}, 400)
                else:
                    RUNTIME.error(f"QuantLib option calculation failed: {exc}", traceback.format_exc())
                    self.send_json({"error": f"QuantLib calculation failed: {type(exc).__name__}: {exc}",
                                    "code": "QUANTLIB_CALCULATION_FAILED"}, 500)
            return

        if p.path == "/api/portfolio/stress":
            try:
                from trading_team.research.portfolio_suite import build_stress_scenarios
                if not RUNTIME._connect_mt5():
                    self.send_json({"error": "A connected MT5 terminal is required for broker-calculated portfolio stress.",
                                    "code": "MT5_UNAVAILABLE"}, 503); return
                bridge = RUNTIME.feed.get_bridge()
                account = bridge.account_info()
                positions = bridge.positions()
                raw_shocks = body.get("shocks_pct", [-10, -5, -2, 2, 5, 10])
                if not isinstance(raw_shocks, list) or not 1 <= len(raw_shocks) <= 12:
                    self.send_json({"error": "shocks_pct must be a list of 1 to 12 percentages.", "code": "INVALID_STRESS_REQUEST"}, 400); return
                def calc_profit(position, target_price):
                    return bridge.order_calc_profit(str(position.get("symbol") or ""), str(position.get("side") or "BUY"),
                                                    safe_number(position.get("volume")), safe_number(position.get("price_current")), target_price)
                result = build_stress_scenarios(positions, account_equity=safe_number(account.get("equity")),
                                                calc_profit=calc_profit, shocks_pct=raw_shocks)
                result["account_currency"] = account.get("currency")
                self.send_json(result)
            except ValueError as exc:
                self.send_json({"error": str(exc), "code": "INVALID_STRESS_REQUEST"}, 400)
            except Exception as exc:
                self.send_json({"error": f"Portfolio stress unavailable: {type(exc).__name__}: {exc}",
                                "code": "PORTFOLIO_STRESS_FAILED"}, 503)
            return
        if p.path == "/api/portfolio/rebalance":
            try:
                from trading_team.research.portfolio_suite import build_rebalance_proposal
                target = body.get("target_weights") or {}
                current = body.get("current_weights") or {}
                if not isinstance(target, dict) or not isinstance(current, dict):
                    self.send_json({"error": "target_weights and current_weights must be symbol-to-weight objects.",
                                    "code": "INVALID_REBALANCE_REQUEST"}, 400); return
                result = build_rebalance_proposal(target, current,
                    capital=safe_number(body.get("capital"), 10000.0),
                    cost_bps_per_side=safe_number(body.get("cost_bps_per_side"), 5.0))
                self.send_json(result)
            except ValueError as exc:
                self.send_json({"error": str(exc), "code": "INVALID_REBALANCE_REQUEST"}, 400)
            except Exception as exc:
                self.send_json({"error": f"Rebalance proposal failed: {type(exc).__name__}: {exc}",
                                "code": "REBALANCE_PROPOSAL_FAILED"}, 500)
            return
        if p.path == "/api/quant/portfolio":
            try:
                import numpy as np
                from trading_team.research.quant_lab.portfolio import portfolio_from_returns
                raw_symbols = body.get("symbols") or []
                if isinstance(raw_symbols, str):
                    raw_symbols = raw_symbols.split(",")
                symbols = []
                for item in raw_symbols:
                    name = str(item or "").strip().upper()
                    if name and name not in symbols:
                        symbols.append(name)
                if not 2 <= len(symbols) <= 12:
                    self.send_json({"error": "Enter between 2 and 12 distinct broker symbols.", "code": "INVALID_PORTFOLIO_SYMBOLS"}, 400); return
                if RUNTIME.feed is None or not RUNTIME.mt5_connected:
                    self.send_json({"error": "Portfolio analysis requires a connected MT5 terminal. No synthetic prices are used.",
                                    "code": "MT5_UNAVAILABLE", "mt5_connected": bool(RUNTIME.mt5_connected),
                                    "connection_detail": str(RUNTIME.connection_detail)}, 503); return
                bars_n = max(60, min(2500, int(body.get("bars") or 400)))
                capital = max(1.0, min(1.0e12, float(body.get("capital") or 10000)))
                rf = max(-0.05, min(0.25, float(body.get("risk_free_rate") if body.get("risk_free_rate") is not None else 0.02)))
                max_weight = max(0.05, min(1.0, float(body.get("max_weight") if body.get("max_weight") is not None else 0.60)))
                transaction_cost_bps = max(0.0, min(500.0, float(body.get("transaction_cost_bps") if body.get("transaction_cost_bps") is not None else 5.0)))
                feed = MultiTimeframeFeed(RUNTIME.feed)
                closes_by_symbol = {}
                failures = {}
                for sym in symbols:
                    try:
                        snap = feed.snapshot(sym, ("D1",), bars_n)
                        rows = list((getattr(snap, "candles", {}) or {}).get("D1") or [])
                        daily = {}
                        for bar in rows:
                            if isinstance(bar, dict):
                                ts = bar.get("timestamp") or bar.get("time") or bar.get("datetime")
                                close = safe_number(bar.get("close", bar.get("c", 0)))
                            else:
                                ts = getattr(bar, "timestamp", None)
                                close = safe_number(getattr(bar, "close", 0))
                            if ts is None or close <= 0 or not np.isfinite(close):
                                continue
                            if hasattr(ts, "date"):
                                day = ts.date().isoformat()
                            else:
                                day = str(ts)[:10]
                            daily[day] = close
                        if len(daily) >= 31:
                            closes_by_symbol[sym] = daily
                        else:
                            failures[sym] = f"Only {len(daily)} valid D1 closes; at least 31 are needed."
                    except Exception as symbol_exc:
                        failures[sym] = str(symbol_exc)[:180]
                usable = list(closes_by_symbol)
                if len(usable) < 2:
                    self.send_json({"error": "At least two symbols need sufficient, verified D1 history.",
                                    "code": "INSUFFICIENT_PORTFOLIO_HISTORY", "failures": failures,
                                    "symbols_loaded": usable}, 503); return
                common_days = sorted(set.intersection(*(set(closes_by_symbol[x]) for x in usable)))
                if len(common_days) < 31:
                    self.send_json({"error": f"Only {len(common_days)} aligned daily observations across the usable symbols; need at least 31.",
                                    "code": "INSUFFICIENT_ALIGNED_HISTORY", "failures": failures,
                                    "symbols_loaded": usable}, 422); return
                log_returns = []
                for sym in usable:
                    values = np.asarray([closes_by_symbol[sym][d] for d in common_days], dtype=float)
                    if np.any(values <= 0) or not np.all(np.isfinite(values)):
                        raise ValueError(f"Invalid daily close series for {sym}")
                    # Simple close-to-close returns match the portfolio risk and
                    # compounding routines; do not label log-return arithmetic as
                    # simple expected returns.
                    log_returns.append(values[1:] / values[:-1] - 1.0)
                matrix = np.column_stack(log_returns)
                result = portfolio_from_returns(
                    matrix, usable, rf=rf, capital=capital,
                    max_weight=max_weight, transaction_cost_bps=transaction_cost_bps,
                )
                result.update({"data_source": "MT5_D1_SIMPLE_RETURNS", "returns_type": "simple", "timeframe": "D1", "aligned_observations": len(common_days),
                               "aligned_returns": int(matrix.shape[0]), "date_start": common_days[0], "date_end": common_days[-1],
                               "requested_symbols": symbols, "missing_symbols": failures,
                               "disclaimer": "Research-only optimizer on historical daily log returns; estimates are not a forecast, not execution instructions, and do not include broker-specific forward costs."})
                self.send_json(result)
            except Exception as exc:
                RUNTIME.error(f"Quant portfolio failed: {exc}", traceback.format_exc())
                self.send_json({"error": f"Portfolio calculation failed: {type(exc).__name__}: {exc}"}, 400)
            return
        # Forward POST to shared GET handlers for backtest / research routes
        if p.path in ("/api/backtest/strategy", "/api/backtest/optimize", "/api/backtest/cross-market", "/api/research/ask", "/api/quant/lab", "/api/ice/evaluate", "/api/macro", "/api/macro/analyze"):
            self.do_GET()
            return
        self.send_json({"error": "not found"}, 404)

    def log_message(self, *_args):
        pass


def serve(host=None, port=None):
    host = host or os.getenv("DASHBOARD_HOST", "127.0.0.1")
    port = int(port or os.getenv("DASHBOARD_PORT", "8105"))
    RUNTIME.start()
    # News refresh has its own bounded scheduler; it continues while trading is paused
    # and shares the provider's single-flight lock with the legacy cycle-triggered refresh.
    try:
        RUNTIME.news_provider.start_scheduler(RUNTIME.news_refresh_seconds, refresh_on_start=not bool(RUNTIME.news_provider.last_fetch_at))
    except Exception as exc:
        RUNTIME.error(f"News scheduler did not start: {type(exc).__name__}: {exc}")
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"AI Trading Team v{VERSION} web control center: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        RUNTIME.running = False
        try:
            RUNTIME.news_provider.stop_scheduler()
        except Exception:
            pass
        server.server_close()


if __name__ == "__main__":
    serve()
