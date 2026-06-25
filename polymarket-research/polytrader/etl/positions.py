"""Reconstruct round-trip *positions* (the unit of P&L) from raw trades.

A position is a wallet's net exposure to one outcome of one market.  We net all
BUY/SELL fills for each ``(wallet, market, outcome)`` and settle any residual
shares at the market's resolved payout ($1 if the outcome won, else $0).

    realized_pnl = sell_proceeds + settlement_payout - buy_cost_basis
    roi          = realized_pnl / buy_cost_basis

Only *resolved* markets yield realized P&L; positions in still-open markets are
recorded with ``status='open'`` and excluded from performance metrics — this is
what keeps the analysis free of look-ahead / unrealized-gain bias.
"""
from __future__ import annotations

from typing import Dict

import numpy as np
import pandas as pd
from sqlalchemy import delete, select

from ..db.models import Market, Position, Trade
from ..db.session import session_scope


def _load_frames(session) -> tuple[pd.DataFrame, pd.DataFrame]:
    trades = pd.read_sql(select(Trade.__table__), session.bind)
    markets = pd.read_sql(select(Market.__table__), session.bind)
    return trades, markets


def build_positions() -> Dict[str, int]:
    """Rebuild the positions table from trades. Idempotent (truncates first).

    Fully vectorized: BUY/SELL fills are aggregated per (wallet, market, outcome)
    with groupby, then P&L / settlement / status are computed with array ops.
    """
    with session_scope() as session:
        trades, markets = _load_frames(session)
        if trades.empty:
            return {"positions": 0}

        keys = ["wallet_address", "market_id", "outcome_index"]
        trades["timestamp"] = pd.to_datetime(trades["timestamp"])
        is_buy = trades["side"] == "BUY"

        buys = (trades[is_buy].groupby(keys, sort=False)
                .agg(bought_shares=("shares", "sum"), cost_basis_usd=("usd_size", "sum"),
                     opened_at=("timestamp", "min")))
        sells = (trades[~is_buy].groupby(keys, sort=False)
                 .agg(sold_shares=("shares", "sum"), proceeds_usd=("usd_size", "sum"),
                      last_sell=("timestamp", "max")))
        p = buys.join(sells, how="left").reset_index()
        p["sold_shares"] = p["sold_shares"].fillna(0.0)
        p["proceeds_usd"] = p["proceeds_usd"].fillna(0.0)
        p = p[p["bought_shares"] > 0].copy()

        mk = markets[["id", "resolved", "resolved_at", "winning_outcome"]].rename(columns={"id": "market_id"})
        mk["resolved_at"] = pd.to_datetime(mk["resolved_at"])
        p = p.merge(mk, on="market_id", how="inner")

        p["avg_entry_price"] = p["cost_basis_usd"] / p["bought_shares"]
        p["shares"] = p["bought_shares"]
        p["net_shares"] = (p["bought_shares"] - p["sold_shares"]).clip(lower=0.0)
        resolved = p["resolved"].astype(bool)
        won = p["outcome_index"].astype("Int64") == p["winning_outcome"].astype("Int64")
        payout = (won & resolved).astype(float)
        p["settlement_usd"] = p["net_shares"] * payout
        fully_exited = (p["net_shares"] <= 1e-9) & (p["sold_shares"] > 0)

        # status: open (unresolved) | closed (exited before resolution) | settled (held to resolution)
        p["status"] = np.where(~resolved, "open", np.where(fully_exited, "closed", "settled"))
        p["closed_at"] = pd.NaT
        p.loc[resolved & fully_exited, "closed_at"] = p.loc[resolved & fully_exited, "last_sell"]
        p.loc[resolved & ~fully_exited, "closed_at"] = p.loc[resolved & ~fully_exited, "resolved_at"]

        p["realized_pnl_usd"] = np.where(
            resolved, p["proceeds_usd"] + p["settlement_usd"] - p["cost_basis_usd"], 0.0)
        p["roi"] = np.where(p["cost_basis_usd"] > 0,
                            p["realized_pnl_usd"] / p["cost_basis_usd"], 0.0)
        dur = (pd.to_datetime(p["closed_at"]) - p["opened_at"]).dt.total_seconds() / 3600.0
        p["duration_hours"] = dur.fillna(0.0).clip(lower=0.0)
        p["is_win"] = np.where(resolved, p["realized_pnl_usd"] > 0, None)

        cols = ["wallet_address", "market_id", "outcome_index", "opened_at", "closed_at",
                "shares", "avg_entry_price", "cost_basis_usd", "proceeds_usd",
                "settlement_usd", "realized_pnl_usd", "roi", "duration_hours", "status", "is_win"]
        out = p[cols].copy()
        out["closed_at"] = out["closed_at"].astype(object).where(out["closed_at"].notna(), None)
        records = out.to_dict("records")
        for r in records:  # normalize Na/NaT -> None for the is_win Boolean column
            if r["is_win"] is not None and not isinstance(r["is_win"], (bool, np.bool_)):
                r["is_win"] = None

        session.execute(delete(Position))
        session.bulk_insert_mappings(Position, records)
        return {"positions": len(records)}
