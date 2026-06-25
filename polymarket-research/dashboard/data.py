"""Data-access layer for the Streamlit dashboard.

These functions read **directly** from the research database (via the
``polytrader`` ORM / pandas ``read_sql``) and from the ``analysis_artifacts``
JSON store. They are intentionally free of any Streamlit imports so they can be
unit-tested / imported on their own, and so the dashboard can wrap them in
``st.cache_data`` without coupling the cache to the query logic.

Every artifact getter returns ``None`` when the artifact is missing so the UI
can show a friendly "not available — run the pipeline" message instead of
crashing.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd
from sqlalchemy import select

from polytrader.config import get_config
from polytrader.db.models import (
    AnalysisArtifact,
    WalletCluster,
    WalletMetric,
    WalletPair,
    WalletScore,
)
from polytrader.db.session import session_scope


# --------------------------------------------------------------------------- #
# Artifact getters
# --------------------------------------------------------------------------- #
def get_artifact(kind: str, name: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Latest payload for ``kind`` (optionally filtered by ``name``), or
    ``None`` if no such artifact exists."""
    with session_scope() as session:
        stmt = select(AnalysisArtifact).where(AnalysisArtifact.kind == kind)
        if name is not None:
            stmt = stmt.where(AnalysisArtifact.name == name)
        stmt = stmt.order_by(AnalysisArtifact.as_of.desc(), AnalysisArtifact.id.desc())
        row = session.scalars(stmt).first()
        return dict(row.payload) if row is not None else None


def get_artifacts(kind: str) -> Dict[str, Dict[str, Any]]:
    """All artifacts of a kind as ``{name: payload}`` (latest run wins)."""
    out: Dict[str, Dict[str, Any]] = {}
    with session_scope() as session:
        stmt = (
            select(AnalysisArtifact)
            .where(AnalysisArtifact.kind == kind)
            .order_by(AnalysisArtifact.as_of.asc(), AnalysisArtifact.id.asc())
        )
        for row in session.scalars(stmt):
            out[row.name] = dict(row.payload)
    return out


# --------------------------------------------------------------------------- #
# Tabular getters
# --------------------------------------------------------------------------- #
def get_overview_stats() -> Dict[str, Any]:
    """Headline counts for the Overview page."""
    cfg = get_config()
    with session_scope() as session:
        n_wallets = session.query(WalletMetric).count()
        n_eligible = (
            session.query(WalletMetric)
            .filter(WalletMetric.eligible.is_(True))
            .count()
        )
        n_ranked = session.query(WalletScore).count()
        as_of = session.scalar(
            select(WalletScore.as_of).order_by(WalletScore.as_of.desc()).limit(1)
        )
    return {
        "n_wallets": n_wallets,
        "n_eligible": n_eligible,
        "n_ranked": n_ranked,
        "data_source": cfg.data_source,
        "as_of": as_of.isoformat() if as_of is not None else None,
    }


def get_top_wallets(n: int = 100) -> pd.DataFrame:
    """Top-``n`` wallets: ``wallet_scores`` joined to ``wallet_metrics``.

    Returns the full joined frame (score components + every metric column) so
    the UI can pick whatever it needs. ``wallet_address`` is preserved; the
    duplicate metrics ``as_of`` is suffixed ``_m`` by the merge.
    """
    with session_scope() as session:
        scores = pd.read_sql(
            select(WalletScore.__table__).order_by(WalletScore.rank).limit(n),
            session.bind,
        )
        if scores.empty:
            return scores
        met = pd.read_sql(select(WalletMetric.__table__), session.bind)
    df = scores.merge(met, on="wallet_address", how="left", suffixes=("", "_m"))
    return df.sort_values("rank").reset_index(drop=True)


def get_wallet_clusters() -> pd.DataFrame:
    """All per-wallet cluster rows (kmeans/hierarchical/pca/community)."""
    with session_scope() as session:
        return pd.read_sql(select(WalletCluster.__table__), session.bind)


def get_wallet_pairs(limit: int = 1000) -> pd.DataFrame:
    """Strongest co-trading edges by ``timing_jaccard`` (capped at ``limit``)."""
    with session_scope() as session:
        return pd.read_sql(
            select(WalletPair.__table__)
            .order_by(WalletPair.timing_jaccard.desc())
            .limit(limit),
            session.bind,
        )


def get_score_components(df_top: pd.DataFrame, address: str) -> Dict[str, float]:
    """Pull a wallet's five score components from an already-loaded top frame."""
    row = df_top.loc[df_top["wallet_address"] == address]
    if row.empty:
        return {}
    r = row.iloc[0]
    return {
        "Profitability": float(r.get("profitability_score", 0.0)),
        "Win rate": float(r.get("win_rate_score", 0.0)),
        "Consistency": float(r.get("consistency_score", 0.0)),
        "Risk": float(r.get("risk_score", 0.0)),
        "Longevity": float(r.get("longevity_score", 0.0)),
    }


# --------------------------------------------------------------------------- #
# Helpers shared by several pages
# --------------------------------------------------------------------------- #
def short_addr(addr: str, head: int = 6, tail: int = 4) -> str:
    """``0x94b6…d911`` style abbreviation for compact labels."""
    if not isinstance(addr, str) or len(addr) <= head + tail + 1:
        return str(addr)
    return f"{addr[:head]}…{addr[-tail:]}"


def category_alpha_to_frame(by_category: Dict[str, Dict[str, Any]]) -> pd.DataFrame:
    """Turn a ``{category: stats}`` dict (elite_by_category / field_by_category)
    into a tidy DataFrame with ``category`` as a column."""
    if not by_category:
        return pd.DataFrame()
    rows: List[Dict[str, Any]] = []
    for cat, stats in by_category.items():
        row = {"category": cat}
        row.update(stats)
        rows.append(row)
    return pd.DataFrame(rows)
