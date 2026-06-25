"""Shared pytest fixtures.

``populated_db`` builds a small but complete synthetic dataset in a throwaway
SQLite file and runs the full analytics pipeline once, so integration tests can
assert on real outputs without re-running per test.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture(scope="session")
def populated_db(tmp_path_factory):
    db = tmp_path_factory.mktemp("db") / "test.db"
    os.environ["POLYTRADER_DATABASE_URL"] = f"sqlite:///{db}"
    os.environ["POLYTRADER_DATA_SOURCE"] = "synthetic"

    from polytrader.config import reset_config_cache
    from polytrader.db.session import reset_engine
    reset_config_cache()
    reset_engine()

    from polytrader.synth.generator import generate_synthetic
    from polytrader.etl.positions import build_positions
    from polytrader.etl.metrics import compute_metrics
    from polytrader.ranking.scorer import rank_wallets
    from polytrader.clustering.cluster import cluster_wallets
    from polytrader.correlation.network import analyze_network
    from polytrader.backtest.engine import run_backtests
    from polytrader.smallaccount.kelly import optimize_small_account
    from polytrader.signals.detect import detect_signals
    from polytrader.alpha.categories import analyze_categories

    gen = generate_synthetic(drop=True, n_wallets=160, n_markets=220,
                             n_leaders=4, n_followers=14)
    build_positions()
    compute_metrics()
    rank_wallets()
    cluster_wallets()
    analyze_network()
    run_backtests()
    optimize_small_account()
    detect_signals()
    analyze_categories()
    return {"db": str(db), "gen": gen}
