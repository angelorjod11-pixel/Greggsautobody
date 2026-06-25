"""Data API client — historical trades & current positions.

Base: https://data-api.polymarket.com
Endpoints: ``GET /trades`` (by market or user), ``GET /positions?user=``.

Wallet discovery is market-centric: Polymarket has no "list all traders" route,
so we enumerate markets (via Gamma) and pull each market's trades here; the union
of ``proxyWallet`` values is our trader universe.  Normalizers are pure/testable.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from .rate_limit import HttpClient


def _ts_to_dt(ts: Any) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(int(float(ts)), tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OSError):
        return None


def normalize_trade(raw: Dict[str, Any], fallback_market_id: Optional[str] = None,
                    seq: int = 0) -> Optional[Dict[str, Any]]:
    """Map a Data-API trade object to the ``trades`` table schema."""
    wallet = (raw.get("proxyWallet") or raw.get("user") or raw.get("maker") or "").lower()
    market_id = raw.get("conditionId") or fallback_market_id
    ts = _ts_to_dt(raw.get("timestamp"))
    if not wallet or not market_id or ts is None:
        return None
    try:
        price = float(raw.get("price"))
        shares = float(raw.get("size"))
    except (TypeError, ValueError):
        return None
    side = str(raw.get("side", "BUY")).upper()
    side = "BUY" if side not in ("BUY", "SELL") else side
    outcome_index = raw.get("outcomeIndex")
    try:
        outcome_index = int(outcome_index)
    except (TypeError, ValueError):
        outcome_index = 0
    tx = raw.get("transactionHash") or raw.get("txHash") or ""
    asset = raw.get("asset") or ""
    trade_id = f"{tx}:{asset}:{int(raw.get('timestamp', 0))}:{side}:{seq}"
    return {
        "id": trade_id,
        "wallet_address": wallet,
        "market_id": str(market_id),
        "outcome_index": outcome_index,
        "side": side,
        "timestamp": ts,
        "price": price,
        "shares": shares,
        "usd_size": price * shares,
        "tx_hash": tx or None,
    }


def iter_market_trades(client: HttpClient, base: str, condition_id: str, *,
                       page_size: int = 500, max_trades: int = 20000) -> Iterable[Dict[str, Any]]:
    """Yield normalized trades for one market, paginating by offset."""
    offset, fetched, seq = 0, 0, 0
    while fetched < max_trades:
        params = {"market": condition_id, "limit": min(page_size, max_trades - fetched),
                  "offset": offset}
        batch = client.get_json(f"{base.rstrip('/')}/trades", params=params)
        if isinstance(batch, dict):
            batch = batch.get("data", batch.get("trades", []))
        if not batch:
            break
        for raw in batch:
            norm = normalize_trade(raw, fallback_market_id=condition_id, seq=seq)
            seq += 1
            if norm:
                yield norm
                fetched += 1
                if fetched >= max_trades:
                    break
        offset += len(batch)
        if len(batch) < params["limit"]:
            break


def get_user_positions(client: HttpClient, base: str, user: str) -> List[Dict[str, Any]]:
    """Current (open) positions for a wallet — used by the live dashboard.
    Returns raw normalized dicts (not persisted to the positions table, which is
    derived from trades)."""
    data = client.get_json(f"{base.rstrip('/')}/positions", params={"user": user.lower()})
    if isinstance(data, dict):
        data = data.get("data", [])
    out = []
    for p in data or []:
        out.append({
            "wallet_address": user.lower(),
            "market_id": p.get("conditionId"),
            "outcome_index": p.get("outcomeIndex"),
            "size": p.get("size"),
            "avg_price": p.get("avgPrice"),
            "current_value": p.get("currentValue"),
            "cash_pnl": p.get("cashPnl"),
            "title": p.get("title"),
        })
    return out
