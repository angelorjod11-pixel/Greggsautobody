"""SQLAlchemy ORM models — the canonical schema.

Design notes
------------
* Engine-agnostic: every column type used here exists on both SQLite (dev/test
  default) and PostgreSQL (production via ``DATABASE_URL``).  ``JSON`` is used
  for flexible nested payloads (equity curves, centroids, Monte-Carlo summaries).
* The raw event tables (``wallets``, ``markets``, ``trades``) are populated by
  collectors / the synthetic generator.  Everything else is *derived* and is
  rebuilt idempotently by the ETL + analytics stages, so an analysis run can be
  reproduced from the raw events alone.
* Money is stored in USD floats.  Polymarket shares are denominated in USDC and
  every outcome token settles at exactly $1 (win) or $0 (loss), which is what
  makes ROI / cost-basis accounting clean.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------- #
# Raw event layer
# --------------------------------------------------------------------------- #
class Wallet(Base):
    __tablename__ = "wallets"

    address: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    first_seen: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_seen: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    trades: Mapped[list["Trade"]] = relationship(back_populates="wallet")
    positions: Mapped[list["Position"]] = relationship(back_populates="wallet")


class Market(Base):
    __tablename__ = "markets"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)  # condition_id / market id
    slug: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    question: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    category: Mapped[Optional[str]] = mapped_column(String(40), index=True, nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    end_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # index of the winning outcome (0/1 for binary markets); NULL if unresolved
    winning_outcome: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    volume_usd: Mapped[float] = mapped_column(Float, default=0.0)
    liquidity_usd: Mapped[float] = mapped_column(Float, default=0.0)

    trades: Mapped[list["Trade"]] = relationship(back_populates="market")


class Trade(Base):
    """A single fill: wallet buys/sells `shares` of an outcome token at `price`."""

    __tablename__ = "trades"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)  # tx_hash:logindex or synthetic id
    wallet_address: Mapped[str] = mapped_column(ForeignKey("wallets.address"), index=True)
    market_id: Mapped[str] = mapped_column(ForeignKey("markets.id"), index=True)
    outcome_index: Mapped[int] = mapped_column(Integer, default=0)  # which outcome token
    side: Mapped[str] = mapped_column(String(4))  # BUY | SELL
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    price: Mapped[float] = mapped_column(Float)        # implied probability paid, in [0,1]
    shares: Mapped[float] = mapped_column(Float)       # outcome tokens, settle at $1 or $0
    usd_size: Mapped[float] = mapped_column(Float)     # price * shares
    tx_hash: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)

    wallet: Mapped["Wallet"] = relationship(back_populates="trades")
    market: Mapped["Market"] = relationship(back_populates="trades")

    __table_args__ = (
        Index("ix_trades_wallet_market", "wallet_address", "market_id"),
        Index("ix_trades_market_ts", "market_id", "timestamp"),
    )


# --------------------------------------------------------------------------- #
# Derived layer — round-trip positions
# --------------------------------------------------------------------------- #
class Position(Base):
    """A wallet's net exposure to one outcome of one market, closed by either an
    offsetting sell or by market resolution.  This is the unit of P&L / ROI."""

    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wallet_address: Mapped[str] = mapped_column(ForeignKey("wallets.address"), index=True)
    market_id: Mapped[str] = mapped_column(ForeignKey("markets.id"), index=True)
    outcome_index: Mapped[int] = mapped_column(Integer, default=0)

    opened_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    shares: Mapped[float] = mapped_column(Float)            # peak shares held (long)
    avg_entry_price: Mapped[float] = mapped_column(Float)   # cost-weighted entry prob
    cost_basis_usd: Mapped[float] = mapped_column(Float)    # total $ paid to enter
    proceeds_usd: Mapped[float] = mapped_column(Float, default=0.0)       # $ from selling early
    settlement_usd: Mapped[float] = mapped_column(Float, default=0.0)     # $ from resolution payout
    realized_pnl_usd: Mapped[float] = mapped_column(Float, default=0.0)
    roi: Mapped[float] = mapped_column(Float, default=0.0)  # pnl / cost_basis
    duration_hours: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(10), default="open")  # open|closed|settled
    is_win: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    wallet: Mapped["Wallet"] = relationship(back_populates="positions")

    __table_args__ = (
        UniqueConstraint("wallet_address", "market_id", "outcome_index", "opened_at",
                         name="uq_position"),
        Index("ix_pos_wallet_status", "wallet_address", "status"),
    )


# --------------------------------------------------------------------------- #
# Analytics outputs
# --------------------------------------------------------------------------- #
class WalletMetric(Base):
    """One row per wallet per analysis run — the full metric vector."""

    __tablename__ = "wallet_metrics"

    wallet_address: Mapped[str] = mapped_column(ForeignKey("wallets.address"), primary_key=True)
    as_of: Mapped[datetime] = mapped_column(DateTime, primary_key=True)

    # activity
    n_positions: Mapped[int] = mapped_column(Integer, default=0)
    n_markets: Mapped[int] = mapped_column(Integer, default=0)
    active_days: Mapped[float] = mapped_column(Float, default=0.0)
    trades_per_active_month: Mapped[float] = mapped_column(Float, default=0.0)
    capital_deployed_usd: Mapped[float] = mapped_column(Float, default=0.0)

    # profitability
    total_profit_usd: Mapped[float] = mapped_column(Float, default=0.0)
    avg_roi: Mapped[float] = mapped_column(Float, default=0.0)
    median_roi: Mapped[float] = mapped_column(Float, default=0.0)
    profit_factor: Mapped[float] = mapped_column(Float, default=0.0)
    win_rate: Mapped[float] = mapped_column(Float, default=0.0)

    # risk / consistency
    sharpe: Mapped[float] = mapped_column(Float, default=0.0)
    max_drawdown: Mapped[float] = mapped_column(Float, default=0.0)  # fraction in [0,1]
    roi_stability: Mapped[float] = mapped_column(Float, default=0.0)
    positive_month_rate: Mapped[float] = mapped_column(Float, default=0.0)

    # behavioral (also feed clustering)
    avg_position_size_usd: Mapped[float] = mapped_column(Float, default=0.0)
    position_size_cv: Mapped[float] = mapped_column(Float, default=0.0)
    median_hold_hours: Mapped[float] = mapped_column(Float, default=0.0)
    entry_lead_days: Mapped[float] = mapped_column(Float, default=0.0)
    markets_per_active_month: Mapped[float] = mapped_column(Float, default=0.0)
    category_concentration: Mapped[float] = mapped_column(Float, default=0.0)
    avg_entry_price: Mapped[float] = mapped_column(Float, default=0.0)
    top_category: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)

    eligible: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)  # category exposure, monthly pnl, etc.


class WalletScore(Base):
    __tablename__ = "wallet_scores"

    wallet_address: Mapped[str] = mapped_column(ForeignKey("wallets.address"), primary_key=True)
    as_of: Mapped[datetime] = mapped_column(DateTime, primary_key=True)

    composite_score: Mapped[float] = mapped_column(Float, index=True)
    rank: Mapped[int] = mapped_column(Integer, index=True)
    profitability_score: Mapped[float] = mapped_column(Float, default=0.0)
    win_rate_score: Mapped[float] = mapped_column(Float, default=0.0)
    consistency_score: Mapped[float] = mapped_column(Float, default=0.0)
    risk_score: Mapped[float] = mapped_column(Float, default=0.0)
    longevity_score: Mapped[float] = mapped_column(Float, default=0.0)
    score_ci_low: Mapped[float] = mapped_column(Float, default=0.0)
    score_ci_high: Mapped[float] = mapped_column(Float, default=0.0)


class WalletCluster(Base):
    __tablename__ = "wallet_clusters"

    wallet_address: Mapped[str] = mapped_column(ForeignKey("wallets.address"), primary_key=True)
    as_of: Mapped[datetime] = mapped_column(DateTime, primary_key=True)
    kmeans_cluster: Mapped[int] = mapped_column(Integer, default=-1)
    hierarchical_cluster: Mapped[int] = mapped_column(Integer, default=-1)
    pca_x: Mapped[float] = mapped_column(Float, default=0.0)
    pca_y: Mapped[float] = mapped_column(Float, default=0.0)
    community: Mapped[int] = mapped_column(Integer, default=-1)


class WalletPair(Base):
    """Pairwise relationship metrics over the correlation universe."""

    __tablename__ = "wallet_pairs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    as_of: Mapped[datetime] = mapped_column(DateTime, index=True)
    wallet_a: Mapped[str] = mapped_column(String(64), index=True)
    wallet_b: Mapped[str] = mapped_column(String(64), index=True)
    n_shared_markets: Mapped[int] = mapped_column(Integer, default=0)
    market_overlap: Mapped[float] = mapped_column(Float, default=0.0)   # Jaccard over markets
    timing_jaccard: Mapped[float] = mapped_column(Float, default=0.0)   # co-entry within window
    direction_corr: Mapped[float] = mapped_column(Float, default=0.0)   # same-side agreement
    profit_corr: Mapped[float] = mapped_column(Float, default=0.0)      # monthly pnl correlation
    lead_lag_hours: Mapped[float] = mapped_column(Float, default=0.0)   # +ve => a leads b

    __table_args__ = (Index("ix_pair_ab", "wallet_a", "wallet_b"),)


class AnalysisArtifact(Base):
    """Generic JSON store for run-level outputs that don't merit a wide table:
    cluster profiles, backtest results, signal/odds tables, category alpha,
    Monte-Carlo summaries, leader-follower lists, run metadata, etc."""

    __tablename__ = "analysis_artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    as_of: Mapped[datetime] = mapped_column(DateTime, index=True)
    kind: Mapped[str] = mapped_column(String(48), index=True)   # e.g. "backtest", "category_alpha"
    name: Mapped[str] = mapped_column(String(80))               # e.g. "strategy_A", "Politics"
    payload: Mapped[dict] = mapped_column(JSON, default=dict)

    __table_args__ = (Index("ix_artifact_kind_name", "kind", "name"),)


ALL_TABLES = [
    Wallet, Market, Trade, Position, WalletMetric, WalletScore,
    WalletCluster, WalletPair, AnalysisArtifact,
]
