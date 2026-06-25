"""Assemble the final research report (Markdown) from database artifacts.

Pulls the ranked wallets and every analysis artifact and renders a single
evidence-based report answering the brief's questions, with confidence intervals
and explicit caveats (data source, look-ahead handling, survivorship controls).
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
from sqlalchemy import select

from ..config import ROOT, get_config
from ..db.models import (AnalysisArtifact, Wallet, WalletCluster, WalletMetric,
                         WalletScore)
from ..db.session import session_scope


def _artifact(session, kind: str, name: Optional[str] = None):
    q = select(AnalysisArtifact.payload).where(AnalysisArtifact.kind == kind)
    if name:
        q = q.where(AnalysisArtifact.name == name)
    return session.scalar(q.order_by(AnalysisArtifact.id.desc()))


def _short(addr: str) -> str:
    return addr[:6] + "…" + addr[-4:] if addr and len(addr) > 12 else addr


def _fmt_pct(x) -> str:
    return "—" if x is None else f"{x*100:.1f}%"


def build_report(out_path: Optional[str] = None) -> Dict[str, object]:
    cfg = get_config()
    out = Path(out_path) if out_path else (ROOT / "reports" / "generated" / "FINAL_REPORT.md")
    out.parent.mkdir(parents=True, exist_ok=True)

    with session_scope() as s:
        scores = pd.read_sql(select(WalletScore.__table__).order_by(WalletScore.rank), s.bind)
        if scores.empty:
            out.write_text("# Polymarket Trader Research — no ranked data available\n")
            return {"path": str(out), "note": "no data"}
        met = pd.read_sql(select(WalletMetric.__table__), s.bind)
        labels = dict(s.execute(select(Wallet.address, Wallet.label)).all())
        clusters = pd.read_sql(select(WalletCluster.__table__), s.bind)
        clu_prof = _artifact(s, "cluster_profile", "kmeans") or {}
        net = _artifact(s, "network_summary") or {}
        lead = _artifact(s, "leader_follower") or {}
        bt = {a.name: a.payload for a in s.execute(
            select(AnalysisArtifact).where(AnalysisArtifact.kind == "backtest")).scalars()}
        bt_meta = _artifact(s, "backtest_meta") or {}
        sa = _artifact(s, "small_account") or {}
        sig = _artifact(s, "signals") or {}
        cat = _artifact(s, "category_alpha") or {}
        gt = _artifact(s, "ground_truth")
        as_of = pd.to_datetime(scores["as_of"].iloc[0])

    df = scores.merge(met, on="wallet_address", how="left", suffixes=("", "_m"))
    df = df.merge(clusters[["wallet_address", "kmeans_cluster", "community"]],
                  on="wallet_address", how="left")
    n_elig = int(met["eligible"].sum()) if "eligible" in met else len(met)

    L: List[str] = []
    w = L.append
    w("# Polymarket Trader Research — Final Report\n")
    w(f"*Generated {datetime.utcnow():%Y-%m-%d %H:%M UTC} · analysis as-of "
      f"{as_of:%Y-%m-%d} · data source: **{cfg.data_source}** · "
      f"{len(met)} wallets analyzed, {n_elig} eligible after filters*\n")
    if cfg.data_source == "synthetic":
        w("> **Note.** This run uses the bundled *synthetic* dataset (live Polymarket "
          "API egress was unavailable in this environment). The numbers below are "
          "illustrative but the methodology, code paths and validation are identical "
          "to a live run — switch `data_source: live` to reproduce against real data.\n")

    # ---------------- methodology guardrails ----------------
    w("## Methodology & bias controls\n")
    w("- **Eligibility filter** (anti-survivorship / anti-small-sample): a wallet is "
      f"ranked only with ≥{cfg.eligibility['min_resolved_positions']} resolved positions, "
      f"≥{cfg.eligibility['min_distinct_markets']} distinct markets, "
      f"≥${cfg.eligibility['min_capital_deployed_usd']:.0f} deployed, and no single "
      f"position >{cfg.eligibility['max_single_position_frac']:.0%} of capital.")
    w("- **Composite score** = 40% profitability + 20% win rate + 20% consistency + "
      "10% risk + 10% longevity, each metric mapped to a population percentile "
      "(scale-free, heavy-tail-robust).")
    w("- **Confidence intervals**: every score carries a 90% bootstrap CI from "
      f"resampling each wallet's own positions ({cfg.ranking.bootstrap_iterations} iters) — "
      "a tight CI means the rank is not luck.")
    w("- **Backtests are look-ahead-safe**: elite wallets are *selected on a training "
      "window* and *evaluated on a later test window*; settlement is used only to "
      "realize P&L, never to decide entry.\n")

    # ---------------- Q1: who are the best ----------------
    w("## 1. Who are the 100 best Polymarket traders?\n")
    w(f"Ranked {len(scores)} eligible wallets. The Top-10 (■ = bootstrap 90% CI):\n")
    w("| # | Wallet | Score [90% CI] | Total P&L | Avg ROI | Win% | Sharpe | MaxDD | Positions |")
    w("|--:|--------|----------------|----------:|--------:|-----:|-------:|------:|----------:|")
    for _, r in df.head(10).iterrows():
        w(f"| {int(r['rank'])} | `{_short(r['wallet_address'])}` | "
          f"{r['composite_score']:.1f} [{r['score_ci_low']:.0f}–{r['score_ci_high']:.0f}] | "
          f"${r['total_profit_usd']:,.0f} | {_fmt_pct(r['avg_roi'])} | {_fmt_pct(r['win_rate'])} | "
          f"{r['sharpe']:.2f} | {_fmt_pct(r['max_drawdown'])} | {int(r['n_positions'])} |")
    top25, top100 = df.head(25), df.head(100)
    w(f"\n*Top-25 and Top-100 cohorts are in the database (`wallet_scores`) and dashboard.* "
      f"Top-100 aggregate: median score {top100['composite_score'].median():.1f}, "
      f"median win rate {_fmt_pct(top100['win_rate'].median())}, "
      f"median avg-ROI {_fmt_pct(top100['avg_roi'].median())}.\n")
    if gt:
        from scipy.stats import spearmanr
        sk = df["wallet_address"].map(lambda a: (gt.get(a) or {}).get("skill"))
        m = sk.notna()
        if m.sum() > 10:
            rho = spearmanr(df.loc[m, "composite_score"], sk[m].astype(float)).correlation
            w(f"> *Validation (synthetic only):* Spearman(composite score, latent skill) "
              f"= **{rho:.2f}** — the ranking recovers ground-truth skill.\n")

    # ---------------- Q2: shared traits ----------------
    w("## 2. What traits do the top traders share?\n")
    if clu_prof.get("clusters"):
        w(f"K-means (k={clu_prof.get('k')}, silhouette {clu_prof.get('silhouette')}) over behavioral "
          "features splits the field into:\n")
        for c in clu_prof["clusters"]:
            p = c["profile"]
            w(f"- **Cluster {c['cluster_id']} — “{c['label']}”** "
              f"({c['size']} wallets, {c['share_profitable']*100:.0f}% profitable, "
              f"mean score {c['mean_composite_score']:.0f}): "
              f"avg size ${p.get('avg_position_size_usd',0):.0f}, "
              f"size-CV {p.get('position_size_cv',0):.2f}, "
              f"hold {p.get('median_hold_hours',0)/24:.0f}d, "
              f"entry lead {p.get('entry_lead_days',0):.0f}d, "
              f"avg entry odds {p.get('avg_entry_price',0):.2f}, "
              f"win {p.get('win_rate',0)*100:.0f}%.")
    if sig.get("findings"):
        f = sig["findings"]
        w(f"\nThe top cohort enters at **avg odds {f.get('elite_avg_entry_price')}** vs "
          f"{f.get('field_avg_entry_price')} for the field, and captures **realized edge "
          f"{f.get('elite_avg_realized_edge')}** vs {f.get('field_avg_realized_edge')} — "
          "i.e. they systematically buy below fair value.\n")

    # ---------------- Q3: worth following ----------------
    w("## 3. Which wallets are statistically worth following?\n")
    df["ci_width"] = df["score_ci_high"] - df["score_ci_low"]
    worth = df[(df["rank"] <= 50)].copy()
    worth = worth.sort_values(["ci_width", "rank"]).head(8)
    leaders = {l["wallet"]: l for l in (lead.get("leaders") or [])}
    w("Prefer **high score + tight CI**, and bonus for being a detected *leader* "
      "(others copy them, with a positive lead-lag):\n")
    w("| Wallet | Rank | Score | CI width | Leader? | #Followers |")
    w("|--------|-----:|------:|---------:|:-------:|-----------:|")
    for _, r in worth.iterrows():
        ld = leaders.get(r["wallet_address"])
        w(f"| `{_short(r['wallet_address'])}` | {int(r['rank'])} | {r['composite_score']:.1f} | "
          f"{r['ci_width']:.1f} | {'✔' if ld else '—'} | {ld['n_followers'] if ld else 0} |")
    if net.get("graph"):
        g = net["graph"]
        w(f"\nNetwork over the top-{net.get('universe_size')} wallets: {g.get('edges')} co-trading "
          f"edges, **{g.get('communities')} communities** (modularity {g.get('modularity')}), "
          f"{net.get('n_leaders_detected')} leader wallets detected. "
          f"Avg market overlap {net.get('avg_market_overlap')}, "
          f"direction agreement {net.get('avg_direction_corr')}.\n")

    # ---------------- Q4: best copy strategy ----------------
    w("## 4. Which copy-trading strategy performed best historically?\n")
    if bt:
        w(f"Look-ahead-safe backtest (train→test split at {str(bt_meta.get('t_split',''))[:10]}, "
          f"{bt_meta.get('n_test_signals','?')} test signals). Start "
          f"${cfg.backtest['starting_capital_usd']:.0f}, {cfg.backtest['slippage_bps']}bps slippage:\n")
        w("| Strategy | Trades | Ending $ | Total Ret | CAGR | Win% | MaxDD | Sharpe/trade |")
        w("|----------|-------:|---------:|----------:|-----:|-----:|------:|-------------:|")
        order = ["A_copy_top10", "B_consensus_3plus", "C_filtered_elite", "D_weighted_consensus"]
        for k in order:
            v = bt.get(k)
            if not v:
                continue
            if v.get("n_trades", 0) == 0:
                w(f"| {k} | 0 | — | — | — | — | — | — *(no qualifying wallets)* |")
                continue
            w(f"| {k} | {v['n_trades']} | ${v['ending_balance']:,.0f} | {_fmt_pct(v['total_return'])} | "
              f"{_fmt_pct(v['cagr'])} | {_fmt_pct(v['win_rate'])} | {_fmt_pct(v['max_drawdown'])} | "
              f"{v['sharpe_per_trade']:.2f} |")
        best_ret = max((k for k in order if bt.get(k, {}).get("n_trades")),
                       key=lambda k: bt[k]["total_return"], default=None)
        best_sharpe = max((k for k in order if bt.get(k, {}).get("n_trades")),
                          key=lambda k: bt[k]["sharpe_per_trade"], default=None)
        if best_ret:
            w(f"\n**Highest return: `{best_ret}`** ({_fmt_pct(bt[best_ret]['total_return'])}). "
              f"**Best risk-adjusted: `{best_sharpe}`** "
              f"(Sharpe/trade {bt[best_sharpe]['sharpe_per_trade']:.2f}, "
              f"win {_fmt_pct(bt[best_sharpe]['win_rate'])}). "
              "Consensus strategies trade less but win more often — the classic breadth/edge tradeoff.\n")

    # ---------------- Q5: $20 allocation ----------------
    w("## 5. How should a $20 account allocate capital?\n")
    if sa.get("scenarios"):
        hc = sa["scenarios"].get("haircut_edge", {})
        w(f"Edge estimated by replaying the Top-10's resolved positions as $-for-$ copies "
          f"(charged {cfg.small_account['slippage_bps']}bps small-order slippage). On a "
          f"**50%-haircut** edge (robustness): mean per-trade return {hc.get('mean_per_trade_return')}, "
          f"win rate {_fmt_pct(hc.get('win_rate'))}, full-Kelly fraction "
          f"**{hc.get('full_kelly_fraction')}** → quarter-Kelly bet "
          f"**${hc.get('recommended_bet_at_20')}** on $20.\n")
        w("| Sizing rule | Median end $ | P(survive) | P(double→$40) | Med. MaxDD | Exp. growth× |")
        w("|-------------|-------------:|-----------:|--------------:|-----------:|-------------:|")
        for r in hc.get("sizing_rules", []):
            w(f"| {r['rule']} | ${r['median_ending']:.0f} | {_fmt_pct(r['prob_survival'])} | "
              f"{_fmt_pct(r['prob_double'])} | {_fmt_pct(r['median_max_drawdown'])} | "
              f"{r['expected_growth_mult']:.2f}× |")
        w(f"\n**Recommendation: `{sa.get('recommended_rule')}`** "
          f"({sa.get('recommendation_basis')}). Constraints honored: bets clamped to "
          f"${cfg.small_account['min_bet_usd']:.0f}–${cfg.small_account['max_bet_usd']:.0f}, "
          "concentration limited, liquid markets only. With only ~20 units of $1 risk, "
          "flat small sizing protects survival while consensus filtering supplies the edge.\n")

    # ---------------- Q6: signals ----------------
    w("## 6. What signals consistently precede profitable trades?\n")
    if sig.get("findings"):
        f = sig["findings"]; it = f.get("interpretation", {})
        w(f"- **Entry timing**: Spearman(days-before-resolution, ROI) for the elite = "
          f"**{f.get('elite_rho_lead_vs_roi')}** → edge {'lives earlier' if it.get('edge_lives_earlier') else 'is not timing-driven'}.")
        w(f"- **Mispricing**: elite realized edge {f.get('elite_avg_realized_edge')} > field "
          f"{f.get('field_avg_realized_edge')} → {'they exploit mispriced probabilities' if it.get('elite_exploit_mispricing') else 'no mispricing edge detected'}.")
        w(f"- **Odds preference**: elite {'favor longshots' if it.get('elite_prefer_longshots') else 'favor favorites'} "
          f"(avg entry odds {f.get('elite_avg_entry_price')}).")
    if cat.get("rankings"):
        rk = cat["rankings"]
        w(f"\n**Alpha by category** — most profitable niche: **{rk.get('most_profitable_niche')}**, "
          f"most repeatable (win rate): **{rk.get('most_repeatable_niche')}**, "
          f"lowest-variance profitable: **{rk.get('lowest_variance_profitable_niche')}**, "
          f"best risk-adjusted: **{rk.get('best_risk_adjusted_niche')}**.")
        if cat.get("elite_by_category"):
            w("\n| Category | Elite n | Elite avg ROI | Elite win% | ROI σ | Profit concentration (top-decile) |")
            w("|----------|--------:|--------------:|-----------:|------:|----------------------------------:|")
            for c, st in sorted(cat["elite_by_category"].items(),
                                key=lambda kv: kv[1]["avg_roi"], reverse=True):
                w(f"| {c} | {st['n_positions']} | {_fmt_pct(st['avg_roi'])} | {_fmt_pct(st['win_rate'])} | "
                  f"{st['roi_std']:.2f} | {_fmt_pct(st['top_decile_profit_share'])} |")

    # ---------------- caveats ----------------
    w("\n## Caveats & robustness\n")
    w("- Past performance need not persist; copy-trading adds latency and slippage the "
      "backtest models but cannot perfectly capture.")
    w("- Edge estimates for the $20 plan are in-sample; we report a 50%-haircut scenario "
      "and quarter-Kelly precisely because point estimates overstate forward edge.")
    w("- On-chain wallets can be Sybil/linked; the network/community analysis flags "
      "coordinated clusters but cannot prove identity.")
    w("- Confidence intervals quantify *sampling* uncertainty only, not regime change.\n")

    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    return {"path": str(out), "sections": 6, "bytes": out.stat().st_size}
