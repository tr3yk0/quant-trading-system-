#!/usr/bin/env python3
"""Nightly self-improve job: drift → meta backfill → offline RL → feature summary."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from trading_team.research.nightly_improve import main

if __name__ == "__main__":
    raise SystemExit(main())
