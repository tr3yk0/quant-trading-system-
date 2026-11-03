# AI Trading Team v4.39.0 — Project Structure

## Root (essential only)

| File | Purpose |
|------|---------|
| `README.md` | Overview, quick start, architecture summary |
| `QUICKSTART_WINDOWS.txt` | Step-by-step install & launch |
| `REGIME_WEIGHTED_COUNCIL_v4.39.md` | Full Council design & limitations |
| `.env.example` | All configuration keys with comments |
| `ENV_MT5.txt` | MetaTrader 5 connection notes |
| `pyproject.toml` | Package definition, extras, entry points |
| `START.bat` / `STOP.bat` / `DEBUG.bat` | Windows launchers |
| `docker-compose.yml` | Optional container orchestration |
| `build_core.ps1` | C++ core build helper |

## Source Layout

```
src/
├── trading_team/                 # Primary package
│   ├── __init__.py               # Package marker + version
│   ├── main.py                   # Single-cycle entry point
│   ├── agents/                   # Specialist agents
│   │   ├── scout.py              # Opportunity / Scout agent
│   │   ├── regime.py             # Regime classification
│   │   ├── structure.py          # Market structure
│   │   ├── news.py               # Instrument-aware news analyst
│   │   ├── risk.py / decision.py / portfolio.py ...
│   │   └── team.py               # Specialist team runner
│   ├── ai_mesh/                  # Multi-desk debate system
│   │   ├── orchestrator.py
│   │   ├── debate_floor.py
│   │   ├── guardian_chair.py
│   │   ├── evidence_hierarchy.py
│   │   └── ...
│   ├── market/
│   │   ├── scout_scanner.py      # Lightweight Scout pre-filter
│   │   ├── desks.py
│   │   └── specialized_scouts.py
│   ├── research/
│   │   ├── regime_weighted_council.py   # ★ Core v4.39 component
│   │   ├── adaptive_confidence.py
│   │   ├── portfolio_*.py
│   │   ├── quant_lab/
│   │   └── ...
│   ├── core/                     # Config, models, events, paths
│   ├── data/                     # MT5, news providers, calendar
│   ├── risk/                     # Risk engine & hard gates
│   ├── strategies/               # Strategy library (ICE grid etc.)
│   ├── adapters/                 # Broker / multi-venue
│   ├── web/                      # Dashboard + static assets
│   ├── monitor/                  # Supervisor / Guardian
│   ├── backtest/
│   └── storage/
│
└── finance/                      # Optional Finance Toolkit
    ├── analytics/
    ├── backtesting/
    ├── portfolio/
    ├── models/
    └── ...
```

## Tests

`tests/` contains the full regression suite (regime council, news, portfolio, UI, launchers, etc.).

## Scripts

`scripts/` holds one-off migration, backfill and installer helpers that are not part of the runtime package.

## Documentation Archive

All historical material lives under `docs/archive/`:

- `releases/` — RELEASE_v4.x.md files
- `build_reports/` — BUILD_REPORT_v4.x.txt
- `install_guides/` — INSTALL_*.txt
- `hotfixes/` — LAUNCHER_HOTFIX_*.md
- `feature_notes/` — Feature design docs from earlier versions

## Optional Components

- `cpp_core/` — C++ acceleration for grid / ICE strategies
- `deploy/k8s/` — Kubernetes manifests
- `third_party_licenses/`

---

This layout was produced by consolidating ~70 versioned documentation files from the project root into `docs/archive/`, adding the missing top-level package `__init__.py`, and removing transient runtime directories.
