"""End-to-end pipeline orchestration.

``run_all()`` executes the full research pipeline in dependency order:

    ingest → positions → metrics → rank → cluster → correlate
           → backtest → small-account → signals → alpha → report

Each step reads from and writes to the database, so steps can also be run
individually (see :mod:`polytrader.cli`).  In ``synthetic`` mode the ingest step
generates a realistic offline dataset; in ``live`` mode it pulls from the
Polymarket APIs.
"""
from __future__ import annotations

import time
from typing import Callable, Dict, List, Optional, Tuple

from .config import get_config
from .db.session import init_db


def _ingest(drop: bool = True) -> Dict:
    cfg = get_config()
    if cfg.data_source == "live":
        from .collect.pipeline import collect_all
        return collect_all(drop=drop)
    from .synth.generator import generate_synthetic
    return generate_synthetic(drop=drop)


# (label, callable) in execution order. Imports are local so a missing optional
# dependency (e.g. the live collector's httpx) never blocks an offline run.
def _steps() -> List[Tuple[str, Callable[[], Dict]]]:
    from .etl.positions import build_positions
    from .etl.metrics import compute_metrics
    from .ranking.scorer import rank_wallets
    from .clustering.cluster import cluster_wallets
    from .correlation.network import analyze_network
    from .backtest.engine import run_backtests
    from .smallaccount.kelly import optimize_small_account
    from .signals.detect import detect_signals
    from .alpha.categories import analyze_categories
    from .report.build_report import build_report

    return [
        ("positions", build_positions),
        ("metrics", compute_metrics),
        ("rank", rank_wallets),
        ("cluster", cluster_wallets),
        ("correlate", analyze_network),
        ("backtest", run_backtests),
        ("small_account", optimize_small_account),
        ("signals", detect_signals),
        ("alpha", analyze_categories),
        ("report", build_report),
    ]


def run_all(drop: bool = True, verbose: bool = True) -> Dict[str, Dict]:
    init_db(drop=drop)
    out: Dict[str, Dict] = {}

    def _run(label, fn):
        t = time.time()
        res = fn()
        dt = time.time() - t
        out[label] = {"result": res, "seconds": round(dt, 2)}
        if verbose:
            brief = {k: res[k] for k in list(res)[:4]} if isinstance(res, dict) else res
            print(f"  [{label:14s}] {dt:6.2f}s  {brief}")

    if verbose:
        print(f"== polytrader pipeline ({get_config().data_source} source) ==")
    _run("ingest", lambda: _ingest(drop=False))  # init_db already ran
    for label, fn in _steps():
        _run(label, fn)
    if verbose:
        total = sum(v["seconds"] for v in out.values())
        print(f"== done in {total:.1f}s ==")
    return out
