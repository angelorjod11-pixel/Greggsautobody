"""CLOB API client — order-book price history (optional signal enrichment).

Base: https://clob.polymarket.com  ·  Endpoint: ``GET /prices-history``

Used to reconstruct the implied-probability path of an outcome token, which the
signals module can use to measure post-entry odds movement.  Not required for the
core ranking pipeline.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from .rate_limit import HttpClient


def price_history(client: HttpClient, base: str, token_id: str, *,
                  interval: str = "max", fidelity: int = 60) -> List[Tuple[datetime, float]]:
    """Return [(timestamp, price)] for a CLOB outcome token.

    ``interval`` ∈ {1m,1h,6h,1d,1w,max}; ``fidelity`` is the bucket size in minutes.
    """
    data = client.get_json(f"{base.rstrip('/')}/prices-history",
                           params={"market": token_id, "interval": interval, "fidelity": fidelity})
    hist = data.get("history", []) if isinstance(data, dict) else []
    out: List[Tuple[datetime, float]] = []
    for pt in hist:
        try:
            t = datetime.fromtimestamp(int(pt["t"]), tz=timezone.utc).replace(tzinfo=None)
            out.append((t, float(pt["p"])))
        except (KeyError, TypeError, ValueError, OSError):
            continue
    return out
