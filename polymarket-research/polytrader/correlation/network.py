"""Correlation & network analysis over the top-ranked wallets.

For every pair of wallets in the universe (default: top-100 by composite score)
that co-traded at least ``min_shared_markets`` markets we compute four
relationship measures the brief asks for:

* **market_overlap**  – Jaccard over the set of markets each traded.
* **timing_jaccard**  – fraction of their combined markets entered on the *same
  side within* ``cofollow_window_hours`` (the "move together" signal; seeds graph edges).
* **direction_corr**  – net same-side agreement over shared markets, in [-1, 1].
* **profit_corr**     – Pearson correlation of monthly P&L vectors.
* **lead_lag_hours**  – mean (entry_b − entry_a) on same-side shared markets;
  positive ⇒ *a* tends to enter first ⇒ *a* leads *b*.

We then build a NetworkX graph (edges = timing_jaccard ≥ threshold), run
**Louvain** community detection, and derive a leader→follower ranking from the
lead-lag signs.  Communities are written back to ``wallet_clusters.community``.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from itertools import combinations
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from sqlalchemy import delete, select, update

from ..config import get_config
from ..db.models import (AnalysisArtifact, Position, WalletCluster,
                         WalletMetric, WalletScore)
from ..db.session import session_scope

try:
    from networkx.algorithms.community import louvain_communities, modularity
    import networkx as nx
    _HAVE_NX = True
except Exception:  # pragma: no cover
    _HAVE_NX = False


def _monthly_corr(a: Dict[str, float], b: Dict[str, float]) -> float:
    common = sorted(set(a) & set(b))
    if len(common) < 3:
        return 0.0
    va = np.array([a[m] for m in common], float)
    vb = np.array([b[m] for m in common], float)
    if np.std(va) < 1e-9 or np.std(vb) < 1e-9:
        return 0.0
    return float(np.corrcoef(va, vb)[0, 1])


def analyze_network(as_of: Optional[datetime] = None) -> Dict[str, object]:
    cfg = get_config()
    cor = cfg.correlation
    top_n = int(cor["top_n"])
    win_h = float(cor["cofollow_window_hours"])
    min_shared = int(cor["min_shared_markets"])
    thr = float(cor["edge_threshold"])

    with session_scope() as session:
        scores = pd.read_sql(
            select(WalletScore.__table__.c.wallet_address, WalletScore.__table__.c.rank,
                   WalletScore.__table__.c.composite_score)
            .order_by(WalletScore.rank).limit(top_n), session.bind)
        if scores.empty:
            return {"pairs": 0, "note": "no ranked wallets"}
        if as_of is None:
            as_of = pd.to_datetime(
                session.scalar(select(WalletScore.as_of).limit(1))).to_pydatetime()
        universe = scores["wallet_address"].tolist()
        rank_of = dict(zip(scores["wallet_address"], scores["rank"]))

        pos = pd.read_sql(
            select(Position.__table__.c.wallet_address, Position.__table__.c.market_id,
                   Position.__table__.c.outcome_index, Position.__table__.c.opened_at)
            .where(Position.wallet_address.in_(universe)), session.bind)
        pos["opened_at"] = pd.to_datetime(pos["opened_at"])
        # per (wallet, market): earliest entry + its outcome side
        entry = (pos.sort_values("opened_at")
                 .groupby(["wallet_address", "market_id"])
                 .agg(t=("opened_at", "first"), side=("outcome_index", "first")).reset_index())
        wmarkets: Dict[str, Dict[str, tuple]] = defaultdict(dict)
        for r in entry.itertuples(index=False):
            wmarkets[r.wallet_address][r.market_id] = (r.t.value / 3.6e12, int(r.side))  # hours

        # monthly pnl from metrics.extra
        met = pd.read_sql(select(WalletMetric.__table__.c.wallet_address,
                                 WalletMetric.__table__.c.extra)
                          .where(WalletMetric.wallet_address.in_(universe)), session.bind)
        monthly = {r.wallet_address: (r.extra or {}).get("monthly_pnl", {})
                   for r in met.itertuples(index=False)}

        pairs: List[dict] = []
        lead_score: Dict[str, float] = defaultdict(float)
        followers_of: Dict[str, list] = defaultdict(list)

        for a, b in combinations(universe, 2):
            ma, mb = wmarkets.get(a, {}), wmarkets.get(b, {})
            if not ma or not mb:
                continue
            sa, sb = set(ma), set(mb)
            shared = sa & sb
            if len(shared) < min_shared:
                continue
            union = sa | sb
            same = opp = co_follow = 0
            leadlag = []
            for m in shared:
                ta, oa = ma[m]; tb, ob = mb[m]
                if oa == ob:
                    same += 1
                    if abs(ta - tb) <= win_h:
                        co_follow += 1
                    leadlag.append(tb - ta)  # +ve => a earlier => a leads
                else:
                    opp += 1
            n_shared = len(shared)
            timing_jac = co_follow / len(union)
            direction = (same - opp) / n_shared
            pcorr = _monthly_corr(monthly.get(a, {}), monthly.get(b, {}))
            ll = float(np.mean(leadlag)) if leadlag else 0.0
            pairs.append(dict(
                as_of=as_of, wallet_a=a, wallet_b=b, n_shared_markets=n_shared,
                market_overlap=len(shared) / len(union), timing_jaccard=timing_jac,
                direction_corr=direction, profit_corr=pcorr, lead_lag_hours=ll))
            # leadership signal: strong co-follow + consistent lead sign
            if timing_jac >= thr and co_follow >= min_shared:
                if ll > 6:      # a clearly leads b
                    lead_score[a] += co_follow; followers_of[a].append(b)
                elif ll < -6:   # b leads a
                    lead_score[b] += co_follow; followers_of[b].append(a)

        # ---- network graph + Louvain ----
        community_of: Dict[str, int] = {}
        graph_stats = {"nodes": len(universe), "edges": 0, "communities": 0, "modularity": None}
        if _HAVE_NX:
            G = nx.Graph()
            G.add_nodes_from(universe)
            for p in pairs:
                if p["timing_jaccard"] >= thr:
                    G.add_edge(p["wallet_a"], p["wallet_b"], weight=p["timing_jaccard"])
            graph_stats["edges"] = G.number_of_edges()
            if G.number_of_edges() > 0:
                comms = louvain_communities(G, weight="weight",
                                            resolution=float(cor.get("louvain_resolution", 1.0)),
                                            seed=cfg.seed)
                comms = sorted(comms, key=len, reverse=True)
                for cid, nodes in enumerate(comms):
                    for node in nodes:
                        community_of[node] = cid
                graph_stats["communities"] = sum(1 for c in comms if len(c) > 1)
                try:
                    graph_stats["modularity"] = round(float(modularity(G, comms, weight="weight")), 4)
                except Exception:
                    pass

        # ---- leader/follower table ----
        leaders = sorted(lead_score.items(), key=lambda kv: kv[1], reverse=True)
        leader_rows = [{
            "wallet": w, "rank": int(rank_of.get(w, -1)),
            "lead_score": round(s, 1),
            "n_followers": len(set(followers_of[w])),
            "followers": list({f: rank_of.get(f, -1) for f in followers_of[w]}.items())[:15],
        } for w, s in leaders[:15] if s > 0]

        # ---- aggregate answers to the brief's questions ----
        pdf = pd.DataFrame(pairs)
        top10 = set(scores.nsmallest(10, "rank")["wallet_address"])
        def _avg(mask_df, col):
            return round(float(mask_df[col].mean()), 4) if len(mask_df) else None
        avg_overlap = _avg(pdf, "market_overlap") if len(pdf) else None
        avg_timing = _avg(pdf, "timing_jaccard") if len(pdf) else None
        summary = {
            "universe_size": len(universe), "n_pairs_evaluated": len(pairs),
            "avg_market_overlap": avg_overlap,
            "avg_timing_jaccard": avg_timing,
            "avg_direction_corr": _avg(pdf, "direction_corr") if len(pdf) else None,
            "avg_profit_corr": _avg(pdf, "profit_corr") if len(pdf) else None,
            "top10_avg_market_overlap": (
                _avg(pdf[pdf.wallet_a.isin(top10) & pdf.wallet_b.isin(top10)], "market_overlap")
                if len(pdf) else None),
            "graph": graph_stats,
            "n_leaders_detected": len(leader_rows),
            "interpretation": {
                "top_traders_enter_same_markets": bool((avg_overlap or 0) > 0.10),
                "top_traders_enter_similar_times": bool((avg_timing or 0) > 0.05),
                "leaders_present": len(leader_rows) > 0,
                "coordinated_communities": graph_stats["communities"] > 0,
            },
        }

        # ---- persist ----
        session.execute(delete(AnalysisArtifact).where(
            AnalysisArtifact.kind.in_(["network_summary", "leader_follower"])))
        session.add(AnalysisArtifact(as_of=as_of, kind="network_summary",
                                     name="top_wallets", payload=summary))
        session.add(AnalysisArtifact(as_of=as_of, kind="leader_follower",
                                     name="leaders", payload={"leaders": leader_rows}))
        # store top pairs (cap to keep table lean)
        pairs_sorted = sorted(pairs, key=lambda p: p["timing_jaccard"], reverse=True)
        from ..db.models import WalletPair
        session.execute(delete(WalletPair))
        session.bulk_insert_mappings(WalletPair, pairs_sorted[:5000])
        # write communities back onto cluster rows
        for w, c in community_of.items():
            session.execute(update(WalletCluster)
                            .where(WalletCluster.wallet_address == w).values(community=c))

        return {"pairs": len(pairs), "edges": graph_stats["edges"],
                "communities": graph_stats["communities"],
                "modularity": graph_stats["modularity"],
                "leaders": len(leader_rows), "summary": summary}
