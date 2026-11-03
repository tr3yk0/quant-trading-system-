#!/usr/bin/env python3
"""
Backfill historical bid/ask tick data from CSV or MT5.

Examples
--------
    python scripts_backfill_ticks.py --csv data/raw/xau_ticks.csv --symbol XAUUSD
    python scripts_backfill_ticks.py --csv data/raw/xau_ticks.csv --resample M1,M5 --reset
    python scripts_backfill_ticks.py --mt5 --symbol XAUUSD --days 5 --resample M1,M5,H1
    python scripts_backfill_ticks.py --mt5 --symbol BTCUSD --start 2026-10-01T00:00:00Z --end 2026-10-07T00:00:00Z

Module form:
    python -m trading_team.backtest.tick_backfill --csv path/to/ticks.csv --symbol XAUUSD
"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from trading_team.backtest.tick_backfill import main

if __name__ == "__main__":
    raise SystemExit(main())
