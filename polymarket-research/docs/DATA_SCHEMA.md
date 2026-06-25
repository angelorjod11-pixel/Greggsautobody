# Data Schema

The canonical schema is the SQLAlchemy ORM in `polytrader/db/models.py`. It has
**9 tables** in three layers:

- **Raw event layer** (`wallets`, `markets`, `trades`) — populated by the
  collectors or the synthetic generator.
- **Derived layer** (`positions`) — round-trip positions reconstructed from
  trades.
- **Analytics outputs** (`wallet_metrics`, `wallet_scores`, `wallet_clusters`,
  `wallet_pairs`, `analysis_artifacts`) — rebuilt by the ETL + analytics stages.

> **Engine & idempotency.** Every column type used here exists on **both SQLite**
> (the zero-config default at `data/polytrader.db`) **and PostgreSQL** (production
> via `POLYTRADER_DATABASE_URL=postgresql+psycopg2://...`). `JSON` columns hold
> flexible nested payloads. The raw event tables are the source of truth;
> **every derived table is rebuilt idempotently from the raw events** (each
> analytics stage truncates its own table(s) and recomputes), so an analysis run
> is fully reproducible from the raw events alone. SQLite connections set
> `PRAGMA foreign_keys=ON` and `journal_mode=WAL`.

Money is stored as USD floats. Polymarket outcome tokens settle at exactly $1
(win) or $0 (loss), which is what makes cost-basis / ROI accounting clean.

---

## Raw event layer

### `wallets` — *raw event*
One row per trading address. Populated by ingest.

| Column | Type | Meaning | Derivation / source |
|---|---|---|---|
| `address` *(PK)* | String(64) | Wallet address (e.g. `0x…`) | Ingest |
| `label` | String(128), null | Optional label. In synthetic mode holds the archetype (`leader`/`follower`/`skilled`/`noise`); in live mode a known tag if any. | Ingest |
| `first_seen` | DateTime, null | Timestamp of the wallet's earliest trade | Min trade timestamp at ingest |
| `last_seen` | DateTime, null | Timestamp of the wallet's latest trade | Max trade timestamp at ingest |

### `markets` — *raw event*
One row per market (binary outcome markets). Populated by ingest.

| Column | Type | Meaning | Derivation / source |
|---|---|---|---|
| `id` *(PK)* | String(80) | Market / condition id | Ingest |
| `slug` | String(256), null | URL slug | Ingest |
| `question` | Text, null | Market question text | Ingest |
| `category` | String(40), null, **indexed** | Category (Politics/Sports/Crypto/Economics/News) | Keyword routing / explicit field |
| `created_at` | DateTime, null | Market creation time | Ingest |
| `end_date` | DateTime, null | Scheduled resolution/end time | Ingest |
| `resolved` | Boolean (default False) | Whether the market has resolved | Ingest |
| `resolved_at` | DateTime, null | Resolution timestamp | Ingest |
| `winning_outcome` | Integer, null | Index of the winning outcome (0/1 for binary); NULL if unresolved | Ingest |
| `volume_usd` | Float (default 0) | Total traded volume (USD) | Ingest |
| `liquidity_usd` | Float (default 0) | Market liquidity (USD) | Ingest |

### `trades` — *raw event*
A single fill: a wallet buys/sells `shares` of an outcome token at `price`. This
is the atomic input to the whole pipeline.

| Column | Type | Meaning | Derivation / source |
|---|---|---|---|
| `id` *(PK)* | String(120) | Unique fill id (`tx_hash:logindex` live, or synthetic id) | Ingest |
| `wallet_address` | FK→`wallets.address`, **indexed** | Trading wallet | Ingest |
| `market_id` | FK→`markets.id`, **indexed** | Market traded | Ingest |
| `outcome_index` | Integer (default 0) | Which outcome token | Ingest |
| `side` | String(4) | `BUY` or `SELL` | Ingest |
| `timestamp` | DateTime, **indexed** | Fill time | Ingest |
| `price` | Float | Implied probability paid, in [0,1] | Ingest |
| `shares` | Float | Outcome tokens (settle at $1 or $0) | Ingest |
| `usd_size` | Float | `price × shares` | Ingest |
| `tx_hash` | String(80), null | On-chain tx hash (live) | Ingest |

**Indexes:** `ix_trades_wallet_market (wallet_address, market_id)`,
`ix_trades_market_ts (market_id, timestamp)`, plus the per-column indexes above.

---

## Derived layer

