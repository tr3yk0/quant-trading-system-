# AI Trading Team v4.39.0
## Regime-Weighted Council + Instrument-Aware News Analyst + Scout Architecture

A multi-agent institutional-grade AI trading system with classical quantitative anchors, specialist research desks, regime-weighted decision council, instrument-aware news analysis, and full MetaTrader 5 integration.

---

## What's New in v4.39.0

- **Scout Scanner** — Lightweight pre-filter on the current symbol snapshot using regime, structure, volatility and spread/ATR checks. Records prioritized research candidates.
- **Regime-Weighted Council** — Regime + Structure form the primary directional anchor. Secondary specialist evidence is family-grouped, contributes to research score, and can only *reduce* size (never increase or independently reverse a valid primary anchor).
- **One-way size scalar** — Council produces a multiplier in the range `[0.1, 1.0]` that is applied by the Risk Engine. It cannot inflate position size.
- **Shadow-first design** — `REGIME_WEIGHTED_COUNCIL_MODE=shadow` (default) audits and displays Council output without affecting live decisions. Set to `active` only after paper/replay validation.
- **News as evidence only** — News remains a non-voting evidence source. Existing reliability gates and mandatory blackouts are preserved.
- Full exposure of Scout status, primary anchor, weighted score, specialist net, size scalar and rationale in the AI Mesh / Council panel.

---

## Project Structure (Cleaned)

```
ai_trading_team/
├── README.md                          # This file
├── QUICKSTART_WINDOWS.txt             # Primary installation & launch guide
├── REGIME_WEIGHTED_COUNCIL_v4.39.md    # Detailed Council architecture
├── .env.example                       # Environment template
├── ENV_MT5.txt                        # MT5-specific notes
├── pyproject.toml                     # Package metadata & entry points
├── docker-compose.yml
├── START.bat / STOP.bat / DEBUG.bat   # Windows launchers
├── build_core.ps1
│
├── src/
│   ├── trading_team/                  # Main package
│   │   ├── agents/                    # Specialist agents (Scout, Regime, News, Risk, Decision...)
│   │   ├── ai_mesh/                   # Debate floor, Guardian Chair, evidence hierarchy
│   │   ├── core/                      # Config, models, events, resilience
│   │   ├── data/                      # MT5 feed, news sources, economic calendar
│   │   ├── market/                    # Scout scanner, desks, registry
│   │   ├── research/                  # Regime-weighted council, quant lab, portfolio, learning
│   │   ├── risk/                      # Risk engine & gates
│   │   ├── strategies/                # Strategy implementations (incl. ICE grid)
│   │   ├── adapters/                  # Broker / MT5 / multi-venue adapters
│   │   ├── web/                       # Dashboard (ThreadingHTTPServer + static assets)
│   │   ├── monitor/                   # Supervisor & Guardian processes
│   │   └── ...
│   └── finance/                       # Optional Finance Toolkit (analytics, backtesting, models)
│
├── tests/                             # Comprehensive regression suite
├── scripts/                           # Utility & migration scripts
├── docs/
│   ├── archive/                       # Historical releases, build reports, install guides, hotfixes
│   └── finance_toolkit/
├── cpp_core/                          # Optional C++ acceleration (grid / ICE)
├── deploy/                            # Kubernetes manifests
├── data/                              # Local data / samples
└── third_party_licenses/
```

All historical release notes, build reports, install guides and feature notes have been moved under `docs/archive/` to keep the root clean.

---

## Quick Start

1. Extract this archive into a **new folder** (do not overwrite an existing live installation or its `.env` / database).
2. Copy `.env.example` → `.env` and fill in your MT5 / API keys.
3. Follow **`QUICKSTART_WINDOWS.txt`** for environment setup, dependency installation and first launch.
4. Prefer paper / shadow mode until you have validated the Council and Risk gates on your own data.

Entry points (after `pip install -e .` or using the launchers):

| Command | Purpose |
|---------|---------|
| `ai-trading-team` | Single analysis cycle |
| `ai-trading-dashboard` | Web dashboard |
| `ai-trading-supervisor` | Long-running supervisor |
| `ai-trading-guardian` | Risk guardian process |
| `ai-trading-backtest` | Backtest CLI |

Or simply run `START.bat` on Windows.

---

## Key Configuration Flags

```bash
# Council behaviour (safe default)
REGIME_WEIGHTED_COUNCIL_MODE=shadow   # or "active" after validation

# Trading mode
TRADING_MODE=paper                   # paper | live
LIVE_TRADING_ENABLED=false

# News
NEWSAPI_API_KEY=                     # optional
```

See `.env.example` and `REGIME_WEIGHTED_COUNCIL_v4.39.md` for the full set of controls.

---

## Architecture Highlights

- **Primary path**: `AgentMesh` (research debate → risk debate → Guardian Chair)
- **Parallel specialists**: Scout, Regime, Structure, Microstructure, Sentiment, Strategy, Portfolio, News, Risk, Decision
- **Hard gates** remain authoritative: data freshness, timestamp integrity, spread/market quality, account risk limits, mandatory news blackouts, broker state, and Risk Engine validation. Council cannot bypass them.
- **Finance Toolkit** (optional extra) provides institutional analytics, portfolio construction, screening and research models under `src/finance/`.

---

## Validation Status

- Regression suite: **387 passed, 40 skipped** (as of the v4.39.0 release build).
- Python compilation and JavaScript syntax checks passed.
- No live-broker profitability claims. Council scores are ordinal research strengths, **not** win probabilities.
- Always validate on paper / replay before enabling active Council mode or live trading.

---

## Documentation Map

| Document | Location |
|----------|----------|
| Council architecture & limitations | `REGIME_WEIGHTED_COUNCIL_v4.39.md` |
| Install / validation sequence | `docs/archive/install_guides/INSTALL_REGIME_WEIGHTED_COUNCIL_v4.39.txt` |
| News Analyst (v4.38) | `docs/archive/feature_notes/NEWS_AGENT_v4.38.0.md` (if present) or original notes |
| Release notes | `docs/archive/releases/` |
| Build reports | `docs/archive/build_reports/` |
| Historical feature notes & hotfixes | `docs/archive/feature_notes/` and `docs/archive/hotfixes/` |

---

## License & Disclaimer

This software is provided for research and educational purposes. Trading involves substantial risk of loss. Past performance (or simulated results) is not indicative of future results. The authors and contributors accept no liability for trading losses incurred through use of this system. Use at your own risk and only with capital you can afford to lose.

---

**Version**: 4.39.0  
**Codename**: Regime-Weighted Council + Scout Architecture
