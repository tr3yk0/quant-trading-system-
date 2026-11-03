#!/usr/bin/env python3
"""Run combinatorial purged CV plan (and optional toy score) on a bar length."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from trading_team.research.purged_cv import combinatorial_purged_splits, evaluate_cpcv, walk_forward_purged


def main(argv=None):
    p = argparse.ArgumentParser(description="Combinatorial purged CV / walk-forward splits")
    p.add_argument("--bars", type=int, default=1000)
    p.add_argument("--groups", type=int, default=6)
    p.add_argument("--test-groups", type=int, default=2)
    p.add_argument("--embargo", type=int, default=5)
    p.add_argument("--purge", type=int, default=3)
    p.add_argument("--max-splits", type=int, default=20)
    p.add_argument("--mode", choices=["cpcv", "walk_forward", "evaluate"], default="cpcv")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    if args.mode == "walk_forward":
        splits = walk_forward_purged(args.bars, embargo=args.embargo, purge=args.purge)
        payload = {"n_splits": len(splits), "splits": [s.to_dict() for s in splits]}
    elif args.mode == "evaluate":
        # Toy score: prefer mid-series train (demo only)
        def score_fn(train_idx, test_idx):
            mid = args.bars / 2
            is_score = 1.0 - abs(sum(train_idx) / len(train_idx) - mid) / args.bars
            oos_score = 1.0 - abs(sum(test_idx) / len(test_idx) - mid) / args.bars
            return {"is_score": is_score, "oos_score": oos_score}
        payload = evaluate_cpcv(
            args.bars, score_fn,
            n_groups=args.groups, n_test_groups=args.test_groups,
            embargo=args.embargo, purge=args.purge, max_splits=args.max_splits,
        )
    else:
        plan = combinatorial_purged_splits(
            args.bars, n_groups=args.groups, n_test_groups=args.test_groups,
            embargo=args.embargo, purge=args.purge, max_splits=args.max_splits,
        )
        payload = plan.to_dict()

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print("=== Purged CV ===")
        if args.mode == "evaluate":
            print(f"  folds scored   : {payload.get('n_folds_scored')}")
            print(f"  mean OOS score : {payload.get('mean_oos_score')}")
            print(f"  mean IS score  : {payload.get('mean_is_score')}")
            print(f"  overfit gap    : {payload.get('overfit_gap')}")
            print(f"  pct +OOS       : {payload.get('pct_positive_oos'):.1f}%")
            print(f"  robust         : {payload.get('robust')}")
        else:
            print(f"  mode           : {args.mode}")
            print(f"  bars           : {args.bars}")
            n = payload.get("n_splits") or payload.get("n_splits")
            print(f"  splits         : {payload.get('n_splits', len(payload.get('splits', [])))}")
            for s in (payload.get("splits") or [])[:5]:
                print(f"    fold {s.get('fold_id')}: train={s.get('n_train')} test={s.get('n_test')} "
                      f"purged={s.get('purged')} embargoed={s.get('embargoed')}")
            if len(payload.get("splits") or []) > 5:
                print(f"    ... ({len(payload['splits']) - 5} more)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
