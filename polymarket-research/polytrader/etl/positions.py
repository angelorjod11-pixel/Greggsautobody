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
    """Rebuild the positions table from trades. Idempotent (truncates first)."""
    with session_scope() as session:
        trades, markets = _load_frames(session)
        if trades.empty:
            return {"positions": 0}

        m = markets.set_index("id")
        trades = trades.sort_values("timestamp")
        # signed shares: +buy / -sell
        trades["signed_shares"] = np.where(trades["side"] == "BUY",
                                           trades["shares"], -trades["shares"])
        rows = []
        grp = trades.groupby(["wallet_address", "market_id", "outcome_index"], sort=False)
        for (wallet, market_id, outcome), g in grp:
            if market_id not in m.index:
                continue
            mk = m.loc[market_id]
            buys = g[g["side"] == "BUY"]
            sells = g[g["side"] == "SELL"]
            bought_shares = float(buys["shares"].sum())
            if bought_shares <= 0:
                continue
            cost_basis = float(buys["usd_size"].sum())
            sold_shares = float(sells["shares"].sum())
            proceeds = float(sells["usd_size"].sum())
            net_shares = max(0.0, bought_shares - sold_shares)
            avg_entry = cost_basis / bought_shares

            resolved = bool(mk["resolved"])
            opened_at = g["timestamp"].iloc[0]
            if resolved:
                payout = 1.0 if int(outcome) == int(mk["winning_outcome"]) else 0.0
                settlement = net_shares * payout
                # closed at last sell if fully exited before resolution, else at resolution
                if net_shares <= 1e-9 and not sells.empty:
                    closed_at, status = sells["timestamp"].iloc[-1], "closed"
                else:
                    closed_at, status = mk["resolved_at"], "settled"
                realized = proceeds + settlement - cost_basis
                roi = realized / cost_basis if cost_basis > 0 else 0.0
                is_win = bool(realized > 0)
            else:
                settlement, realized, roi, is_win = 0.0, 0.0, 0.0, None
                closed_at, status = None, "open"

            dur_h = 0.0
            if closed_at is not None and pd.notna(closed_at):
                dur_h = max(0.0, (pd.Timestamp(closed_at) - pd.Timestamp(opened_at))
                            .total_seconds() / 3600.0)

            rows.append(dict(
                wallet_address=wallet, market_id=market_id, outcome_index=int(outcome),
                opened_at=pd.Timestamp(opened_at).to_pydatetime(),
                closed_at=(pd.Timestamp(closed_at).to_pydatetime()
                           if closed_at is not None and pd.notna(closed_at) else None),
                shares=bought_shares, avg_entry_price=avg_entry, cost_basis_usd=cost_basis,
                proceeds_usd=proceeds, settlement_usd=settlement, realized_pnl_usd=realized,
                roi=roi, duration_hours=dur_h, status=status, is_win=is_win,
            ))

        session.execute(delete(Position))
        session.bulk_insert_mappings(Position, rows)
        return {"positions": len(rows)}
