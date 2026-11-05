"""
Live pipeline: real-time ticks → hedge caches → Greeks/VaR → stream bus.

Call `tick_pipeline(symbol, positions=...)` from the orchestrator loop, or
`start_background_pipeline(symbols)` for a daemon thread.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from trading_team.data.realtime import get_feed_hub, make_mt5_tick_source, Tick
from trading_team.monitor.dashboard import publish_hedge, publish_regime
from trading_team.monitor.alerts import get_alert_engine
from trading_team.stream.bus import get_bus


def enrich_metadata_from_feed(meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge live feed prices/returns into snapshot metadata for PortfolioAgent."""
    meta = dict(meta or {})
    hub = get_feed_hub()
    snap = hub.snapshot_for_hedge()
    if snap.get("prices") and not meta.get("prices"):
        meta["prices"] = snap["prices"]
    if snap.get("returns_by_symbol") and not meta.get("returns_by_symbol"):
        meta["returns_by_symbol"] = snap["returns_by_symbol"]
    if snap.get("closes_by_symbol") and not meta.get("closes_by_symbol"):
        meta["closes_by_symbol"] = snap["closes_by_symbol"]
    return meta


def run_hedge_cycle(
    positions: list[dict[str, Any]] | None = None,
    *,
    regime_state: str | None = None,
    hedge_universe: list[str] | None = None,
) -> dict[str, Any]:
    """
    One-shot: pull feed caches → Greeks/beta hedges → publish + VaR.
    Returns the hedge bundle.
    """
    hub = get_feed_hub()
    returns_map = hub.returns_by_symbol()
    prices = hub.prices_map()
    positions = positions or []

    from trading_team.risk.hedging import suggest_hedges_with_greeks
    from trading_team.risk.var_stream import publish_portfolio_var

    bundle = suggest_hedges_with_greeks(
        positions,
        returns_map,
        hedge_candidates=hedge_universe,
        regime_state=regime_state,
        prices=prices,
        prefer_delta_neutral=True,
    )
    publish_hedge({
        "portfolio_greeks": bundle.get("portfolio_greeks") or {},
        "beta_hedges": bundle.get("beta_hedges") or [],
        "delta_hedges": bundle.get("delta_hedges") or [],
        "greeks_summary": bundle.get("greeks_summary") or "",
        "net_exposure": {},
        "regime_state": regime_state,
    })
    # Stream VaR from the same returns window
    if returns_map:
        # Weight by |notional| if positions given
        weights = None
        if positions:
            notionals = {}
            for p in positions:
                s = str(p.get("symbol") or "")
                n = abs(float(p.get("notional") or 0) or 0)
                if s and n:
                    notionals[s] = notionals.get(s, 0.0) + n
            total = sum(notionals.values()) or 1.0
            weights = {s: v / total for s, v in notionals.items()}
        publish_portfolio_var(returns_map, weights=weights)

    # Feed health alerts
    health = hub.publish_health()
    get_alert_engine().on_feed_health(health)
    return bundle


def inject_synthetic_ticks(symbols: list[str], *, n: int = 30, base_prices: dict[str, float] | None = None) -> int:
    """Test helper: push synthetic ticks into the hub."""
    import random
    hub = get_feed_hub()
    bases = base_prices or {s: 100.0 + i for i, s in enumerate(symbols)}
    count = 0
    for _ in range(n):
        for s in symbols:
            px = bases[s] * (1 + random.gauss(0, 0.001))
            bases[s] = px
            hub.on_tick(Tick(symbol=s, bid=px - 0.01, ask=px + 0.01, last=px))
            count += 1
    return count


class BackgroundPipeline:
    """Daemon: poll MT5 (or synthetic), refresh hedges, stream health."""

    def __init__(
        self,
        symbols: list[str],
        *,
        positions_provider: Any = None,
        regime_provider: Any = None,
        interval_s: float = 1.0,
        use_mt5: bool = True,
    ):
        self.symbols = symbols
        self.positions_provider = positions_provider  # callable → list[dict]
        self.regime_provider = regime_provider        # callable → str | None
        self.interval_s = interval_s
        self.use_mt5 = use_mt5
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        hub = get_feed_hub()
        if self.use_mt5:
            hub.set_source(make_mt5_tick_source(self.symbols), poll_interval=self.interval_s)
        hub.start()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="bg-pipeline", daemon=True)
        self._thread.start()
        get_bus().publish("system", {"level": "info", "msg": "BackgroundPipeline started", "symbols": self.symbols})

    def stop(self) -> None:
        self._stop.set()
        get_feed_hub().stop()
        if self._thread:
            self._thread.join(timeout=3.0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                positions = self.positions_provider() if callable(self.positions_provider) else []
                regime = self.regime_provider() if callable(self.regime_provider) else None
                run_hedge_cycle(positions or [], regime_state=regime, hedge_universe=self.symbols)
            except Exception as exc:
                get_bus().publish("system", {"level": "error", "where": "BackgroundPipeline", "error": str(exc)[:200]})
            self._stop.wait(self.interval_s)
