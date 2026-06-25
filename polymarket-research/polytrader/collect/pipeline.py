"""Live collection orchestrator.

``collect_all()`` builds the trader dataset from the public Polymarket APIs:

    Gamma /markets  ──►  for each market:  Data-API /trades  ──►  wallets ∪ trades

and writes ``markets``, ``wallets`` and ``trades`` rows — exactly the same schema
the synthetic generator targets, so the downstream ETL + analytics are identical
for live and synthetic data.

Run with ``POLYTRADER_DATA_SOURCE=live python -m polytrader.cli all`` in an
environment whose egress policy permits ``*.polymarket.com``.  (In sandboxes that
block those hosts this step raises a network error by design — use the default
synthetic source there.)
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List

from ..config import get_config
from ..db.models import Market, Trade, Wallet
from ..db.session import init_db, session_scope
from .data_api import iter_market_trades
from .gamma import iter_markets
from .rate_limit import HttpClient


def collect_all(drop: bool = True, verbose: bool = True) -> Dict[str, int]:
    cfg = get_config()
    c = cfg.collect
    init_db(drop=drop)

    client = HttpClient(rate_per_sec=float(c["requests_per_second"]),
                        max_retries=int(c["max_retries"]),
                        backoff_base=float(c["backoff_base_seconds"]))
    markets: List[dict] = []
    wallets: Dict[str, dict] = {}
    seen_trade_ids: set = set()
    n_trades = 0
    failed_markets = 0

    try:
        # 1) discover resolved, liquid markets
        if verbose:
            print(f"[collect] discovering up to {c['max_markets']} markets via Gamma…")
        for m in iter_markets(client, c["gamma_base"], page_size=int(c["page_size"]),
                              max_markets=int(c["max_markets"]), closed=True,
                              min_volume=float(c["min_market_volume_usd"]),
                              category_map=cfg.categories):
            markets.append(m)
        if verbose:
            print(f"[collect] {len(markets)} markets; pulling trades…")

        # 2) per-market trades -> wallets + trades (flush in batches)
        with session_scope() as session:
            session.bulk_insert_mappings(
                Market, [{k: v for k, v in m.items() if not k.startswith("_")} for m in markets])

        batch: List[dict] = []
        for i, m in enumerate(markets):
            try:
                for tr in iter_market_trades(client, c["data_base"], m["id"],
                                             page_size=int(c["page_size"]),
                                             max_trades=int(c["max_trades_per_market"])):
                    if tr["id"] in seen_trade_ids:
                        continue
                    seen_trade_ids.add(tr["id"])
                    a = tr["wallet_address"]
                    w = wallets.get(a)
                    if w is None:
                        wallets[a] = {"address": a, "label": None,
                                      "first_seen": tr["timestamp"], "last_seen": tr["timestamp"]}
                    else:
                        w["first_seen"] = min(w["first_seen"], tr["timestamp"])
                        w["last_seen"] = max(w["last_seen"], tr["timestamp"])
                    batch.append(tr)
                    n_trades += 1
                    if len(batch) >= 5000:
                        with session_scope() as session:
                            session.bulk_insert_mappings(Trade, batch)
                        batch = []
            except Exception as exc:  # noqa: BLE001 — resilience: skip a bad market
                failed_markets += 1
                if verbose:
                    print(f"[collect]   market {m['id']} failed: {exc}")
            if verbose and (i + 1) % 100 == 0:
                print(f"[collect]   {i+1}/{len(markets)} markets, {n_trades} trades…")

        if batch:
            with session_scope() as session:
                session.bulk_insert_mappings(Trade, batch)
        # 3) wallets (only those that traded)
        with session_scope() as session:
            session.bulk_insert_mappings(Wallet, list(wallets.values()))
    finally:
        client.close()

    return {"markets": len(markets), "wallets": len(wallets), "trades": n_trades,
            "failed_markets": failed_markets}
