"""Aggregate resolved positions into the per-wallet metric vector.

Produces one :class:`WalletMetric` row per wallet, covering every quantity the
brief asks for (total profit, avg/median ROI, win rate, Sharpe, max drawdown,
profit factor, avg position size, #markets, frequency) plus the behavioral
features used by clustering/correlation, and an ``eligible`` flag enforcing the
minimum-activity filters that protect against small-sample noise.
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, Optional

import numpy as np
import pandas as pd
from sqlalchemy import delete, select

from ..config import get_config
from ..db.models import Market, Position, WalletMetric
from ..db.session import session_scope

MONTH = 30.4


# --------------------------------------------------------------------------- #
# small numeric helpers (kept explicit so the methodology is auditable)
# --------------------------------------------------------------------------- #
def _sharpe(roi: np.ndarray) -> float:
    """Per-position Sharpe = mean(ROI)/std(ROI). No annualization — positions are
    event-driven, not periodic — so this is a unitless reward-to-variability."""
    if len(roi) < 2:
        return 0.0
    sd = float(np.std(roi, ddof=1))
    return float(np.mean(roi) / sd) if sd > 1e-9 else 0.0


def _profit_factor(pnl: np.ndarray) -> float:
    gains = pnl[pnl > 0].sum()
    losses = -pnl[pnl < 0].sum()
    if losses <= 1e-9:
        return float(gains) if gains > 0 else 0.0  # all wins -> report gross gain magnitude
    return float(gains / losses)


def _max_drawdown(pnl_ordered: np.ndarray, capital: float) -> float:
    """Peak-to-trough drawdown of the equity curve (capital + cumulative pnl),
    returned as a fraction in [0,1]. Capital base keeps it well-defined even when
    early cumulative pnl is near zero."""
    if len(pnl_ordered) == 0 or capital <= 0:
        return 0.0
    equity = capital + np.cumsum(pnl_ordered)
    running_max = np.maximum.accumulate(np.concatenate([[capital], equity]))[1:]
    dd = (running_max - equity) / running_max
    return float(np.clip(np.max(dd), 0.0, 1.0))


def _cv(x: np.ndarray) -> float:
    mu = np.mean(x)
    if abs(mu) < 1e-9:
        return 0.0
    return float(np.std(x, ddof=1) / abs(mu)) if len(x) > 1 else 0.0


def _hhi(weights: np.ndarray) -> float:
    s = weights.sum()
    if s <= 0:
        return 0.0
    p = weights / s
    return float(np.sum(p ** 2))


def compute_metrics(as_of: Optional[datetime] = None) -> Dict[str, int]:
    cfg = get_config()
    elig = cfg.eligibility
    with session_scope() as session:
        pos = pd.read_sql(select(Position.__table__), session.bind)
        markets = pd.read_sql(
            select(Market.__table__.c.id, Market.__table__.c.category,
                   Market.__table__.c.end_date), session.bind
        ).rename(columns={"id": "market_id"})
        if pos.empty:
            return {"wallet_metrics": 0}

        # only resolved positions carry realized performance
        resolved = pos[pos["status"].isin(["settled", "closed"])].copy()
        resolved = resolved.merge(markets, on="market_id", how="left")
        for col in ("opened_at", "closed_at", "end_date"):
            resolved[col] = pd.to_datetime(resolved[col])

        if as_of is None:
            as_of = (pd.to_datetime(cfg.as_of) if cfg.as_of
                     else resolved["closed_at"].max()).to_pydatetime()

        rows = []
        for wallet, g in resolved.groupby("wallet_address", sort=False):
            g = g.sort_values("closed_at")
            pnl = g["realized_pnl_usd"].to_numpy(dtype=float)
            roi = g["roi"].to_numpy(dtype=float)
            cost = g["cost_basis_usd"].to_numpy(dtype=float)
            n = len(g)
            capital = float(cost.sum())
            first = g["opened_at"].min()
            last = g["closed_at"].max()
            active_days = max(1.0, (last - first).total_seconds() / 86400.0)
            active_months = max(active_days / MONTH, 1e-6)

            # monthly pnl (consistency + reused by correlation engine)
            gm = g.dropna(subset=["closed_at"]).copy()
            gm["ym"] = gm["closed_at"].dt.strftime("%Y-%m")
            monthly = gm.groupby("ym")["realized_pnl_usd"].sum()
            pos_month_rate = float((monthly > 0).mean()) if len(monthly) else 0.0
            roi_cv = _cv(monthly.to_numpy()) if len(monthly) > 1 else 0.0
            roi_stability = 1.0 / (1.0 + roi_cv)

            # category exposure (by capital)
            cat_cap = g.groupby("category")["cost_basis_usd"].sum()
            cat_share = (cat_cap / cat_cap.sum()).to_dict() if cat_cap.sum() > 0 else {}
            top_cat = max(cat_share, key=cat_share.get) if cat_share else None
            cat_conc = _hhi(cat_cap.to_numpy()) if len(cat_cap) else 0.0

            # entry lead vs resolution
            lead_days = ((g["end_date"] - g["opened_at"]).dt.total_seconds()
                         / 86400.0).clip(lower=0).mean()
            lead_days = float(lead_days) if pd.notna(lead_days) else 0.0

            n_markets = int(g["market_id"].nunique())
            metric = dict(
                wallet_address=wallet, as_of=as_of,
                n_positions=n, n_markets=n_markets, active_days=active_days,
                trades_per_active_month=float(n / active_months),
                capital_deployed_usd=capital,
                total_profit_usd=float(pnl.sum()),
                avg_roi=float(np.mean(roi)), median_roi=float(np.median(roi)),
                profit_factor=_profit_factor(pnl),
                win_rate=float((g["is_win"] == True).mean()),  # noqa: E712
                sharpe=_sharpe(roi),
                max_drawdown=_max_drawdown(pnl, capital),
                roi_stability=roi_stability, positive_month_rate=pos_month_rate,
                avg_position_size_usd=float(np.mean(cost)),
                position_size_cv=_cv(cost),
                median_hold_hours=float(np.median(g["duration_hours"].to_numpy())),
                entry_lead_days=lead_days,
                markets_per_active_month=float(n_markets / active_months),
                category_concentration=cat_conc,
                avg_entry_price=float(np.average(g["avg_entry_price"], weights=cost)
                                      if capital > 0 else g["avg_entry_price"].mean()),
                top_category=top_cat,
                extra={"category_exposure": cat_share,
                       "monthly_pnl": {k: float(v) for k, v in monthly.items()}},
            )
            # eligibility gate (anti-survivorship / anti-small-sample)
            single_frac = float(cost.max() / capital) if capital > 0 else 1.0
            metric["eligible"] = bool(
                n >= elig["min_resolved_positions"]
                and n_markets >= elig["min_distinct_markets"]
                and capital >= elig["min_capital_deployed_usd"]
                and active_days >= elig["min_active_days"]
                and single_frac <= elig["max_single_position_frac"]
            )
            rows.append(metric)

        session.execute(delete(WalletMetric))
        session.bulk_insert_mappings(WalletMetric, rows)
        n_elig = sum(1 for r in rows if r["eligible"])
        return {"wallet_metrics": len(rows), "eligible": n_elig, "as_of": as_of.isoformat()}
