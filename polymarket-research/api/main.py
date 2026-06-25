"""Read-only REST API over the Polymarket research database + analysis artifacts.

This service exposes the derived analytics (rankings, clusters, correlation
network, copy-trading backtests, small-account sizing, entry signals and
category alpha) as plain JSON so the Streamlit dashboard — or any external
client — can consume them without touching SQLAlchemy directly.

It is **read-only**: every endpoint is a ``GET`` that loads rows / artifacts and
serialises them. Nothing here writes to the database. All data is produced
offline by ``python -m polytrader.cli all``.

Run it with::

    export POLYTRADER_DATABASE_URL="sqlite:////abs/path/to/data/polytrader.db"
    export PYTHONPATH=/abs/path/to/polymarket-research
    uvicorn api.main:app --reload --port 8000

Then browse the interactive docs at http://localhost:8000/docs.
"""
from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
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

app = FastAPI(
    title="Polymarket Trader Research API",
    description=(
        "Read-only JSON API over the populated research database: wallet "
        "rankings, behavioral clusters, the correlation network, copy-trading "
        "backtests, small-account Kelly sizing, entry signals and category alpha."
    ),
    version="1.0.0",
)


# --------------------------------------------------------------------------- #
# Serialisation helpers
# --------------------------------------------------------------------------- #
def _clean(obj: Any) -> Any:
    """Recursively make a value strictly JSON-safe.

    pandas/NumPy hand us ``NaN``/``inf`` floats and ``Timestamp``/``numpy``
    scalars that the default JSON encoder either rejects (NaN -> invalid JSON
    for strict clients) or cannot serialise. We normalise everything to native
    Python types and replace non-finite floats with ``None``.
    """
    # Fast path for the common scalar cases.
    if obj is None or isinstance(obj, (str, bool, int)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    # Timestamps / datetimes / dates all stringify to ISO-8601.
    if isinstance(obj, (pd.Timestamp, datetime, date)):
        return obj.isoformat()
    if hasattr(obj, "item"):
        try:
            return _clean(obj.item())
        except Exception:  # pragma: no cover - defensive
            return str(obj)
    if isinstance(obj, pd.Series):
        return _clean(obj.to_dict())
    return obj


def _df_records(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """Convert a DataFrame to a list of JSON-safe row dicts."""
    return _clean(df.to_dict(orient="records"))


def _get_artifact(kind: str, name: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Fetch the single most-recent artifact payload for ``kind`` (and optional
    ``name``). Returns ``None`` when no such artifact exists so callers can
    decide between 404 and an empty default."""
    with session_scope() as session:
        stmt = select(AnalysisArtifact).where(AnalysisArtifact.kind == kind)
        if name is not None:
            stmt = stmt.where(AnalysisArtifact.name == name)
        # newest first, in case multiple runs were appended
        stmt = stmt.order_by(AnalysisArtifact.as_of.desc(), AnalysisArtifact.id.desc())
        row = session.scalars(stmt).first()
        return dict(row.payload) if row is not None else None


def _get_artifacts(kind: str) -> Dict[str, Dict[str, Any]]:
    """Fetch all artifacts of a kind as ``{name: payload}`` (latest as_of wins
    when a name repeats)."""
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


def _latest_as_of() -> Optional[str]:
    """ISO string of the most recent scoring run, or ``None`` if unranked."""
    with session_scope() as session:
        val = session.scalar(
            select(WalletScore.as_of).order_by(WalletScore.as_of.desc()).limit(1)
        )
    return val.isoformat() if val is not None else None


# Columns surfaced by /wallets/top, in display order. Sourced from the
# wallet_scores ⋈ wallet_metrics join produced below.
_TOP_COLUMNS = [
    "rank",
    "address",
    "composite_score",
    "score_ci_low",
    "score_ci_high",
    "total_profit_usd",
    "avg_roi",
    "win_rate",
    "sharpe",
    "max_drawdown",
    "n_positions",
    "top_category",
]


def _top_wallets_df(n: int) -> pd.DataFrame:
    """Top-N wallets: scores joined to their metric vector, trimmed to the
    columns the dashboard/table needs and with ``wallet_address`` renamed to
    ``address``."""
    with session_scope() as session:
        scores = pd.read_sql(
            select(WalletScore.__table__).order_by(WalletScore.rank).limit(n),
            session.bind,
        )
        if scores.empty:
            return pd.DataFrame(columns=_TOP_COLUMNS)
        met = pd.read_sql(select(WalletMetric.__table__), session.bind)

    df = scores.merge(met, on="wallet_address", how="left", suffixes=("", "_m"))
    df = df.rename(columns={"wallet_address": "address"})
    keep = [c for c in _TOP_COLUMNS if c in df.columns]
    return df.sort_values("rank")[keep]


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", summary="Service + data-source health check")
def health() -> JSONResponse:
    """Liveness probe that also reports how much ranked data is loaded."""
    cfg = get_config()
    with session_scope() as session:
        n_ranked = session.query(WalletScore).count()
    return JSONResponse(
        _clean(
            {
                "status": "ok",
                "data_source": cfg.data_source,
                "as_of": _latest_as_of(),
                "n_wallets_ranked": n_ranked,
            }
        )
    )


@app.get("/wallets/top", summary="Top-N ranked wallets with headline metrics")
def wallets_top(
    n: int = Query(100, ge=1, le=1000, description="How many top wallets to return")
) -> JSONResponse:
    """Return the top-``n`` wallets by composite score, each with rank, the
    bootstrap CI and the headline profitability / risk metrics."""
    df = _top_wallets_df(n)
    return JSONResponse(_df_records(df))


@app.get("/wallets/{address}", summary="Full detail for a single wallet")
def wallet_detail(address: str) -> JSONResponse:
    """Return one wallet's full metric row, score row and cluster row.

    404s if the address has no metric row (i.e. is not in the analysed set).
    """
    with session_scope() as session:
        metric = _latest_row(session, WalletMetric, address)
        score = _latest_row(session, WalletScore, address)
        cluster = _latest_row(session, WalletCluster, address)

    if metric is None and score is None and cluster is None:
        raise HTTPException(status_code=404, detail=f"wallet {address} not found")

    return JSONResponse(
        _clean(
            {
                "address": address,
                "metric": _row_to_dict(metric),
                "score": _row_to_dict(score),
                "cluster": _row_to_dict(cluster),
            }
        )
    )


@app.get("/clusters", summary="K-means cluster profiles + per-wallet PCA coords")
def clusters() -> JSONResponse:
    """The ``cluster_profile/kmeans`` artifact (k, silhouette, per-cluster
    archetype profiles) plus the per-wallet PCA scatter coordinates from
    ``wallet_clusters``."""
    profile = _get_artifact("cluster_profile", "kmeans") or {}
    with session_scope() as session:
        coords = pd.read_sql(
            select(
                WalletCluster.__table__.c.wallet_address,
                WalletCluster.__table__.c.pca_x,
                WalletCluster.__table__.c.pca_y,
                WalletCluster.__table__.c.kmeans_cluster,
                WalletCluster.__table__.c.community,
            ),
            session.bind,
        ).rename(columns={"wallet_address": "address"})
    return JSONResponse(
        _clean({"profile": profile, "wallets": coords.to_dict(orient="records")})
    )


@app.get("/network", summary="Network summary + strongest co-trading edges")
def network() -> JSONResponse:
    """The ``network_summary`` artifact plus up to the 500 strongest
    ``wallet_pairs`` edges (by ``timing_jaccard``) for graph rendering."""
    summary = _get_artifact("network_summary", "top_wallets") or {}
    with session_scope() as session:
        edges = pd.read_sql(
            select(
                WalletPair.__table__.c.wallet_a,
                WalletPair.__table__.c.wallet_b,
                WalletPair.__table__.c.timing_jaccard,
                WalletPair.__table__.c.market_overlap,
                WalletPair.__table__.c.direction_corr,
                WalletPair.__table__.c.profit_corr,
                WalletPair.__table__.c.n_shared_markets,
                WalletPair.__table__.c.lead_lag_hours,
            )
            .order_by(WalletPair.timing_jaccard.desc())
            .limit(500),
            session.bind,
        )
    return JSONResponse(
        _clean({"summary": summary, "edges": edges.to_dict(orient="records")})
    )


@app.get("/leaders", summary="Leader/follower rankings")
def leaders() -> JSONResponse:
    """The ``leader_follower`` artifact payload (the leaders list)."""
    payload = _get_artifact("leader_follower", "leaders") or {"leaders": []}
    return JSONResponse(_clean(payload))


@app.get("/backtests", summary="Copy-trading backtest results")
def backtests() -> JSONResponse:
    """All four strategy payloads keyed by name, plus the run metadata.

    Shape: ``{"meta": <backtest_meta>, "strategies": {name: <payload>}}``.
    Each strategy payload includes its ``equity_curve`` for charting.
    """
    meta = _get_artifact("backtest_meta", "meta") or {}
    strategies = _get_artifacts("backtest")
    return JSONResponse(_clean({"meta": meta, "strategies": strategies}))


@app.get("/small-account", summary="$20 small-account Kelly sizing study")
def small_account() -> JSONResponse:
    """The ``small_account/summary`` artifact (recommended rule, Kelly
    fractions and the per-rule Monte-Carlo sizing tables)."""
    payload = _get_artifact("small_account", "summary") or {}
    return JSONResponse(_clean(payload))


@app.get("/signals", summary="Elite-vs-field entry-timing / odds signals")
def signals() -> JSONResponse:
    """The ``signals/entry_signals`` artifact (per-bucket elite vs field
    tables and the headline findings)."""
    payload = _get_artifact("signals", "entry_signals") or {}
    return JSONResponse(_clean(payload))


@app.get("/alpha", summary="Category alpha breakdown")
def alpha() -> JSONResponse:
    """The ``category_alpha/by_category`` artifact (elite & field per-category
    stats plus the niche rankings)."""
    payload = _get_artifact("category_alpha", "by_category") or {}
    return JSONResponse(_clean(payload))


# --------------------------------------------------------------------------- #
# Small ORM helpers (kept at the bottom; used by the detail endpoint)
# --------------------------------------------------------------------------- #
def _latest_row(session, model, address: str):
    """Return the most-recent row of ``model`` for ``address`` (these tables are
    keyed on the composite ``(wallet_address, as_of)``), or ``None`` if the
    wallet has no row. Avoids ``session.get`` so a missing wallet yields ``None``
    instead of an invalid-primary-key error."""
    return session.scalars(
        select(model)
        .where(model.wallet_address == address)
        .order_by(model.as_of.desc())
        .limit(1)
    ).first()


def _row_to_dict(obj) -> Optional[Dict[str, Any]]:
    """Turn a mapped ORM instance into a plain column dict (or ``None``)."""
    if obj is None:
        return None
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}
