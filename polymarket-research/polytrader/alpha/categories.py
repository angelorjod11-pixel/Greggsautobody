"""Alpha discovery by market category.

Where does the edge live?  For each category (Politics, Sports, Crypto,
Economics, News) we measure realized performance over resolved positions, for
the **field** and for the **elite** (top-ranked wallets), and surface:

* average / median ROI and win rate,
* ROI dispersion (variance) — the *repeatability* / risk of the niche,
* a risk-adjusted ratio (avg_roi / roi_std),
* **profit concentration** — the share of category profit captured by the top
  decile of wallets (a high share ⇒ alpha is concentrated in few hands).

From these we name the most profitable, most repeatable (highest win rate) and
lowest-variance niches.
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from sqlalchemy import select

from ..config import get_config
from ..db.models import AnalysisArtifact, Market, Position, WalletScore
from ..db.session import session_scope


def _top_decile_profit_share(g: pd.DataFrame) -> float:
    """Share of (positive) category profit captured by the top-decile wallets."""
    by_wallet = g.groupby("wallet_address")["realized_pnl_usd"].sum().sort_values(ascending=False)
    total = by_wallet[by_wallet > 0].sum()
    if total <= 0:
        return 0.0
    k = max(1, int(np.ceil(len(by_wallet) * 0.1)))
    return float(by_wallet.head(k).clip(lower=0).sum() / total)


def _cat_stats(g: pd.DataFrame) -> Dict[str, float]:
    roi = g["roi"].to_numpy(float)
    pnl = g["realized_pnl_usd"].to_numpy(float)
    std = float(np.std(roi, ddof=1)) if len(roi) > 1 else 0.0
    return {
        "n_positions": int(len(g)),
        "n_wallets": int(g["wallet_address"].nunique()),
        "avg_roi": round(float(np.mean(roi)), 4),
        "median_roi": round(float(np.median(roi)), 4),
        "win_rate": round(float((g["is_win"] == True).mean()), 4),  # noqa: E712
        "total_profit_usd": round(float(pnl.sum()), 2),
        "roi_std": round(std, 4),
        "roi_per_unit_risk": round(float(np.mean(roi) / std), 4) if std > 1e-9 else 0.0,
        "top_decile_profit_share": round(_top_decile_profit_share(g), 4),
    }


def analyze_categories(as_of: Optional[datetime] = None) -> Dict[str, object]:
    cfg = get_config()
    top_n = int(cfg.correlation["top_n"])

    with session_scope() as session:
        pos = pd.read_sql(select(
            Position.__table__.c.wallet_address, Position.__table__.c.market_id,
            Position.__table__.c.roi, Position.__table__.c.realized_pnl_usd,
            Position.__table__.c.is_win, Position.__table__.c.status), session.bind)
        mk = pd.read_sql(select(Market.__table__.c.id, Market.__table__.c.category)
                         .where(True), session.bind).rename(columns={"id": "market_id"})
        top = set(pd.read_sql(select(WalletScore.__table__.c.wallet_address)
                              .order_by(WalletScore.rank).limit(top_n), session.bind)["wallet_address"])
        if pos.empty:
            return {"note": "no positions"}
        if as_of is None:
            as_of = pd.to_datetime(session.scalar(select(WalletScore.as_of).limit(1)) or
                                   datetime.utcnow()).to_pydatetime()

        pos = pos[pos["status"].isin(["settled", "closed"])].merge(mk, on="market_id", how="left")
        pos["is_elite"] = pos["wallet_address"].isin(top)

        def _by_cat(sub):
            return {str(cat): _cat_stats(g) for cat, g in sub.groupby("category", observed=True)
                    if cat is not None}

        field = _by_cat(pos)
        elite = _by_cat(pos[pos.is_elite])

        # rank niches by the elite view (where alpha actually is)
        prof = {c: s for c, s in elite.items() if s["n_positions"] >= 30}
        most_profitable = max(prof, key=lambda c: prof[c]["avg_roi"]) if prof else None
        most_repeatable = max(prof, key=lambda c: prof[c]["win_rate"]) if prof else None
        # lowest variance among profitable niches
        prof_pos = {c: s for c, s in prof.items() if s["avg_roi"] > 0}
        lowest_variance = (min(prof_pos, key=lambda c: prof_pos[c]["roi_std"])
                           if prof_pos else None)
        best_risk_adjusted = (max(prof_pos, key=lambda c: prof_pos[c]["roi_per_unit_risk"])
                              if prof_pos else None)

        payload = {
            "as_of": as_of.isoformat(),
            "field_by_category": field,
            "elite_by_category": elite,
            "rankings": {
                "most_profitable_niche": most_profitable,
                "most_repeatable_niche": most_repeatable,
                "lowest_variance_profitable_niche": lowest_variance,
                "best_risk_adjusted_niche": best_risk_adjusted,
            },
        }
        session.add(AnalysisArtifact(as_of=as_of, kind="category_alpha", name="by_category", payload=payload))
        return {"rankings": payload["rankings"],
                "elite_categories": {c: {"avg_roi": s["avg_roi"], "win_rate": s["win_rate"],
                                         "n": s["n_positions"]} for c, s in elite.items()}}