### `positions` — *derived* (from `trades`)
A wallet's net round-trip exposure to one outcome of one market — the unit of
P&L / ROI. Built by `etl/positions.py` (truncate-and-rebuild). All BUY/SELL fills
for each `(wallet, market, outcome)` are netted; residual shares settle at the
resolved payout.

| Column | Type | Meaning | Derivation |
|---|---|---|---|
| `id` *(PK)* | Integer (autoinc) | Surrogate key | — |
| `wallet_address` | FK→`wallets.address`, **indexed** | Wallet | group key |
| `market_id` | FK→`markets.id`, **indexed** | Market | group key |
| `outcome_index` | Integer (default 0) | Outcome token | group key |
| `opened_at` | DateTime, **indexed** | First BUY time | `min(BUY timestamp)` |
| `closed_at` | DateTime, null | Close time | `last_sell` if exited early; else `resolved_at`; NULL if open |
| `shares` | Float | Peak (long) shares bought | `Σ BUY shares` |
| `avg_entry_price` | Float | Cost-weighted entry probability | `cost_basis_usd / bought_shares` |
| `cost_basis_usd` | Float | Total $ paid to enter | `Σ BUY usd_size` |
| `proceeds_usd` | Float (default 0) | $ from selling early | `Σ SELL usd_size` |
| `settlement_usd` | Float (default 0) | $ from resolution payout | `net_shares × 1` if held outcome won & resolved, else 0 |
| `realized_pnl_usd` | Float (default 0) | Realized P&L (resolved only) | `proceeds + settlement − cost_basis` |
| `roi` | Float (default 0) | Return on cost basis | `realized_pnl / cost_basis` |
| `duration_hours` | Float (default 0) | Holding time | `(closed_at − opened_at)` in hours, ≥0 |
| `status` | String(10) (default `open`) | `open` (unresolved) / `closed` (exited pre-resolution) / `settled` (held to resolution) | from `resolved` + net shares |
| `is_win` | Boolean, null | Won (resolved positions); NULL if open | `realized_pnl_usd > 0` when resolved |

**Constraints / indexes:**
`UniqueConstraint uq_position (wallet_address, market_id, outcome_index, opened_at)`,
`ix_pos_wallet_status (wallet_address, status)`, plus per-column indexes above.
Only `settled`/`closed` positions carry realized performance; `open` positions
are excluded from all metrics (look-ahead safety).

---

## Analytics outputs

### `wallet_metrics` — *derived* (from `positions`)
One row per wallet per analysis run (`as_of`) — the full metric vector. Built by
`etl/metrics.py` over resolved positions. Composite PK `(wallet_address, as_of)`.
See **METHODOLOGY §2** for exact metric formulas.

| Column | Type | Meaning |
|---|---|---|
| `wallet_address` *(PK)* | FK→`wallets.address` | Wallet |
| `as_of` *(PK)* | DateTime | Analysis snapshot time |
| `n_positions` | Integer | # resolved positions |
| `n_markets` | Integer | # distinct markets |
| `active_days` | Float | First-to-last span (days, ≥1) |
| `trades_per_active_month` | Float | `n_positions / active_months` |
| `capital_deployed_usd` | Float | Σ cost basis |
| `total_profit_usd` | Float | Σ realized P&L |
| `avg_roi` | Float | Mean per-position ROI |
| `median_roi` | Float | Median per-position ROI |
| `profit_factor` | Float | Gross gains / gross losses |
| `win_rate` | Float | Fraction of positions won |
| `sharpe` | Float | Per-position mean(ROI)/std(ROI), no annualization |
| `max_drawdown` | Float | Equity-curve peak-to-trough fraction [0,1] |
| `roi_stability` | Float | `1/(1+cv(monthly pnl))` |
| `positive_month_rate` | Float | Fraction of active months profitable |
| `avg_position_size_usd` | Float | Mean cost basis / position |
| `position_size_cv` | Float | CV of position cost basis (sizing discipline) |
| `median_hold_hours` | Float | Median holding time |
| `entry_lead_days` | Float | Mean days entered before resolution |
| `markets_per_active_month` | Float | Breadth |
| `category_concentration` | Float | HHI over category exposure by capital |
| `avg_entry_price` | Float | Capital-weighted mean entry probability |
| `top_category` | String(40), null | Largest-capital-share category |
| `eligible` | Boolean, **indexed** | Passed ALL eligibility filters |
| `extra` | JSON | `{ "category_exposure": {cat: share}, "monthly_pnl": {"YYYY-MM": pnl} }` — `monthly_pnl` is reused by the correlation engine's `profit_corr` |

