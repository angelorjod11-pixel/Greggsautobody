"""Early-signal detection.

What do profitable trades have in common *at entry* — before the outcome is
known?  We slice resolved positions by features observable at entry time and
measure realized performance, comparing the **elite** (top-ranked wallets)
against the **field** (everyone else):

* **Entry timing** — days before resolution (do edges live early or late?).
* **Entry odds** — implied probability paid (longshots vs favorites).
* **Liquidity** — market volume bucket.
* **Realized edge** — ``payout − entry_price``: positive ⇒ the trader bought
  below fair value (exploited a mispricing).  Comparing elite vs field realized
  edge by timing/odds shows what the top traders "see before others".

No look-ahead: every slicing key (timing, odds, liquidity) is known at entry;
only the realized return is measured after the fact.
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


def _bucket_table(df: pd.DataFrame, by: str, edges: List[float], labels: List[str],
                  min_n: int) -> List[dict]:
    df = df.copy()
    df["bucket"] = pd.cut(df[by], bins=edges, labels=labels, include_lowest=True)
    rows = []
    for b, g in df.groupby("bucket", observed=True):
        if len(g) < min_n:
            continue
        rows.append({
            "bucket": str(b), "n": int(len(g)),
            "win_rate": round(float((g["is_win"] == True).mean()), 4),  # noqa: E712
            "avg_roi": round(float(g["roi"].mean()), 4),
            "median_roi": round(float(g["roi"].median()), 4),
            "avg_realized_edge": round(float(g["realized_edge"].mean()), 4),
        })
    return rows


def detect_signals(as_of: Optional[datetime] = None) -> Dict[str, object]:
    cfg = get_config()
    sg = cfg.signals
    top_n = int(cfg.correlation["top_n"])

    with session_scope() as session:
        pos = pd.read_sql(select(
            Position.__table__.c.wallet_address, Position.__table__.c.market_id,
            Position.__table__.c.outcome_index, Position.__table__.c.opened_at,
            Position.__table__.c.avg_entry_price, Position.__table__.c.roi,
            Position.__table__.c.is_win, Position.__table__.c.status), session.bind)
        mk = pd.read_sql(select(
            Market.__table__.c.id, Market.__table__.c.end_date, Market.__table__.c.category,
            Market.__table__.c.winning_outcome, Market.__table__.c.volume_usd), session.bind
            ).rename(columns={"id": "market_id"})
        top = set(pd.read_sql(select(WalletScore.__table__.c.wallet_address)
                              .order_by(WalletScore.rank).limit(top_n), session.bind)["wallet_address"])
        if pos.empty:
            return {"note": "no positions"}
        if as_of is None:
            as_of = pd.to_datetime(session.scalar(select(WalletScore.as_of).limit(1)) or
                                   datetime.utcnow()).to_pydatetime()

        pos = pos[pos["status"].isin(["settled", "closed"])].merge(mk, on="market_id", how="left")
        pos["opened_at"] = pd.to_datetime(pos["opened_at"])
        pos["end_date"] = pd.to_datetime(pos["end_date"])
        pos["entry_lead_days"] = ((pos["end_date"] - pos["opened_at"]).dt.total_seconds()
                                  / 86400.0).clip(lower=0)
        payout = (pos["outcome_index"] == pos["winning_outcome"]).astype(float)
        pos["realized_edge"] = payout - pos["avg_entry_price"]
        pos["is_elite"] = pos["wallet_address"].isin(top)

        lead_edges = list(sg["entry_buckets_days"])
        lead_labels = [f"{lead_edges[i]}-{lead_edges[i+1]}d" for i in range(len(lead_edges) - 1)]
        odds_edges = list(sg["odds_buckets"])
        odds_labels = [f"{odds_edges[i]:.2f}-{odds_edges[i+1]:.2f}" for i in range(len(odds_edges) - 1)]
        min_n = int(sg["min_positions_per_bucket"])

        out: Dict[str, object] = {"as_of": as_of.isoformat()}
        for who, sub in [("elite", pos[pos.is_elite]), ("field", pos[~pos.is_elite])]:
            out[who] = {
                "by_entry_timing": _bucket_table(sub, "entry_lead_days", lead_edges, lead_labels, min_n),
                "by_entry_odds": _bucket_table(sub, "avg_entry_price", odds_edges, odds_labels, min_n),
            }

        # headline relationships (Spearman, robust to nonlinearity)
        from scipy.stats import spearmanr
        el = pos[pos.is_elite]
        def _rho(a, b):
            if len(a) < 20 or a.std() < 1e-9:
                return None
            return round(float(spearmanr(a, b).correlation), 4)
        out["findings"] = {
            "elite_avg_entry_price": round(float(el["avg_entry_price"].mean()), 4),
            "field_avg_entry_price": round(float(pos[~pos.is_elite]["avg_entry_price"].mean()), 4),
            "elite_avg_realized_edge": round(float(el["realized_edge"].mean()), 4),
            "field_avg_realized_edge": round(float(pos[~pos.is_elite]["realized_edge"].mean()), 4),
            "elite_rho_lead_vs_roi": _rho(el["entry_lead_days"], el["roi"]),
            "elite_rho_entryprice_vs_roi": _rho(el["avg_entry_price"], el["roi"]),
            "interpretation": {
                "edge_lives_earlier": None,        # filled below
                "elite_exploit_mispricing": bool(el["realized_edge"].mean() >
                                                 pos[~pos.is_elite]["realized_edge"].mean()),
                "elite_prefer_longshots": bool(el["avg_entry_price"].mean() <
                                               pos[~pos.is_elite]["avg_entry_price"].mean()),
            },
        }
        rho_lead = out["findings"]["elite_rho_lead_vs_roi"]
        out["findings"]["interpretation"]["edge_lives_earlier"] = bool(rho_lead is not None and rho_lead > 0.03)

        session.add(AnalysisArtifact(as_of=as_of, kind="signals", name="entry_signals", payload=out))
        return {"elite_avg_realized_edge": out["findings"]["elite_avg_realized_edge"],
                "field_avg_realized_edge": out["findings"]["field_avg_realized_edge"],
                "edge_lives_earlier": out["findings"]["interpretation"]["edge_lives_earlier"],
                "elite_rho_lead_vs_roi": rho_lead}
