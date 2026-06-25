"""Behavioral clustering of eligible wallets.

Answers "what do similar traders look like?" by segmenting wallets on *behavior*
(position sizing, holding period, entry timing, breadth, contrarianism, win/ROI)
rather than on identity.  We run three complementary views:

* **K-means** with k chosen by silhouette over the configured range — hard,
  centroid-based segments for labeling archetypes.
* **Agglomerative (Ward)** hierarchical clustering — a second opinion robust to
  non-spherical groups; agreement with k-means is a stability check.
* **PCA(2)** — a 2-D embedding for the dashboard scatter.

Each k-means cluster gets a profile (size, mean metrics) and an auto-generated
human label so the final report can describe the archetypes in plain English.
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from sklearn.cluster import AgglomerativeClustering, KMeans
from sklearn.decomposition import PCA
from sklearn.metrics import silhouette_score
from sklearn.preprocessing import StandardScaler
from sqlalchemy import delete, select

from ..config import get_config
from ..db.models import AnalysisArtifact, WalletMetric, WalletScore, WalletCluster
from ..db.session import session_scope


def _label_cluster(prof: Dict[str, float]) -> str:
    """Heuristic human label from a cluster's mean behavioral profile."""
    bits: List[str] = []
    bits.append("Sharp" if prof["avg_roi"] > 0.10 else
                ("Break-even" if prof["avg_roi"] > -0.05 else "Losing"))
    bits.append("disciplined" if prof["position_size_cv"] < 0.5 else "erratic")
    if prof["median_hold_hours"] > 24 * 14:
        bits.append("long-hold")
    elif prof["median_hold_hours"] < 24 * 2:
        bits.append("fast-flip")
    if prof["entry_lead_days"] > 21:
        bits.append("early-entrant")
    if prof["category_concentration"] > 0.5:
        bits.append("specialist")
    elif prof["category_concentration"] < 0.3:
        bits.append("diversified")
    if prof["avg_entry_price"] < 0.35:
        bits.append("longshot-buyer")
    elif prof["avg_entry_price"] > 0.65:
        bits.append("favorite-buyer")
    return " ".join(bits)


def cluster_wallets(as_of: Optional[datetime] = None) -> Dict[str, object]:
    cfg = get_config()
    cc = cfg.clustering
    features = list(cc["features"])
    krange = range(int(cc["kmeans_k_range"][0]), int(cc["kmeans_k_range"][1]) + 1)

    with session_scope() as session:
        met = pd.read_sql(
            select(WalletMetric.__table__).where(WalletMetric.eligible == True),  # noqa: E712
            session.bind)
        if len(met) < max(krange) + 1:
            return {"clustered": 0, "note": "too few eligible wallets"}
        if as_of is None:
            as_of = pd.to_datetime(met["as_of"]).max().to_pydatetime()

        X = met[features].to_numpy(dtype=float)
        X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
        Xs = StandardScaler().fit_transform(X)

        # --- choose k by silhouette ---
        best_k, best_sil, best_labels = None, -1.0, None
        sil_by_k = {}
        for k in krange:
            km = KMeans(n_clusters=k, n_init=10, random_state=cfg.seed).fit(Xs)
            sil = float(silhouette_score(Xs, km.labels_)) if k > 1 else -1.0
            sil_by_k[k] = sil
            if sil > best_sil:
                best_k, best_sil, best_labels = k, sil, km.labels_
        kmeans_labels = best_labels

        # --- hierarchical (second opinion at the same k) ---
        hier = AgglomerativeClustering(n_clusters=best_k, linkage=cc.get("hierarchical_linkage", "ward"))
        hier_labels = hier.fit_predict(Xs)

        # --- PCA(2) embedding ---
        pca = PCA(n_components=int(cc.get("pca_components", 2)), random_state=cfg.seed)
        coords = pca.fit_transform(Xs)

        met = met.reset_index(drop=True)
        met["kmeans_cluster"] = kmeans_labels
        met["hierarchical_cluster"] = hier_labels
        met["pca_x"] = coords[:, 0]
        met["pca_y"] = coords[:, 1]

        # --- cluster profiles + labels ---
        scores = pd.read_sql(select(WalletScore.__table__.c.wallet_address,
                                    WalletScore.__table__.c.composite_score), session.bind)
        merged = met.merge(scores, on="wallet_address", how="left")
        profile_cols = features + ["win_rate", "avg_roi", "total_profit_usd",
                                   "n_positions", "composite_score"]
        profiles = []
        for cid, g in merged.groupby("kmeans_cluster"):
            prof = {c: float(g[c].mean()) for c in profile_cols if c in g}
            profiles.append({
                "cluster_id": int(cid), "size": int(len(g)),
                "label": _label_cluster(prof),
                "mean_composite_score": float(g["composite_score"].mean()),
                "share_profitable": float((g["total_profit_usd"] > 0).mean()),
                "profile": {k: round(v, 4) for k, v in prof.items()},
            })
        profiles.sort(key=lambda p: p["mean_composite_score"], reverse=True)

        # --- persist (clustering owns these rows; correlation later sets community) ---
        session.execute(delete(WalletCluster))
        session.bulk_insert_mappings(WalletCluster, [
            dict(wallet_address=r["wallet_address"], as_of=as_of,
                 kmeans_cluster=int(r["kmeans_cluster"]),
                 hierarchical_cluster=int(r["hierarchical_cluster"]),
                 pca_x=float(r["pca_x"]), pca_y=float(r["pca_y"]), community=-1)
            for _, r in met.iterrows()])
        session.execute(delete(AnalysisArtifact).where(AnalysisArtifact.kind == "cluster_profile"))
        session.add(AnalysisArtifact(
            as_of=as_of, kind="cluster_profile", name="kmeans",
            payload={"k": int(best_k), "silhouette": round(best_sil, 4),
                     "silhouette_by_k": {str(k): round(v, 4) for k, v in sil_by_k.items()},
                     "pca_explained_variance": [round(float(x), 4) for x in pca.explained_variance_ratio_],
                     "features": features, "clusters": profiles}))

        return {"clustered": len(met), "k": int(best_k), "silhouette": round(best_sil, 3),
                "labels": [p["label"] for p in profiles]}
