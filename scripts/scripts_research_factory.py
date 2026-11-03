#!/usr/bin/env python3
"""Run the research factory on a toy score (or plug in your evaluator)."""
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

from trading_team.research.research_factory import run_factory


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--strategy", default="trend_following")
    p.add_argument("--search", choices=["grid", "bayes"], default="bayes")
    p.add_argument("--trials", type=int, default=12)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    grid = {
        "lookback": [10, 20, 40],
        "threshold": [0.05, 0.1, 0.2],
        "stop_atr": [1.0, 1.5, 2.0],
    }
    rng = random.Random(0)

    def evaluate_params(params):
        # Toy OOS score: prefer moderate lookback + mid threshold (demo only)
        score = 1.0 - abs(params["lookback"] - 20) / 40.0
        score -= abs(params["threshold"] - 0.1) * 2
        noise = (rng.random() - 0.5) * 0.05
        oos = score + noise
        # synthetic returns for gates
        rets = [oos * 0.01 + (rng.random() - 0.5) * 0.002 for _ in range(80)]
        return {"is_score": score + 0.05, "oos_score": oos, "returns": rets, "symbol": "TOY"}

    report = run_factory(
        strategy_id=args.strategy,
        param_grid=grid,
        evaluate_params=evaluate_params,
        max_trials=args.trials,
        search=args.search,
        auto_register=False,
    )
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print("=== Research factory ===")
        print(f"  strategy : {report['strategy_id']}")
        print(f"  search   : {report['search']}")
        print(f"  trials   : {report['n_trials']}")
        print(f"  passed   : {report['n_passed_gates']}")
        best = report.get("best") or {}
        print(f"  best oos : {best.get('oos_score')}")
        print(f"  best params: {best.get('params')}")
        print(f"  gates    : {best.get('gate_reasons')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