### `wallet_scores` — *derived* (from `wallet_metrics` + `positions`)
One row per eligible wallet per run. Built by `ranking/scorer.py`. Composite PK
`(wallet_address, as_of)`. See **METHODOLOGY §4–5**.

| Column | Type | Meaning |
|---|---|---|
| `wallet_address` *(PK)* | FK→`wallets.address` | Wallet |
| `as_of` *(PK)* | DateTime | Snapshot time |
| `composite_score` | Float, **indexed** | Final score in [0,100] |
| `rank` | Integer, **indexed** | 1-based rank by composite score (1 = best) |
| `profitability_score` | Float | Profitability pillar sub-score ×100 |
| `win_rate_score` | Float | Win-rate pillar sub-score ×100 |
| `consistency_score` | Float | Consistency pillar sub-score ×100 |
| `risk_score` | Float | Risk pillar sub-score ×100 |
| `longevity_score` | Float | Longevity pillar sub-score ×100 |
| `score_ci_low` | Float | Bootstrap 5th-percentile composite (90% CI low) |
| `score_ci_high` | Float | Bootstrap 95th-percentile composite (90% CI high) |

### `wallet_clusters` — *derived* (from `wallet_metrics`; `community` from network)
One row per clustered (eligible) wallet per run. K-means/hierarchical/PCA written
by `clustering/cluster.py`; `community` is later set by `correlation/network.py`.
Composite PK `(wallet_address, as_of)`.

| Column | Type | Meaning |
|---|---|---|
| `wallet_address` *(PK)* | FK→`wallets.address` | Wallet |
| `as_of` *(PK)* | DateTime | Snapshot time |
| `kmeans_cluster` | Integer (default −1) | K-means cluster id (k chosen by silhouette) |
| `hierarchical_cluster` | Integer (default −1) | Agglomerative (Ward) cluster id at same k |
| `pca_x` | Float (default 0) | PCA component-1 coordinate |
| `pca_y` | Float (default 0) | PCA component-2 coordinate |
| `community` | Integer (default −1) | Louvain community id from the co-trading graph (−1 = none) |

### `wallet_pairs` — *derived* (from `positions` over the correlation universe)
Pairwise relationship metrics over the top-N ranked wallets. Built by
`correlation/network.py` (stores up to the top 5000 pairs by `timing_jaccard`).
See **METHODOLOGY §7**.

| Column | Type | Meaning |
|---|---|---|
| `id` *(PK)* | Integer (autoinc) | Surrogate key |
| `as_of` | DateTime, **indexed** | Snapshot time |
| `wallet_a` | String(64), **indexed** | First wallet of the pair |
| `wallet_b` | String(64), **indexed** | Second wallet of the pair |
| `n_shared_markets` | Integer | # markets both traded |
| `market_overlap` | Float | Jaccard over traded-market sets |
| `timing_jaccard` | Float | Co-entry (same side within `cofollow_window_hours`) ÷ union size |
| `direction_corr` | Float | Same-side agreement `(same−opp)/n_shared` ∈ [−1,1] |
| `profit_corr` | Float | Pearson corr of monthly-P&L vectors |
| `lead_lag_hours` | Float | Mean `(entry_b − entry_a)`; **+ve ⇒ a leads b** |

**Index:** `ix_pair_ab (wallet_a, wallet_b)`.

### `analysis_artifacts` — *derived* (generic JSON store)
A generic key/value JSON store for run-level outputs that don't merit a wide
table (cluster profiles, backtest results, signal/odds tables, category alpha,
Monte-Carlo summaries, leader-follower lists, run metadata, synthetic ground
truth). Each analytics stage deletes its own `kind` rows before re-inserting, so
artifacts are rebuilt idempotently.

| Column | Type | Meaning |
|---|---|---|
| `id` *(PK)* | Integer (autoinc) | Surrogate key |
| `as_of` | DateTime, **indexed** | Snapshot time |
| `kind` | String(48), **indexed** | Artifact category (see table below) |
| `name` | String(80) | Sub-name within the kind |
| `payload` | JSON | Arbitrary nested payload |

**Index:** `ix_artifact_kind_name (kind, name)`.

#### Artifact `kind` / `name` values and payloads

