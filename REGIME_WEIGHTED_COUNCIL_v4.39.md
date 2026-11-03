# Regime-Weighted Council and Scout Scanner — v4.39.0

## Purpose

The Council is a regime/structure-anchored research stage for the existing multi-agent decision pipeline. Its purpose is to avoid letting every secondary specialist behave like an equal-weight binary veto, while retaining explicit safeguards for market-data integrity, broker eligibility, account risk, and order execution.

It is **not** a calibrated win-probability model and does not independently authorize an order.

## Architecture

1. **Existing universe scanner and market directors** continue their existing broker-symbol catalogue, scheduling and per-instrument evaluation. The Council does not add a second full-universe polling loop or create a heavyweight LLM agent for every symbol.
2. **Scout Scanner** evaluates a current `MarketSnapshot` with inexpensive checks: available M5 history, valid price and ATR, quote freshness when explicitly marked stale, trend-strength proxy, ATR expansion, regime direction, light swing structure, spread/ATR and optional MTF context. `scan_universe()` is also available for a caller that already has a batch of snapshots.
3. **Regime + Structure anchor** is the primary direction. Both scores must have meaningful magnitude and agree with the candidate side. An absent/weak/misaligned anchor is rejected rather than rescued by the secondary council.
4. **Secondary specialist families** contribute a bounded research score. Reports are grouped into families to avoid counting duplicate aliases as independent votes. Secondary reports can lower confidence or size; they cannot reverse the anchored direction.
5. **Context adjustments** scale size for a high uncertainty proxy, opposing volume-flow proxy and material secondary disagreement. `size_multiplier` is clamped to `[0.1, 1.0]`.
6. **Independent risk and execution stages** retain authority over data integrity, quote age, instrument mapping, eligibility, spread quality, mandatory news blackouts, portfolio exposure, drawdown, margin, stops, broker order validation and kill switch.

## Defaults and activation

Default configuration is shadow mode:

```dotenv
REGIME_WEIGHTED_COUNCIL_MODE=shadow
SCOUT_MIN_ADX=20
SCOUT_MIN_ATR_RATIO=0.80
SCOUT_REQUIRE_STRUCTURE_ALIGNMENT=1
SCOUT_MIN_BARS=60
SCOUT_MIN_BASE_QUALITY=0.42
SCOUT_MAX_SPREAD_ATR=0.35
```

Despite the legacy setting name `SCOUT_MIN_ADX`, the scout currently uses an **ADX-style trend-strength proxy scaled 0–100**, not a conventionally calculated ADX value. The report labels it as a proxy to avoid presenting unlike measurements as equivalent.

`shadow` evaluates and records Council output but leaves legacy decisions and sizing unchanged. `active` is an explicit experimental opt-in and should first be used in offline replay, then paper/shadow execution with realistic costs. Any unrecognized mode value falls back to `shadow`.

## Decision semantics

- `regime_score`, `structure_score`, `specialist_net_score`, `anchor_score` and `final_score` are ordinal strengths in `[-1, 1]`. They are **not** probabilities or expected returns.
- `entropy` is a normalized uncertainty proxy based on available consensus/disagreement diagnostics. It is **not** Shannon entropy unless a future implementation supplies a separately validated entropy calculation.
- `vpa_score` is currently a simple volume/range proxy derived from available M5 candles. It is **not** a validated institutional volume-spread-analysis model or an order-book read.
- `scout_qualified` means the baseline screen passed. It does not mean a setup is profitable or approved to trade.
- `take_trade` means only that the Council's primary research criteria passed. `execution_authorized` is always false in the serialized Council record.
- Missing, stale or contradictory input should lead to a neutral/rejected Council result, not an invented vote.

## Secondary disagreement vs hard blocks

Council context such as specialist disagreement, a neutral secondary desk, high uncertainty or the current volume-flow proxy does not independently kill an otherwise qualified primary anchor; it can reduce size. This does **not** soften account/risk limits, unsafe broker/instrument state, stale data, timestamp-integrity errors, spread/market-quality constraints, continuous data-health or walk-forward hard stops, mandatory news blackouts, invalid protection levels or execution validation. Those controls remain hard gates.

In the AI Mesh path, the only legacy Risk Debate blocker intentionally treated as contextual in active Council mode is the exact `Research consensus is weak` note, and only after the Scout and primary anchor pass. Other blockers remain. In the legacy `DecisionAgent` path, its statistical meta-label validation and portfolio guards remain independent safeguards; Council does not bypass them.

## Position sizing

The Council produces a size scalar only. Downstream risk sizing applies it to permitted risk/quantity calculations; existing minimum/maximum lots, stop-distance sizing, account exposure, margin and broker checks still apply. It must never be used to increase size above 1.0x. The multiplier is recorded in audit state so an operator can review which adjustments occurred.

Illustrative modifiers in the current deterministic implementation:

- uncertainty proxy > 0.75: ×0.40;
- uncertainty proxy > 0.60: ×0.70;
- volume-flow proxy significantly opposes primary direction: ×0.50;
- a material secondary family disagrees: ×0.50;
- combined modifiers are clamped at 0.10–1.00.

These are initial engineering defaults, **not empirically optimized trading parameters**. They need walk-forward calibration and out-of-sample verification before live use.

## Dashboard and audit

The AI Mesh page exposes the Scout status, mode, primary anchor, weighted score, specialist net score, size scalar, adjustment reasons and rationale. Each trace stage records `trade_authorization: false`; the Council stage is evidence for the risk/debate path, not an order command.

## Validation status

The release regression suite includes unit tests for scouting, anchor alignment, one-way sizing, specialist-family deduplication, stale/unknown quote handling, shadow defaults, active-mode gate retention and risk-scalar application. The test suite does not include live MT5/broker acceptance or profitability proof. A release test pass cannot validate financial performance.
