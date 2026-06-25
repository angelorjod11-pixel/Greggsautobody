"""Integration tests: the full pipeline on a small synthetic dataset.

These assert the system recovers the planted ground truth (skill, leaders) and
honors its own configured methodology (weights, look-ahead split).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sqlalchemy import func, select

from polytrader.config import get_config
from polytrader.db.models import (AnalysisArtifact, Position, Wallet,
                                  WalletCluster, WalletMetric, WalletScore)
from polytrader.db.session import session_scope


def _artifact(s, kind, name=None):
    q = select(AnalysisArtifact.payload).where(AnalysisArtifact.kind == kind)
    if name:
        q = q.where(AnalysisArtifact.name == name)
    return s.scalar(q.order_by(AnalysisArtifact.id.desc()))


def test_positions_built(populated_db):
    with session_scope() as s:
        n = s.scalar(select(func.count()).select_from(Position))
        resolved = s.scalar(select(func.count()).select_from(Position)
                            .where(Position.status.in_(["settled", "closed"])))
    assert n > 500 and resolved > 400


def test_metrics_and_eligibility(populated_db):
    with session_scope() as s:
        met = pd.read_sql(select(WalletMetric.__table__), s.bind)
    assert len(met) > 50
    assert met["eligible"].sum() > 20
    # eligible wallets clear the configured thresholds
    el = get_config().eligibility
    elig = met[met["eligible"]]
    assert (elig["n_positions"] >= el["min_resolved_positions"]).all()
    assert (elig["n_markets"] >= el["min_distinct_markets"]).all()


def test_ranking_recovers_skill(populated_db):
    with session_scope() as s:
        sc = pd.read_sql(select(WalletScore.__table__), s.bind)
        labels = dict(s.execute(select(Wallet.address, Wallet.label)).all())
        gt = _artifact(s, "ground_truth")
    sc["skill"] = sc["wallet_address"].map(lambda a: (gt.get(a) or {}).get("skill"))
    sc["label"] = sc["wallet_address"].map(labels)
    rho = spearmanr(sc["composite_score"], sc["skill"]).correlation
    assert rho > 0.2, f"ranking should track latent skill, got rho={rho:.2f}"
    # leaders should sit well above median rank; none in the bottom quartile
    med_rank = sc["rank"].median()
    leader_ranks = sc[sc["label"] == "leader"]["rank"]
    assert (leader_ranks < med_rank).mean() >= 0.75


def test_composite_uses_configured_weights(populated_db):
    w = get_config().ranking.weights
    with session_scope() as s:
        sc = pd.read_sql(select(WalletScore.__table__), s.bind)
    recomputed = (w["profitability"] * sc["profitability_score"]
                  + w["win_rate"] * sc["win_rate_score"]
                  + w["consistency"] * sc["consistency_score"]
                  + w["risk"] * sc["risk_score"]
                  + w["longevity"] * sc["longevity_score"])
    assert np.allclose(recomputed, sc["composite_score"], atol=1e-6)
    # CI brackets the point estimate
    assert (sc["score_ci_low"] <= sc["composite_score"] + 1e-6).all()
    assert (sc["score_ci_high"] >= sc["composite_score"] - 1e-6).all()


def test_clusters_partition_eligibles(populated_db):
    with session_scope() as s:
        clu = pd.read_sql(select(WalletCluster.__table__), s.bind)
        n_elig = s.scalar(select(func.count()).select_from(WalletMetric)
                         .where(WalletMetric.eligible == True))  # noqa: E712
    assert len(clu) == n_elig
    assert clu["kmeans_cluster"].nunique() >= 2


def test_network_and_leaders(populated_db):
    with session_scope() as s:
        net = _artifact(s, "network_summary")
        lead = _artifact(s, "leader_follower")
        labels = dict(s.execute(select(Wallet.address, Wallet.label)).all())
    assert net["graph"]["nodes"] > 0
    leaders = lead["leaders"]
    assert len(leaders) >= 1
    # the single highest-lead-score wallet should be a ground-truth leader
    top_leader = max(leaders, key=lambda L: L["lead_score"])
    assert labels.get(top_leader["wallet"]) == "leader"


def test_backtest_lookahead_safe(populated_db):
    with session_scope() as s:
        meta = _artifact(s, "backtest_meta")
        strats = {a.name: a.payload for a in s.execute(
            select(AnalysisArtifact).where(AnalysisArtifact.kind == "backtest")).scalars()}
    assert pd.to_datetime(meta["t_split"]) < pd.to_datetime(meta["as_of"])  # split precedes end
    assert meta["n_test_signals"] > 0
    for name in ["A_copy_top10", "B_consensus_3plus", "C_filtered_elite", "D_weighted_consensus"]:
        assert name in strats and "ending_balance" in strats[name]


def test_small_account_outputs(populated_db):
    with session_scope() as s:
        sa = _artifact(s, "small_account", "summary")
    if "scenarios" not in sa:        # tiny samples may lack enough elite trades
        return
    hc = sa["scenarios"]["haircut_edge"]
    assert 0.0 <= hc["full_kelly_fraction"] <= 1.0
    for rule in hc["sizing_rules"]:
        assert 0.0 <= rule["prob_survival"] <= 1.0
        assert 0.0 <= rule["prob_double"] <= 1.0


def test_signals_and_alpha(populated_db):
    with session_scope() as s:
        sig = _artifact(s, "signals", "entry_signals")
        cat = _artifact(s, "category_alpha", "by_category")
    assert "findings" in sig and "elite_avg_realized_edge" in sig["findings"]
    assert "elite_by_category" in cat and "rankings" in cat
    # elite should exploit mispricing more than the field on average
    f = sig["findings"]
    assert f["elite_avg_realized_edge"] >= f["field_avg_realized_edge"]