| `kind` | `name`(s) | Written by | Payload contents |
|---|---|---|---|
| `ground_truth` | `wallet_skill` | `synth/generator.py` | Synthetic-only. Map `address → {skill, archetype}` (the latent skill in [0,1] and archetype `leader`/`follower`/`skilled`/`noise`), persisted so the report can validate `Spearman(composite score, latent skill)`. |
| `cluster_profile` | `kmeans` | `clustering/cluster.py` | `{ k, silhouette, silhouette_by_k: {k: score}, pca_explained_variance: [..], features: [..], clusters: [{cluster_id, size, label, mean_composite_score, share_profitable, profile: {feature means}}] }`. |
| `network_summary` | `top_wallets` | `correlation/network.py` | `{ universe_size, n_pairs_evaluated, avg_market_overlap, avg_timing_jaccard, avg_direction_corr, avg_profit_corr, top10_avg_market_overlap, graph: {nodes, edges, communities, modularity}, n_leaders_detected, interpretation: {top_traders_enter_same_markets, top_traders_enter_similar_times, leaders_present, coordinated_communities} }`. |
| `leader_follower` | `leaders` | `correlation/network.py` | `{ leaders: [{wallet, rank, lead_score, n_followers, followers: [(addr, rank), …]}] }` (top 15 by lead score). |
| `backtest` | `A_copy_top10`, `B_consensus_3plus`, `C_filtered_elite`, `D_weighted_consensus` | `backtest/engine.py` | Per strategy: `{ n_trades, n_skipped_concurrency, starting_balance, ending_balance, total_return, cagr, win_rate, avg_trade_return, max_drawdown, sharpe_per_trade, equity_curve: [[ts, equity], …], description, n_selected_wallets }`. |
| `backtest_meta` | `meta` | `backtest/engine.py` | `{ t_split, as_of, n_train_wallets, n_test_signals, train_fraction }` — the chronological train/test split metadata. |
| `small_account` | `summary` | `smallaccount/kelly.py` | `{ bankroll_usd, constraints: {min_bet, max_bet, slippage_bps, horizon_trades}, scenarios: { in_sample_edge: {…}, haircut_edge: {…} }, recommended_rule, recommendation_basis }`. Each scenario: `{ n_trades_in_edge_sample, mean_per_trade_return, std_per_trade_return, win_rate, full_kelly_fraction, fractional_kelly_used, recommended_bet_at_20, sizing_rules: [{rule, median/mean/p10/p90_ending, prob_survival, prob_ruin, prob_double, median_max_drawdown, expected_growth_mult}] }`. |
| `signals` | `entry_signals` | `signals/detect.py` | `{ as_of, elite: {by_entry_timing: [...], by_entry_odds: [...]}, field: {…}, findings: {elite/field avg entry price & realized edge, elite_rho_lead_vs_roi, elite_rho_entryprice_vs_roi, interpretation: {edge_lives_earlier, elite_exploit_mispricing, elite_prefer_longshots}} }`. Each bucket row: `{bucket, n, win_rate, avg_roi, median_roi, avg_realized_edge}`. |
| `category_alpha` | `by_category` | `alpha/categories.py` | `{ as_of, field_by_category: {cat: stats}, elite_by_category: {cat: stats}, rankings: {most_profitable_niche, most_repeatable_niche, lowest_variance_profitable_niche, best_risk_adjusted_niche} }`. Per-category stats: `{n_positions, n_wallets, avg_roi, median_roi, win_rate, total_profit_usd, roi_std, roi_per_unit_risk, top_decile_profit_share}`. |

---

## Storage notes

- **SQLite by default.** With no configuration, the engine is a local file at
  `data/polytrader.db` (created automatically; parent directory ensured). SQLite
  is set to enforce foreign keys and use WAL journaling for concurrent reads.
- **PostgreSQL-ready.** Set
  `POLYTRADER_DATABASE_URL=postgresql+psycopg2://user:pass@host:5432/polytrader`
  (install the `postgres` extra). The schema uses only column types common to
  both engines, so no DDL changes are needed.
- **Idempotent derived tables.** `positions`, `wallet_metrics`, `wallet_scores`,
  `wallet_clusters`, `wallet_pairs`, and each `analysis_artifacts` kind are
  truncated and recomputed by their owning stage. The raw event tables
  (`wallets`, `markets`, `trades`) are the only source of truth; the entire
  derived layer can be regenerated from them, which is what makes a run
  reproducible. `python -m polytrader.cli all` (without `--keep`) drops and
  rebuilds everything for a clean snapshot.
