"""Command-line entrypoint.

Examples
--------
    python -m polytrader.cli all                 # full pipeline (synthetic by default)
    POLYTRADER_DATA_SOURCE=live python -m polytrader.cli all
    python -m polytrader.cli rank                # re-run a single stage
    python -m polytrader.cli top --n 25          # print the top-N wallets
    python -m polytrader.cli report              # regenerate the report only
"""
from __future__ import annotations

import argparse
import json
import sys


def _print(obj):
    print(json.dumps(obj, indent=2, default=str))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="polytrader", description="Polymarket trader research system")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("all", help="run the full pipeline end-to-end")
    sub.add_parser("initdb", help="create database tables")
    for name in ["ingest", "positions", "metrics", "rank", "cluster", "correlate",
                 "backtest", "smallaccount", "signals", "alpha", "report"]:
        sub.add_parser(name, help=f"run the {name} stage")
    t = sub.add_parser("top", help="print top-N ranked wallets")
    t.add_argument("--n", type=int, default=25)
    p.add_argument("--config", default=None, help="path to config.yaml")
    p.add_argument("--keep", action="store_true", help="do not drop existing data on 'all'")
    args = p.parse_args(argv)

    if args.config:
        import os
        os.environ["POLYTRADER_CONFIG"] = args.config

    cmd = args.cmd
    if cmd == "all":
        from .pipeline import run_all
        run_all(drop=not args.keep)
        return 0
    if cmd == "initdb":
        from .db.session import init_db
        init_db(drop=True); print("tables created"); return 0
    if cmd == "top":
        from .ranking.scorer import get_top_wallets
        df = get_top_wallets(args.n)
        cols = ["rank", "wallet_address", "composite_score", "score_ci_low", "score_ci_high",
                "total_profit_usd", "avg_roi", "win_rate", "sharpe", "n_positions"]
        cols = [c for c in cols if c in df.columns]
        import pandas as pd
        with pd.option_context("display.max_rows", None, "display.width", 200):
            print(df[cols].to_string(index=False))
        return 0

    # single stages
    dispatch = {
        "ingest": lambda: __import__("polytrader.pipeline", fromlist=["_ingest"])._ingest(drop=True),
        "positions": lambda: __import__("polytrader.etl.positions", fromlist=["build_positions"]).build_positions(),
        "metrics": lambda: __import__("polytrader.etl.metrics", fromlist=["compute_metrics"]).compute_metrics(),
        "rank": lambda: __import__("polytrader.ranking.scorer", fromlist=["rank_wallets"]).rank_wallets(),
        "cluster": lambda: __import__("polytrader.clustering.cluster", fromlist=["cluster_wallets"]).cluster_wallets(),
        "correlate": lambda: __import__("polytrader.correlation.network", fromlist=["analyze_network"]).analyze_network(),
        "backtest": lambda: __import__("polytrader.backtest.engine", fromlist=["run_backtests"]).run_backtests(),
        "smallaccount": lambda: __import__("polytrader.smallaccount.kelly", fromlist=["optimize_small_account"]).optimize_small_account(),
        "signals": lambda: __import__("polytrader.signals.detect", fromlist=["detect_signals"]).detect_signals(),
        "alpha": lambda: __import__("polytrader.alpha.categories", fromlist=["analyze_categories"]).analyze_categories(),
        "report": lambda: __import__("polytrader.report.build_report", fromlist=["build_report"]).build_report(),
    }
    _print(dispatch[cmd]())
    return 0


if __name__ == "__main__":
    sys.exit(main())
