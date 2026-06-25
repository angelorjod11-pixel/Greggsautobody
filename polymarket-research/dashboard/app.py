"""Streamlit dashboard for the Polymarket trader research system.

Reads **directly** from the populated research database (it imports the
``polytrader`` package and the local :mod:`dashboard.data` helpers) — the
FastAPI service does *not* need to be running. Plotly powers the charts.

Seven views (sidebar radio): Overview, Wallet detail, Network, Clusters,
Backtests, Small account ($20), and Signals & Alpha. Each guards gracefully if
its underlying artifact is missing.

Run it with::

    export POLYTRADER_DATABASE_URL="sqlite:////abs/path/to/data/polytrader.db"
    export PYTHONPATH=/abs/path/to/polymarket-research
    streamlit run dashboard/app.py
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional

# Make `import dashboard.data` / `import polytrader` work whether Streamlit is
# launched from the repo root or elsewhere.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

try:  # spring_layout drives the network graph; degrade gracefully if absent
    import networkx as nx

    _HAVE_NX = True
except Exception:  # pragma: no cover
    _HAVE_NX = False

from dashboard import data as D

st.set_page_config(
    page_title="Polymarket Trader Research",
    page_icon=None,
    layout="wide",
)


# --------------------------------------------------------------------------- #
# Cached data loaders (thin wrappers around dashboard.data so the heavy SQL /
# JSON reads happen once per input across reruns)
# --------------------------------------------------------------------------- #
@st.cache_data(show_spinner=False)
def load_overview() -> Dict[str, Any]:
    return D.get_overview_stats()


@st.cache_data(show_spinner=False)
def load_top(n: int) -> pd.DataFrame:
    return D.get_top_wallets(n)


@st.cache_data(show_spinner=False)
def load_clusters_df() -> pd.DataFrame:
    return D.get_wallet_clusters()


@st.cache_data(show_spinner=False)
def load_pairs(limit: int) -> pd.DataFrame:
    return D.get_wallet_pairs(limit)


@st.cache_data(show_spinner=False)
def load_artifact(kind: str, name: Optional[str] = None) -> Optional[Dict[str, Any]]:
    return D.get_artifact(kind, name)


@st.cache_data(show_spinner=False)
def load_artifacts(kind: str) -> Dict[str, Dict[str, Any]]:
    return D.get_artifacts(kind)


def _missing(label: str) -> None:
    """Standardised 'artifact not available' notice."""
    st.info(
        f"**{label}** is not available in the database yet. "
        "Run `python -m polytrader.cli all` to populate the analysis artifacts."
    )


# --------------------------------------------------------------------------- #
# Page: Overview
# --------------------------------------------------------------------------- #
def page_overview() -> None:
    st.header("Overview")
    stats = load_overview()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Wallets analysed", f"{stats['n_wallets']:,}")
    c2.metric("Eligible wallets", f"{stats['n_eligible']:,}")
    c3.metric("Ranked wallets", f"{stats['n_ranked']:,}")
    c4.metric("Data source", str(stats["data_source"]))
    st.caption(f"As of: {stats['as_of'] or 'n/a'}")

    top = load_top(100)
    if top.empty:
        _missing("Wallet rankings")
        return

    st.subheader("Top 25 wallets")
    cols = [
        "rank", "wallet_address", "composite_score", "score_ci_low", "score_ci_high",
        "total_profit_usd", "avg_roi", "win_rate", "sharpe", "max_drawdown",
        "n_positions", "top_category",
    ]
    cols = [c for c in cols if c in top.columns]
    st.dataframe(
        top.head(25)[cols],
        use_container_width=True,
        hide_index=True,
    )

    st.subheader("Top 15 composite scores (with bootstrap 90% CI)")
    top15 = top.head(15).copy()
    top15["label"] = top15["wallet_address"].map(D.short_addr)
    # Asymmetric error bars from the stored CI bounds.
    err_plus = (top15["score_ci_high"] - top15["composite_score"]).clip(lower=0)
    err_minus = (top15["composite_score"] - top15["score_ci_low"]).clip(lower=0)
    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            x=top15["label"],
            y=top15["composite_score"],
            error_y=dict(
                type="data",
                symmetric=False,
                array=err_plus,
                arrayminus=err_minus,
                color="rgba(0,0,0,0.45)",
            ),
            marker_color="#2c7fb8",
            hovertext=top15["wallet_address"],
            name="composite",
        )
    )
    fig.update_layout(
        xaxis_title="wallet",
        yaxis_title="composite score",
        height=420,
        margin=dict(t=10, b=10),
    )
    st.plotly_chart(fig, use_container_width=True)


# --------------------------------------------------------------------------- #
# Page: Wallet detail
# --------------------------------------------------------------------------- #
def page_wallet_detail() -> None:
    st.header("Wallet detail")
    top = load_top(200)
    if top.empty:
        _missing("Wallet rankings")
        return

    clusters = load_clusters_df()
    cluster_by_addr = (
        clusters.set_index("wallet_address") if not clusters.empty else pd.DataFrame()
    )

    # Selectbox of top wallets labelled "rank — 0xabcd…1234".
    options = top["wallet_address"].tolist()
    labels = {
        a: f"#{int(r)} — {D.short_addr(a)}"
        for a, r in zip(top["wallet_address"], top["rank"])
    }
    address = st.selectbox(
        "Wallet", options, format_func=lambda a: labels.get(a, a)
    )
    row = top.loc[top["wallet_address"] == address].iloc[0]

    left, right = st.columns([1.1, 1])

    with left:
        st.subheader("Score components")
        comps = D.get_score_components(top, address)
        comp_df = pd.DataFrame(
            {"component": list(comps.keys()), "score": list(comps.values())}
        )
        fig = px.bar(
            comp_df, x="component", y="score", range_y=[0, 100],
            color="component", text="score",
        )
        fig.update_traces(texttemplate="%{text:.1f}", textposition="outside")
        fig.update_layout(showlegend=False, height=360, yaxis_title="0–100", margin=dict(t=10))
        st.plotly_chart(fig, use_container_width=True)
        st.metric("Composite score", f"{row['composite_score']:.2f}",
                  help=f"90% CI: {row['score_ci_low']:.2f} – {row['score_ci_high']:.2f}")

    with right:
        st.subheader("Key metrics")
        metric_fields = [
            ("Rank", "rank", "{:.0f}"),
            ("Total profit (USD)", "total_profit_usd", "${:,.0f}"),
            ("Avg ROI", "avg_roi", "{:.1%}"),
            ("Median ROI", "median_roi", "{:.1%}"),
            ("Win rate", "win_rate", "{:.1%}"),
            ("Profit factor", "profit_factor", "{:.2f}"),
            ("Sharpe", "sharpe", "{:.3f}"),
            ("Max drawdown", "max_drawdown", "{:.1%}"),
            ("Positions", "n_positions", "{:.0f}"),
            ("Markets", "n_markets", "{:.0f}"),
            ("Active days", "active_days", "{:.0f}"),
            ("Median hold (h)", "median_hold_hours", "{:.0f}"),
            ("Top category", "top_category", "{}"),
        ]
        rows = []
        for label, col, fmt in metric_fields:
            if col in row and pd.notna(row[col]):
                try:
                    rows.append({"metric": label, "value": fmt.format(row[col])})
                except (ValueError, TypeError):
                    rows.append({"metric": label, "value": str(row[col])})
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        st.subheader("Cluster & community")
        if not cluster_by_addr.empty and address in cluster_by_addr.index:
            cr = cluster_by_addr.loc[address]
            comm = int(cr["community"])
            st.write(
                {
                    "kmeans_cluster": int(cr["kmeans_cluster"]),
                    "hierarchical_cluster": int(cr["hierarchical_cluster"]),
                    "community": comm if comm >= 0 else "—(not in a network community)",
                }
            )
        else:
            st.caption("No cluster row for this wallet.")


# --------------------------------------------------------------------------- #
# Page: Network
# --------------------------------------------------------------------------- #
def page_network() -> None:
    st.header("Network")
    summary = load_artifact("network_summary", "top_wallets")
    pairs = load_pairs(500)
    top = load_top(100)

    if summary is None and pairs.empty:
        _missing("Network analysis")
        return

    if summary:
        st.subheader("Network summary")
        g = summary.get("graph", {})
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Nodes", g.get("nodes", "—"))
        c2.metric("Edges", g.get("edges", "—"))
        c3.metric("Communities", g.get("communities", "—"))
        c4.metric("Modularity", g.get("modularity", "—"))
        d1, d2, d3 = st.columns(3)
        d1.metric("Avg market overlap", summary.get("avg_market_overlap", "—"))
        d2.metric("Avg timing jaccard", summary.get("avg_timing_jaccard", "—"))
        d3.metric("Leaders detected", summary.get("n_leaders_detected", "—"))

    # ---- spring-layout graph of the top wallets ----
    st.subheader("Co-trading graph (spring layout)")
    if pairs.empty:
        st.caption("No wallet pairs to draw.")
    elif not _HAVE_NX:
        st.warning("networkx is not available, cannot lay out the graph.")
    else:
        clusters = load_clusters_df()
        comm_by_addr = (
            dict(zip(clusters["wallet_address"], clusters["community"]))
            if not clusters.empty else {}
        )
        score_by_addr = dict(zip(top["wallet_address"], top["composite_score"]))
        rank_by_addr = dict(zip(top["wallet_address"], top["rank"]))

        G = nx.Graph()
        for r in pairs.itertuples(index=False):
            G.add_edge(r.wallet_a, r.wallet_b, weight=float(r.timing_jaccard))
        if G.number_of_nodes() == 0:
            st.caption("Graph has no nodes.")
        else:
            pos = nx.spring_layout(G, weight="weight", seed=42, k=None)
            fig = _build_network_figure(G, pos, comm_by_addr, score_by_addr, rank_by_addr)
            st.plotly_chart(fig, use_container_width=True)

    # ---- leaders table ----
    st.subheader("Leaders → followers")
    leaders = load_artifact("leader_follower", "leaders")
    if not leaders or not leaders.get("leaders"):
        st.caption("No leader/follower signal detected.")
    else:
        rows = [
            {
                "wallet": D.short_addr(l["wallet"]),
                "rank": l.get("rank"),
                "lead_score": l.get("lead_score"),
                "n_followers": l.get("n_followers"),
            }
            for l in leaders["leaders"]
        ]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def _build_network_figure(
    G: "nx.Graph",
    pos: Dict[str, Any],
    comm_by_addr: Dict[str, int],
    score_by_addr: Dict[str, float],
    rank_by_addr: Dict[str, int],
) -> go.Figure:
    """Assemble the plotly edge + node traces for the spring-layout graph."""
    # Edges as a single trace with None separators between segments.
    edge_x: List[Optional[float]] = []
    edge_y: List[Optional[float]] = []
    for a, b in G.edges():
        x0, y0 = pos[a]
        x1, y1 = pos[b]
        edge_x += [x0, x1, None]
        edge_y += [y0, y1, None]
    edge_trace = go.Scatter(
        x=edge_x, y=edge_y, mode="lines",
        line=dict(width=0.6, color="rgba(120,120,120,0.4)"),
        hoverinfo="none", showlegend=False,
    )

    node_x, node_y, colors, sizes, texts = [], [], [], [], []
    for node in G.nodes():
        x, y = pos[node]
        node_x.append(x)
        node_y.append(y)
        comm = comm_by_addr.get(node, -1)
        colors.append(comm)
        score = score_by_addr.get(node, 50.0)
        # scale composite score (~0–100) into a visible marker size band
        sizes.append(8 + 0.28 * float(score))
        texts.append(
            f"{D.short_addr(node)}<br>rank: {rank_by_addr.get(node, '—')}"
            f"<br>community: {comm if comm >= 0 else '—'}"
            f"<br>score: {score:.1f}"
        )
    node_trace = go.Scatter(
        x=node_x, y=node_y, mode="markers", hoverinfo="text", text=texts,
        marker=dict(
            color=colors, colorscale="Turbo", showscale=True,
            colorbar=dict(title="community"), size=sizes,
            line=dict(width=0.5, color="white"),
        ),
        showlegend=False,
    )
    fig = go.Figure(data=[edge_trace, node_trace])
    fig.update_layout(
        height=560, margin=dict(t=10, b=10, l=10, r=10),
        xaxis=dict(visible=False), yaxis=dict(visible=False),
        plot_bgcolor="white",
    )
    return fig


# --------------------------------------------------------------------------- #
# Page: Clusters
# --------------------------------------------------------------------------- #
def page_clusters() -> None:
    st.header("Clusters")
    profile = load_artifact("cluster_profile", "kmeans")
    clusters = load_clusters_df()
    top = load_top(1000)

    if clusters.empty and profile is None:
        _missing("Clustering")
        return

    if profile:
        c1, c2, c3 = st.columns(3)
        c1.metric("k", profile.get("k", "—"))
        c2.metric("Silhouette", profile.get("silhouette", "—"))
        ev = profile.get("pca_explained_variance") or []
        c3.metric("PCA explained var", f"{sum(ev):.1%}" if ev else "—")

    st.subheader("Behavioral map (PCA embedding)")
    if clusters.empty:
        st.caption("No per-wallet cluster coordinates available.")
    else:
        scatter = clusters.copy()
        score_by_addr = (
            dict(zip(top["wallet_address"], top["composite_score"]))
            if not top.empty else {}
        )
        scatter["composite_score"] = scatter["wallet_address"].map(score_by_addr).fillna(40.0)
        scatter["cluster"] = scatter["kmeans_cluster"].astype(str)
        scatter["addr"] = scatter["wallet_address"].map(D.short_addr)
        fig = px.scatter(
            scatter, x="pca_x", y="pca_y", color="cluster",
            size="composite_score", size_max=18, hover_name="addr",
            hover_data={"pca_x": ":.2f", "pca_y": ":.2f", "composite_score": ":.1f",
                        "cluster": True},
            labels={"pca_x": "PCA-1", "pca_y": "PCA-2"},
        )
        fig.update_layout(height=520, margin=dict(t=10))
        st.plotly_chart(fig, use_container_width=True)

    if profile and profile.get("clusters"):
        st.subheader("Cluster archetypes")
        rows = []
        for c in profile["clusters"]:
            prof = c.get("profile", {})
            rows.append(
                {
                    "cluster": c.get("cluster_id"),
                    "label": c.get("label"),
                    "size": c.get("size"),
                    "mean_composite": round(c.get("mean_composite_score", 0.0), 1),
                    "share_profitable": c.get("share_profitable"),
                    "avg_roi": prof.get("avg_roi"),
                    "win_rate": prof.get("win_rate"),
                    "median_hold_h": prof.get("median_hold_hours"),
                    "entry_lead_days": prof.get("entry_lead_days"),
                    "avg_entry_price": prof.get("avg_entry_price"),
                }
            )
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


# --------------------------------------------------------------------------- #
# Page: Backtests
# --------------------------------------------------------------------------- #
_STRATEGY_ORDER = [
    "A_copy_top10", "B_consensus_3plus", "C_filtered_elite", "D_weighted_consensus",
]


def page_backtests() -> None:
    st.header("Backtests")
    meta = load_artifact("backtest_meta", "meta")
    strategies = load_artifacts("backtest")

    if not strategies:
        _missing("Backtests")
        return

    if meta:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Train fraction", meta.get("train_fraction", "—"))
        c2.metric("Train wallets", meta.get("n_train_wallets", "—"))
        c3.metric("Test signals", f"{meta.get('n_test_signals', 0):,}")
        c4.metric("Split", str(meta.get("t_split", "—"))[:10])

    ordered = [s for s in _STRATEGY_ORDER if s in strategies] + [
        s for s in strategies if s not in _STRATEGY_ORDER
    ]

    st.subheader("Strategy comparison")
    rows = []
    for name in ordered:
        p = strategies[name]
        rows.append(
            {
                "strategy": name,
                "description": p.get("description", ""),
                "wallets": p.get("n_selected_wallets"),
                "trades": p.get("n_trades"),
                "ending_$": p.get("ending_balance"),
                "total_return": p.get("total_return"),
                "CAGR": p.get("cagr"),
                "win_rate": p.get("win_rate"),
                "max_DD": p.get("max_drawdown"),
                "sharpe/trade": p.get("sharpe_per_trade"),
            }
        )
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    st.subheader("Equity curves")
    fig = go.Figure()
    drew_any = False
    for name in ordered:
        curve = strategies[name].get("equity_curve") or []
        if not curve:
            continue
        drew_any = True
        ts = [pd.to_datetime(t) for t, _ in curve]
        vals = [v for _, v in curve]
        fig.add_trace(go.Scatter(x=ts, y=vals, mode="lines", name=name))
    if drew_any:
        fig.update_layout(
            height=460, xaxis_title="time", yaxis_title="equity (USD)",
            margin=dict(t=10), legend=dict(orientation="h", y=-0.2),
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.caption("No equity curves stored for these strategies.")


# --------------------------------------------------------------------------- #
# Page: Small account ($20)
# --------------------------------------------------------------------------- #
def page_small_account() -> None:
    st.header("Small account ($20 bankroll)")
    payload = load_artifact("small_account", "summary")
    if not payload or "scenarios" not in payload:
        _missing("Small-account study")
        if payload and payload.get("note"):
            st.caption(payload["note"])
        return

    # The deliverable focuses on the (robust) haircut scenario.
    scenarios = payload["scenarios"]
    scenario_key = "haircut_edge" if "haircut_edge" in scenarios else next(iter(scenarios))
    sc = scenarios[scenario_key]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Recommended rule", payload.get("recommended_rule", "—"))
    c2.metric("Full-Kelly fraction", sc.get("full_kelly_fraction", "—"))
    c3.metric("Fractional Kelly used", sc.get("fractional_kelly_used", "—"))
    c4.metric("Recommended bet @ $20", f"${sc.get('recommended_bet_at_20', 0):.2f}")
    st.caption(
        f"Basis: {payload.get('recommendation_basis', '')} · scenario shown: "
        f"`{scenario_key}` · constraints: {payload.get('constraints', {})}"
    )

    rules = sc.get("sizing_rules", [])
    if not rules:
        st.caption("No sizing-rule simulations available.")
        return

    rules_df = pd.DataFrame(rules)
    st.subheader(f"Sizing rules — {scenario_key}")
    show_cols = [
        "rule", "median_ending", "mean_ending", "p10_ending", "p90_ending",
        "prob_survival", "prob_double", "median_max_drawdown", "expected_growth_mult",
    ]
    show_cols = [c for c in show_cols if c in rules_df.columns]
    st.dataframe(rules_df[show_cols], use_container_width=True, hide_index=True)

    g1, g2 = st.columns(2)
    with g1:
        st.subheader("P(survive) by rule")
        fig = px.bar(rules_df, x="rule", y="prob_survival", range_y=[0, 1],
                     color="rule", text="prob_survival")
        fig.update_traces(texttemplate="%{text:.2f}", textposition="outside")
        fig.update_layout(showlegend=False, height=360, margin=dict(t=10))
        st.plotly_chart(fig, use_container_width=True)
    with g2:
        st.subheader("Expected growth multiple by rule")
        fig = px.bar(rules_df, x="rule", y="expected_growth_mult",
                     color="rule", text="expected_growth_mult")
        fig.update_traces(texttemplate="%{text:.2f}x", textposition="outside")
        fig.update_layout(showlegend=False, height=360, margin=dict(t=10))
        st.plotly_chart(fig, use_container_width=True)


# --------------------------------------------------------------------------- #
# Page: Signals & Alpha
# --------------------------------------------------------------------------- #
def page_signals_alpha() -> None:
    st.header("Signals & Alpha")

    # ---- entry signals ----
    st.subheader("Entry signals — elite vs field")
    signals = load_artifact("signals", "entry_signals")
    if not signals:
        _missing("Entry signals")
    else:
        findings = signals.get("findings", {})
        if findings:
            f1, f2, f3 = st.columns(3)
            f1.metric("Elite realized edge", findings.get("elite_avg_realized_edge", "—"))
            f2.metric("Field realized edge", findings.get("field_avg_realized_edge", "—"))
            f3.metric("Elite ρ(lead, ROI)", findings.get("elite_rho_lead_vs_roi", "—"))

        def _bucket_block(by_key: str, title: str) -> None:
            st.markdown(f"**{title}**")
            cols = st.columns(2)
            for col, who in zip(cols, ("elite", "field")):
                with col:
                    st.caption(who.capitalize())
                    table = (signals.get(who, {}) or {}).get(by_key, [])
                    if table:
                        st.dataframe(pd.DataFrame(table), use_container_width=True,
                                     hide_index=True)
                    else:
                        st.caption("No buckets met the minimum sample size.")

        _bucket_block("by_entry_timing", "By entry timing (days before resolution)")
        _bucket_block("by_entry_odds", "By entry odds (implied probability paid)")

    st.divider()

    # ---- category alpha ----
    st.subheader("Category alpha — where the edge lives (elite)")
    alpha = load_artifact("category_alpha", "by_category")
    if not alpha:
        _missing("Category alpha")
        return

    elite_df = D.category_alpha_to_frame(alpha.get("elite_by_category", {}))
    if elite_df.empty:
        st.caption("No elite category stats available.")
    else:
        show_cols = [
            "category", "n_positions", "n_wallets", "avg_roi", "median_roi",
            "win_rate", "total_profit_usd", "roi_std", "roi_per_unit_risk",
            "top_decile_profit_share",
        ]
        show_cols = [c for c in show_cols if c in elite_df.columns]
        st.dataframe(elite_df[show_cols].sort_values("avg_roi", ascending=False),
                     use_container_width=True, hide_index=True)

        fig = px.bar(
            elite_df.sort_values("avg_roi", ascending=False),
            x="category", y="avg_roi", color="category", text="avg_roi",
        )
        fig.update_traces(texttemplate="%{text:.1%}", textposition="outside")
        fig.update_layout(showlegend=False, height=380, yaxis_title="avg ROI",
                          margin=dict(t=10))
        st.plotly_chart(fig, use_container_width=True)

    rankings = alpha.get("rankings", {})
    if rankings:
        st.subheader("Niche rankings (elite view)")
        st.dataframe(
            pd.DataFrame(
                [{"question": k.replace("_", " "), "answer": v} for k, v in rankings.items()]
            ),
            use_container_width=True, hide_index=True,
        )


# --------------------------------------------------------------------------- #
# Router
# --------------------------------------------------------------------------- #
PAGES = {
    "Overview": page_overview,
    "Wallet detail": page_wallet_detail,
    "Network": page_network,
    "Clusters": page_clusters,
    "Backtests": page_backtests,
    "Small account ($20)": page_small_account,
    "Signals & Alpha": page_signals_alpha,
}


def main() -> None:
    st.sidebar.title("Polymarket Trader Research")
    stats = load_overview()
    st.sidebar.caption(
        f"Source: {stats['data_source']} · {stats['n_ranked']:,} ranked "
        f"· as of {(stats['as_of'] or 'n/a')[:10]}"
    )
    choice = st.sidebar.radio("View", list(PAGES.keys()))
    st.sidebar.markdown("---")
    st.sidebar.caption(
        "Reads directly from the research DB. Start the API separately with "
        "`uvicorn api.main:app --port 8000` if you need the JSON endpoints."
    )
    PAGES[choice]()


if __name__ == "__main__":
    main()
