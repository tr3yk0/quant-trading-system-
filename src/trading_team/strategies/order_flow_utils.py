"""Phase 2 helpers — order-flow context for strategy evaluation and routing."""
from __future__ import annotations

from typing import Any

from trading_team.core.models import MarketSnapshot, Side
from trading_team.strategies.base import StrategyContext


def extract_order_flow(snapshot: MarketSnapshot, ctx: StrategyContext | None = None) -> dict[str, Any]:
    """Normalize Phase-1 order_flow_detail + regime fields into one dict."""
    meta = dict(snapshot.metadata or {})
    detail = meta.get("order_flow_detail") if isinstance(meta.get("order_flow_detail"), dict) else {}
    if ctx and isinstance(ctx.metadata.get("order_flow_detail"), dict) and not detail:
        detail = ctx.metadata["order_flow_detail"]
    regime = (ctx.regime if ctx and ctx.regime else {}) or {}
    obi_block = detail.get("obi") if isinstance(detail.get("obi"), dict) else {}
    cvd_block = detail.get("cvd") if isinstance(detail.get("cvd"), dict) else {}
    div = detail.get("divergence") if isinstance(detail.get("divergence"), dict) else {}
    flags = list(detail.get("flags") or regime.get("order_flow_flags") or [])
    if div.get("divergence") and div.get("divergence") != "none" and str(div["divergence"]) not in flags:
        flags.append(str(div["divergence"]))
    return {
        "status": str(detail.get("status") or "unavailable"),
        "proxy": float(detail.get("order_flow_proxy") or meta.get("order_flow") or regime.get("order_flow_proxy") or 0.0),
        "obi": float(obi_block.get("obi") or regime.get("obi") or 0.0),
        "obi_status": str(obi_block.get("status") or "unavailable"),
        "cvd": float(cvd_block.get("cvd") or 0.0),
        "cvd_slope": float(cvd_block.get("cvd_slope") or regime.get("cvd_slope") or 0.0),
        "cvd_status": str(cvd_block.get("status") or "unavailable"),
        "divergence": str(div.get("divergence") or regime.get("cvd_divergence") or "none"),
        "divergence_detail": str(div.get("detail") or ""),
        "flags": flags,
        "available": str(detail.get("status") or "") == "ok"
        or str(obi_block.get("status") or "") == "ok"
        or str(cvd_block.get("status") or "") == "ok",
    }


def flow_alignment(side: Side, flow: dict[str, Any]) -> tuple[float, list[str]]:
    """Return (multiplier ∈ ~[0.45, 1.15], notes) for a directional thesis vs order flow."""
    if side == Side.FLAT or not flow.get("available"):
        return 1.0, ["Order-flow unavailable — no adjustment"]

    notes: list[str] = []
    mult = 1.0
    proxy = float(flow.get("proxy") or 0.0)
    obi = float(flow.get("obi") or 0.0)
    slope = float(flow.get("cvd_slope") or 0.0)
    div = str(flow.get("divergence") or "none")
    flags = set(flow.get("flags") or [])

    # Confirmations
    if side == Side.BUY:
        if proxy > 0.05 or slope > 0 or obi > 0.12:
            mult *= 1.08
            notes.append("Order-flow supports BUY (proxy/OBI/CVD)")
        if "bullish_confirm" in flags or "cvd_slope_up" in flags or "obi_pressure_bid" in flags:
            mult *= 1.05
            notes.append("Flow flags confirm long")
        if div == "bearish_absorption" or "bearish_absorption" in flags:
            mult *= 0.55
            notes.append("Bearish absorption — long penalized")
        if proxy < -0.08 and slope < 0:
            mult *= 0.70
            notes.append("Flow opposes long")
    elif side == Side.SELL:
        if proxy < -0.05 or slope < 0 or obi < -0.12:
            mult *= 1.08
            notes.append("Order-flow supports SELL (proxy/OBI/CVD)")
        if "bearish_confirm" in flags or "cvd_slope_down" in flags or "obi_pressure_ask" in flags:
            mult *= 1.05
            notes.append("Flow flags confirm short")
        if div == "bullish_absorption" or "bullish_absorption" in flags:
            mult *= 0.55
            notes.append("Bullish absorption — short penalized")
        if proxy > 0.08 and slope > 0:
            mult *= 0.70
            notes.append("Flow opposes short")

    return max(0.45, min(1.15, mult)), notes


def spread_cost_score(snapshot: MarketSnapshot, *, soft_bps: float = 12.0, hard_bps: float = 30.0) -> tuple[float, str]:
    """0–1 quality score from spread in bps (1 = cheap, 0 = untradeable)."""
    price = float(snapshot.price or 0.0)
    spread = float(snapshot.spread or 0.0)
    if price <= 0:
        return 0.5, "No price for spread score"
    bps = spread / price * 10000.0
    if bps >= hard_bps:
        return 0.0, f"Spread {bps:.1f} bps ≥ hard limit {hard_bps:.0f}"
    if bps <= soft_bps:
        return 1.0, f"Spread {bps:.1f} bps within soft limit"
    # Linear decay between soft and hard
    score = max(0.0, 1.0 - (bps - soft_bps) / max(hard_bps - soft_bps, 1e-9))
    return round(score, 3), f"Spread {bps:.1f} bps — cost score {score:.2f}"
