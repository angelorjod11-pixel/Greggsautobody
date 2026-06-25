"""Gamma API client — market & event metadata.

Base: https://gamma-api.polymarket.com  ·  Endpoint used: ``GET /markets``

Gamma returns rich market metadata.  We normalize the fields we need into the
``markets`` table shape.  The normalizers are pure functions (no network) so the
field-mapping logic is unit-tested offline.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from dateutil import parser as dateparser

from .rate_limit import HttpClient


def _parse_json_list(val: Any) -> List[Any]:
    """Gamma encodes arrays as JSON strings, e.g. outcomePrices='["1","0"]'."""
    if val is None:
        return []
    if isinstance(val, list):
        return val
    try:
        out = json.loads(val)
        return out if isinstance(out, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _parse_dt(val: Any) -> Optional[datetime]:
    if not val:
        return None
    try:
        dt = dateparser.parse(val) if isinstance(val, str) else None
        if dt and dt.tzinfo:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except (ValueError, OverflowError):
        return None


def categorize(question: str, tags: Iterable[str], explicit: Optional[str],
               category_map: Dict[str, List[str]]) -> Optional[str]:
    """Route a market to one of the configured categories by keyword, preferring
    an explicit category/tag match, then keywords in the question."""
    hay = " ".join([question or ""] + [str(t) for t in (tags or [])]).lower()
    if explicit:
        for cat in category_map:
            if cat.lower() == str(explicit).lower():
                return cat
    best, best_hits = None, 0
    for cat, kws in category_map.items():
        hits = sum(1 for kw in kws if kw.lower() in hay)
        if hits > best_hits:
            best, best_hits = cat, hits
    return best


def winning_outcome_from_prices(outcome_prices: List[Any], resolved: bool) -> Optional[int]:
    """After resolution Gamma sets outcomePrices to ~['1','0']; the '1' index won."""
    if not resolved or not outcome_prices:
        return None
    try:
        vals = [float(x) for x in outcome_prices]
    except (TypeError, ValueError):
        return None
    if not vals:
        return None
    idx = int(max(range(len(vals)), key=lambda i: vals[i]))
    return idx if vals[idx] >= 0.9 else None  # require a decisive settlement


def normalize_market(raw: Dict[str, Any], category_map: Dict[str, List[str]]) -> Optional[Dict[str, Any]]:
    """Map a Gamma market object to the ``markets`` table schema."""
    condition_id = raw.get("conditionId") or raw.get("condition_id")
    market_id = condition_id or (str(raw["id"]) if raw.get("id") is not None else None)
    if not market_id:
        return None
    closed = bool(raw.get("closed", False))
    prices = _parse_json_list(raw.get("outcomePrices"))
    win = winning_outcome_from_prices(prices, closed)
    resolved = closed and win is not None

    tags = []
    for ev in (raw.get("events") or []):
        for t in (ev.get("tags") or []):
            tags.append(t.get("label") if isinstance(t, dict) else t)
    if raw.get("category"):
        tags.append(raw["category"])

    volume = float(raw.get("volumeNum") or raw.get("volume") or 0.0)
    liquidity = float(raw.get("liquidityNum") or raw.get("liquidity") or 0.0)
    end = _parse_dt(raw.get("endDate"))
    return {
        "id": str(market_id),
        "slug": raw.get("slug"),
        "question": raw.get("question"),
        "category": categorize(raw.get("question", ""), tags, raw.get("category"), category_map),
        "created_at": _parse_dt(raw.get("startDate") or raw.get("createdAt")),
        "end_date": end,
        "resolved": resolved,
        "resolved_at": end if resolved else None,
        "winning_outcome": win,
        "volume_usd": volume,
        "liquidity_usd": liquidity,
        # carried for the trade collector (not stored): the CLOB token ids
        "_clob_token_ids": _parse_json_list(raw.get("clobTokenIds")),
    }


def iter_markets(client: HttpClient, base: str, *, page_size: int = 500,
                 max_markets: int = 5000, closed: Optional[bool] = True,
                 min_volume: float = 0.0, category_map: Optional[Dict[str, List[str]]] = None
                 ) -> Iterable[Dict[str, Any]]:
    """Yield normalized markets, paginating ``/markets`` by offset.

    ``closed=True`` restricts to resolved markets (needed for realized P&L).
    """
    category_map = category_map or {}
    offset, fetched = 0, 0
    while fetched < max_markets:
        params = {"limit": min(page_size, max_markets - fetched), "offset": offset,
                  "order": "volumeNum", "ascending": "false"}
        if closed is not None:
            params["closed"] = str(closed).lower()
        batch = client.get_json(f"{base.rstrip('/')}/markets", params=params)
        if isinstance(batch, dict):  # some deployments wrap in {data: [...]}
            batch = batch.get("data", [])
        if not batch:
            break
        for raw in batch:
            norm = normalize_market(raw, category_map)
            if norm and norm["volume_usd"] >= min_volume:
                yield norm
                fetched += 1
                if fetched >= max_markets:
                    break
        offset += len(batch)
        if len(batch) < params["limit"]:
            break
