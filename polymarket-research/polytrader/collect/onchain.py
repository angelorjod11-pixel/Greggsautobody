"""On-chain enrichment via the Polymarket order-book subgraph (Goldsky / GraphQL).

This is an *alternative / cross-check* source to the Data API: the subgraph
indexes ``OrderFilled`` events from the CTF Exchange contract on Polygon, giving
fully on-chain, tamper-evident trade history.

⚠️ Entity and field names differ between subgraph deployments/versions.  The
query below targets the common ``enrichedOrderFilled`` entity; if your endpoint
exposes a different schema, run an introspection query first and adjust
``FILLED_ORDERS_QUERY`` / :func:`normalize_filled_order`.  The Data-API path
(gamma + data_api) is the primary, best-supported collector.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from .rate_limit import HttpClient

# Time-keyset pagination (more reliable than skip for large ranges).
FILLED_ORDERS_QUERY = """
query Filled($lastTs: BigInt!, $first: Int!) {
  enrichedOrderFilleds(
    first: $first,
    orderBy: timestamp,
    orderDirection: asc,
    where: { timestamp_gt: $lastTs }
  ) {
    id
    timestamp
    maker { id }
    taker { id }
    market { id }
    side
    size
    price
    transactionHash
  }
}
"""


def normalize_filled_order(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Map a subgraph OrderFilled entity to the ``trades`` table schema."""
    try:
        ts = datetime.fromtimestamp(int(raw["timestamp"]), tz=timezone.utc).replace(tzinfo=None)
        price = float(raw["price"])
        shares = float(raw["size"])
    except (KeyError, TypeError, ValueError, OSError):
        return None
    maker = (raw.get("maker") or {}).get("id") if isinstance(raw.get("maker"), dict) else raw.get("maker")
    wallet = (maker or "").lower()
    market = (raw.get("market") or {}).get("id") if isinstance(raw.get("market"), dict) else raw.get("market")
    if not wallet or not market:
        return None
    side = str(raw.get("side", "BUY")).upper()
    side = "BUY" if side not in ("BUY", "SELL") else side
    return {
        "id": str(raw.get("id")),
        "wallet_address": wallet,
        "market_id": str(market),
        "outcome_index": int(raw.get("outcomeIndex", 0) or 0),
        "side": side,
        "timestamp": ts,
        "price": price,
        "shares": shares,
        "usd_size": price * shares,
        "tx_hash": raw.get("transactionHash"),
    }


class SubgraphClient:
    def __init__(self, client: HttpClient, url: str):
        self.client = client
        self.url = url

    def query(self, gql: str, variables: Dict[str, Any]) -> Dict[str, Any]:
        resp = self.client.post_json(self.url, {"query": gql, "variables": variables})
        if "errors" in resp:
            raise RuntimeError(f"subgraph error: {resp['errors']}")
        return resp.get("data", {})

    def iter_filled_orders(self, *, start_ts: int = 0, page_size: int = 1000,
                           max_orders: int = 100000) -> Iterable[Dict[str, Any]]:
        """Stream normalized on-chain fills using timestamp keyset pagination."""
        last_ts, fetched = start_ts, 0
        while fetched < max_orders:
            data = self.query(FILLED_ORDERS_QUERY, {"lastTs": str(last_ts),
                                                    "first": min(page_size, max_orders - fetched)})
            rows = data.get("enrichedOrderFilleds", [])
            if not rows:
                break
            for raw in rows:
                norm = normalize_filled_order(raw)
                if norm:
                    yield norm
                    fetched += 1
            last_ts = int(rows[-1]["timestamp"])
            if len(rows) < page_size:
                break
