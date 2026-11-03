#!/usr/bin/env python3
"""Demo / utility: allocate capital across symbols or strategies via risk parity / max Sharpe."""
from __future__ import annotations
import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from trading_team.research.portfolio_allocator import allocate, size_multipliers_from_weights


def _synthetic_returns(names, n=120, seed=7):
    rng = random.Random(seed)
    out = {}
    for i, nm in enumerate(names):
        drift = 0.0002 * (1 if i % 2 == 0 else -1)
        vol = 0.01 + 0.002 * i
        series = []
        x = 0.0
        for _ in range(n):
            x = drift + vol * (rng.random() - 0.5) * 2
            series.append(x)
        out[nm] = series
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description="Portfolio allocator (risk parity / max sharpe / inv vol)")
    p.add_argument("--names", default="trend_following,mean_reversion,breakout,momentum_burst")
    p.add_argument("--method", default="risk_parity",
                   choices=["risk_parity", "max_sharpe", "inverse_volatility", "equal_weight"])
    p.add_argument("--max-weight", type=float, default=0.40)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    names = [x.strip() for x in args.names.split(",") if x.strip()]
    rets = _synthetic_returns(names)
    res = allocate(names, rets, method=args.method, max_weight=args.max_weight)
    mults = size_multipliers_from_weights(res.weight_map())
    payload = {**res.to_dict(), "size_multipliers": mults}
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"=== Portfolio allocation ({res.method}) ===")
        for n, w, rc in zip(res.names, res.weights, res.risk_contributions):
            print(f"  {n:28s}  w={w:.3f}  rc={rc:.3f}  size_mult={mults[n]:.2f}")
        print(f"  expected_vol≈{res.expected_vol:.5f}")
        for note in res.notes:
            print(f"  note: {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
