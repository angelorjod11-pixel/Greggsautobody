"""Small-account ($20 bankroll) optimization.

Question: how should someone starting with $20 size bets when copying the elite,
given $1–$5 position limits, fees and (heavier) small-order slippage?

Method
------
1. **Edge distribution.**  We estimate the per-dollar return a small account would
   actually face by replaying the Top-N elite's resolved positions as held-to-
   resolution copies, charged small-account slippage.  This is the empirical
   reward distribution ``r``.  Because it is in-sample (optimistic), we also run a
   **haircut** scenario that shrinks each trade's edge toward zero for robustness.
2. **Kelly fraction.**  We numerically maximize ``E[log(1 + f·r)]`` to get the
   full-Kelly fraction ``f*``; the deployed rule uses fractional Kelly
   (``kelly_fraction``) clamped to the $1–$5 band — Kelly is famously over-
   aggressive out of sample.
3. **Monte-Carlo.**  Each sizing rule is simulated over many bootstrapped paths
   to estimate ending-balance distribution, max drawdown, probability of survival
   (never falling below the ruin threshold) and probability of doubling to $40.
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from scipy.optimize import minimize_scalar
from sqlalchemy import select

from ..config import get_config
from ..db.models import AnalysisArtifact, Market, Position, WalletScore
from ..db.session import session_scope


def _elite_return_distribution(session, top_n: int, slippage_bps: float, fee_bps: float) -> np.ndarray:
    """Per-dollar P&L of held-to-resolution copies of the top-N elite's positions."""
    top = pd.read_sql(select(WalletScore.__table__.c.wallet_address)
                      .order_by(WalletScore.rank).limit(top_n), session.bind)["wallet_address"].tolist()
    if not top:
        return np.array([])
    pos = pd.read_sql(select(
        Position.__table__.c.wallet_address, Position.__table__.c.market_id,
        Position.__table__.c.outcome_index, Position.__table__.c.avg_entry_price,
        Position.__table__.c.status).where(Position.wallet_address.in_(top)), session.bind)
    mk = pd.read_sql(select(Market.__table__.c.id, Market.__table__.c.winning_outcome)
                     .where(Market.resolved == True), session.bind).rename(columns={"id": "market_id"})  # noqa: E712
    pos = pos[pos["status"].isin(["settled", "closed"])].merge(mk, on="market_id", how="inner")
    if pos.empty:
        return np.array([])
    slip = slippage_bps / 1e4
    fee = fee_bps / 1e4
    ep = np.clip(pos["avg_entry_price"].to_numpy() * (1 + slip) + fee, 0.01, 0.99)
    payout = (pos["outcome_index"].to_numpy() == pos["winning_outcome"].to_numpy()).astype(float)
    return (payout - ep) / ep  # per-dollar return, in [-1, (1-ep)/ep]


def _kelly_fraction(r: np.ndarray) -> float:
    """Full-Kelly fraction maximizing E[log(1+f r)] over f in [0, 0.999]."""
    if len(r) == 0 or r.mean() <= 0:
        return 0.0

    def neg_growth(f):
        return -np.mean(np.log1p(np.clip(f * r, -0.999999, None)))

    res = minimize_scalar(neg_growth, bounds=(0.0, 0.999), method="bounded")
    return float(max(0.0, res.x))


def _simulate_rule(r: np.ndarray, rule: str, kelly_f: float, cfg, rng) -> Dict[str, float]:
    sa = cfg.small_account
    B0 = float(sa["bankroll_usd"]); lo = float(sa["min_bet_usd"]); hi = float(sa["max_bet_usd"])
    H = int(sa["horizon_trades"]); paths = int(sa["monte_carlo_paths"])
    ruin = float(sa["ruin_threshold_usd"]); target = float(sa["double_target_usd"])
    kf = float(sa["kelly_fraction"])

    draws = rng.choice(r, size=(paths, H), replace=True)
    bankroll = np.full(paths, B0)
    peak = np.full(paths, B0)
    max_dd = np.zeros(paths)
    ruined = np.zeros(paths, dtype=bool)
    doubled = np.zeros(paths, dtype=bool)

    for j in range(H):
        # determine stake per path
        if rule == "flat_1":
            stake = np.full(paths, lo)
        elif rule == "flat_2":
            stake = np.full(paths, 2.0)
        elif rule == "flat_5":
            stake = np.full(paths, hi)
        elif rule == "frac_kelly":
            stake = kelly_f * kf * bankroll
        elif rule == "half_kelly":
            stake = kelly_f * 0.5 * bankroll
        else:  # full_kelly
            stake = kelly_f * bankroll
        stake = np.clip(stake, lo, hi)
        stake = np.minimum(stake, bankroll)              # cannot bet more than we have
        can_bet = (bankroll >= lo) & (~ruined)
        stake = np.where(can_bet, stake, 0.0)

        bankroll = bankroll + stake * draws[:, j]
        peak = np.maximum(peak, bankroll)
        max_dd = np.maximum(max_dd, (peak - bankroll) / peak)
        ruined = ruined | (bankroll < ruin)
        doubled = doubled | (bankroll >= target)

    return {
        "rule": rule,
        "median_ending": round(float(np.median(bankroll)), 2),
        "mean_ending": round(float(np.mean(bankroll)), 2),
        "p10_ending": round(float(np.percentile(bankroll, 10)), 2),
        "p90_ending": round(float(np.percentile(bankroll, 90)), 2),
        "prob_survival": round(float(1.0 - ruined.mean()), 4),
        "prob_ruin": round(float(ruined.mean()), 4),
        "prob_double": round(float(doubled.mean()), 4),
        "median_max_drawdown": round(float(np.median(max_dd)), 4),
        "expected_growth_mult": round(float(np.mean(bankroll) / B0), 3),
    }


