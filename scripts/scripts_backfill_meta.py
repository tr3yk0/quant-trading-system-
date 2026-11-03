#!/usr/bin/env python3
"""
Backfill meta-labeler from historical closed trades.

Usage (from project root, venv activated):

    python scripts_backfill_meta.py
    python scripts_backfill_meta.py --reset --limit 5000
    python scripts_backfill_meta.py --db data/trading_team.db --json

Or as a module:

    python -m trading_team.research.trade_learning --reset
"""
from __future__ import annotations
import sys
from pathlib import Path

# Ensure src/ is on path when run as a loose script
ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from trading_team.research.trade_learning import main

if __name__ == "__main__":
    raise SystemExit(main())
