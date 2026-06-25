"""Wallet ranking engine.

Builds a composite score in [0,100] from the per-wallet metric vector using the
weighting scheme in ``config.yaml``:

    40% Profitability   (total profit, avg ROI, profit factor)
    20% Win rate
    20% Consistency     (median ROI, ROI stability, positive-month rate)
    10% Risk            (low max-drawdown, Sharpe)
    10% Longevity       (active days, trade count)

Each raw metric is winsorized then mapped to a population **percentile** so the
blend is scale-free and robust to the heavy tails typical of trading P&L
(one whale's profit can't dominate the whole score).  "Lower-is-better" metrics
(max drawdown) are inverted before percentiling.

To guard against small-sample luck — a wallet that went 9/10 on coin-flips — we
attach a **bootstrap confidence interval** to every score by resampling each
wallet's own resolved positions.  Wallets whose CI is wide or whose lower bound
collapses are flagged implicitly by the interval, so followers can prefer
high-score *and* tight-CI wallets.
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, Optional

import numpy as np
import pandas as pd
from sqlalchemy import delete, select

from ..config import get_config
from ..db.models import Position, WalletMetric, WalletScore
from ..db.session import session_scope


def _winsorize(x: np.ndarray, p: float) -> np.ndarray:
    if p <= 0 or len(x) == 0:
        return x
    lo, hi = np.nanpercentile(x, [100 * p, 100 * (1 - p)])
    return np.clip(x, lo, hi)


class _Percentiler:
    """Maps a value to its percentile [0,1] within a fixed population sample."""

    def __init__(self, values: np.ndarray, invert: bool = False):
        v = np.sort(np.asarray(values, dtype=float))
        self.sorted = v
        self.n = max(len(v), 1)
        self.invert = invert

    def __call__(self, x):
        x = np.asarray(x, dtype=float)
        pct = np.searchsorted(self.sorted, x, side="right") / self.n
        return (1.0 - pct) if self.invert else pct


def _profit_factor(pnl: np.ndarray) -> float:
    gains = pnl[pnl > 0].sum()
    losses = -pnl[pnl < 0].sum()
    if losses <= 1e-9:
        return float(gains) if gains > 0 else 0.0
    return float(gains / losses)


def rank_wallets(as_of: Optional[datetime] = None) -> Dict[str, object]:
    cfg = get_config()
    rk = cfg.ranking
    w = rk.weights
    B = int(rk.bootstrap_iterations)
    rng = np.random.default_rng(cfg.seed)

    with session_scope() as session:
        met = pd.read_sql(
            select(WalletMetric.__table__).where(WalletMetric.eligible == True),  # noqa: E712
            session.bind,
        )
        if met.empty:
            return {"ranked": 0, "note": "no eligible wallets"}
        if as_of is None:
            as_of = pd.to_datetime(met["as_of"]).max().to_pydatetime()

        # --- population percentilers (built on point estimates of eligibles) ---
        P = {
            "total_profit": _Percentiler(_winsorize(met["total_profit_usd"].values, rk.winsorize_pct)),
            "avg_roi": _Percentiler(_winsorize(met["avg_roi"].values, rk.winsorize_pct)),
            "profit_factor": _Percentiler(_winsorize(met["profit_factor"].values, rk.winsorize_pct)),
            "win_rate": _Percentiler(met["win_rate"].values),
            "median_roi": _Percentiler(_winsorize(met["median_roi"].values, rk.winsorize_pct)),
            "roi_stability": _Percentiler(met["roi_stability"].values),
            "positive_month_rate": _Percentiler(met["positive_month_rate"].values),
            "max_drawdown": _Percentiler(met["max_drawdown"].values, invert=True),
            "sharpe": _Percentiler(_winsorize(met["sharpe"].values, rk.winsorize_pct)),
            "active_days": _Percentiler(met["active_days"].values),
            "trade_count": _Percentiler(met["n_positions"].values),
        }

        def composite(prof, win, cons, risk, longev):
            return 100.0 * (w["profitability"] * prof + w["win_rate"] * win
                            + w["consistency"] * cons + w["risk"] * risk
                            + w["longevity"] * longev)

        # --- point-estimate sub-scores ---
        pr = rk.profitability
        prof = (pr["total_profit"] * P["total_profit"](met["total_profit_usd"])
                + pr["avg_roi"] * P["avg_roi"](met["avg_roi"])
                + pr["profit_factor"] * P["profit_factor"](met["profit_factor"]))
        win = P["win_rate"](met["win_rate"])
        co = rk.consistency
        cons = (co["median_roi"] * P["median_roi"](met["median_roi"])
                + co["roi_stability"] * P["roi_stability"](met["roi_stability"])
                + co["positive_month_rate"] * P["positive_month_rate"](met["positive_month_rate"]))
        ri = rk.risk
        risk = (ri["max_drawdown"] * P["max_drawdown"](met["max_drawdown"])
                + ri["sharpe"] * P["sharpe"](met["sharpe"]))
        lo = rk.longevity
        longev = (lo["active_days"] * P["active_days"](met["active_days"])
                  + lo["trade_count"] * P["trade_count"](met["n_positions"]))

        met = met.assign(
            profitability_score=100 * prof, win_rate_score=100 * win,
            consistency_score=100 * cons, risk_score=100 * risk,
            longevity_score=100 * longev,
            composite_score=composite(prof, win, cons, risk, longev),
        )

        # --- bootstrap CIs over each wallet's own positions ---
        pos = pd.read_sql(
            select(Position.__table__.c.wallet_address, Position.__table__.c.realized_pnl_usd,
                   Position.__table__.c.roi, Position.__table__.c.is_win,
                   Position.__table__.c.status), session.bind)
        pos = pos[pos["status"].isin(["settled", "closed"])]
        by_wallet = {a: g for a, g in pos.groupby("wallet_address")}

        ci_low = np.zeros(len(met)); ci_high = np.zeros(len(met))
        # fixed-within-bootstrap component percentiles (structural metrics)
        cons_fixed = (co["roi_stability"] * np.asarray(P["roi_stability"](met["roi_stability"]))
                      + co["positive_month_rate"] * np.asarray(P["positive_month_rate"](met["positive_month_rate"])))
        risk_pt = np.asarray(risk)
        long_pt = np.asarray(longev)

        for i, (_, row) in enumerate(met.iterrows()):
            g = by_wallet.get(row["wallet_address"])
            if g is None or len(g) < 3:
                ci_low[i] = ci_high[i] = row["composite_score"]; continue
            pnl = g["realized_pnl_usd"].to_numpy(float)
            roi = g["roi"].to_numpy(float)
            iswin = (g["is_win"] == True).to_numpy(float)  # noqa: E712
            n = len(g)
            idx = rng.integers(0, n, size=(B, n))
            tp_bs = pnl[idx].sum(1)
            avgroi_bs = roi[idx].mean(1)
            win_bs = iswin[idx].mean(1)
            medroi_bs = np.median(roi[idx], axis=1)
            pf_bs = np.array([_profit_factor(pnl[idx[b]]) for b in range(B)])

            prof_bs = (pr["total_profit"] * P["total_profit"](tp_bs)
                       + pr["avg_roi"] * P["avg_roi"](avgroi_bs)
                       + pr["profit_factor"] * P["profit_factor"](pf_bs))
            win_s_bs = P["win_rate"](win_bs)
            cons_bs = co["median_roi"] * P["median_roi"](medroi_bs) + cons_fixed[i]
            comp_bs = composite(prof_bs, win_s_bs, cons_bs, risk_pt[i], long_pt[i])
            ci_low[i], ci_high[i] = np.percentile(comp_bs, [5, 95])

        met = met.assign(score_ci_low=ci_low, score_ci_high=ci_high)
        met = met.sort_values("composite_score", ascending=False).reset_index(drop=True)
        met["rank"] = np.arange(1, len(met) + 1)

        # --- persist ---
        session.execute(delete(WalletScore))
        session.bulk_insert_mappings(WalletScore, [
            dict(wallet_address=r["wallet_address"], as_of=as_of,
                 composite_score=float(r["composite_score"]), rank=int(r["rank"]),
                 profitability_score=float(r["profitability_score"]),
                 win_rate_score=float(r["win_rate_score"]),
                 consistency_score=float(r["consistency_score"]),
                 risk_score=float(r["risk_score"]), longevity_score=float(r["longevity_score"]),
                 score_ci_low=float(r["score_ci_low"]), score_ci_high=float(r["score_ci_high"]))
            for _, r in met.iterrows()])

        return {"ranked": len(met), "as_of": as_of.isoformat(),
                "top10": met.head(10)[["wallet_address", "composite_score",
                                       "score_ci_low", "score_ci_high"]].to_dict("records")}


def get_top_wallets(n: int = 100, as_of: Optional[datetime] = None) -> pd.DataFrame:
    """Convenience: load the top-N ranked wallets joined with their metrics."""
    with session_scope() as session:
        q = select(WalletScore.__table__).order_by(WalletScore.rank).limit(n)
        scores = pd.read_sql(q, session.bind)
        met = pd.read_sql(select(WalletMetric.__table__), session.bind)
    return scores.merge(met, on="wallet_address", how="left", suffixes=("", "_m"))