def optimize_small_account(as_of: Optional[datetime] = None) -> Dict[str, object]:
    cfg = get_config()
    sa = cfg.small_account
    rng = np.random.default_rng(cfg.seed)

    with session_scope() as session:
        if as_of is None:
            as_of = pd.to_datetime(session.scalar(select(WalletScore.as_of).limit(1)) or
                                   datetime.utcnow()).to_pydatetime()
        # follow the Top-10 by default (matches the best-performing copy strategy)
        r_full = _elite_return_distribution(session, top_n=10,
                                            slippage_bps=float(sa["slippage_bps"]),
                                            fee_bps=float(sa["fee_bps"]))
        if len(r_full) < 20:
            payload = {"note": "insufficient elite trades for edge estimation"}
            session.add(AnalysisArtifact(as_of=as_of, kind="small_account", name="summary", payload=payload))
            return payload

        # haircut scenario: shrink each trade's edge 50% toward 0 (robustness)
        r_hair = 0.5 * r_full

        rules = ["flat_1", "flat_2", "flat_5", "frac_kelly", "half_kelly", "full_kelly"]
        scenarios = {}
        for sc_name, r in [("in_sample_edge", r_full), ("haircut_edge", r_hair)]:
            kf_full = _kelly_fraction(r)
            sims = [_simulate_rule(r, rule, kf_full, cfg, rng) for rule in rules]
            scenarios[sc_name] = {
                "n_trades_in_edge_sample": int(len(r)),
                "mean_per_trade_return": round(float(r.mean()), 4),
                "std_per_trade_return": round(float(r.std()), 4),
                "win_rate": round(float((r > 0).mean()), 4),
                "full_kelly_fraction": round(kf_full, 4),
                "fractional_kelly_used": round(kf_full * float(sa["kelly_fraction"]), 4),
                "recommended_bet_at_20": round(float(np.clip(
                    kf_full * float(sa["kelly_fraction"]) * float(sa["bankroll_usd"]),
                    float(sa["min_bet_usd"]), float(sa["max_bet_usd"]))), 2),
                "sizing_rules": sims,
            }

        # pick the best rule on the haircut scenario by survival-weighted growth
        hair = scenarios["haircut_edge"]["sizing_rules"]
        def _utility(s):
            return s["prob_survival"] * np.log(max(s["expected_growth_mult"], 1e-3))
        best = max(hair, key=_utility)

        payload = {
            "bankroll_usd": float(sa["bankroll_usd"]),
            "constraints": {"min_bet": float(sa["min_bet_usd"]), "max_bet": float(sa["max_bet_usd"]),
                            "slippage_bps": float(sa["slippage_bps"]), "horizon_trades": int(sa["horizon_trades"])},
            "scenarios": scenarios,
            "recommended_rule": best["rule"],
            "recommendation_basis": "max survival-weighted log-growth on the 50%-haircut edge",
        }
        session.add(AnalysisArtifact(as_of=as_of, kind="small_account", name="summary", payload=payload))
        return {"recommended_rule": best["rule"],
                "full_kelly_fraction_haircut": scenarios["haircut_edge"]["full_kelly_fraction"],
                "recommended_bet_at_20": scenarios["haircut_edge"]["recommended_bet_at_20"],
                "haircut_best": {k: best[k] for k in
                                 ("rule", "prob_survival", "prob_double", "expected_growth_mult")}}
