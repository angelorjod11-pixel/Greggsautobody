# Polymarket Trader Research — REST API & Dashboard

This document describes the **read-only JSON API** (`api/main.py`, FastAPI) and the
**Streamlit dashboard** (`dashboard/app.py`) that sit on top of the populated
research database.

Both layers are read-only. They never write to the database; all analytics are
produced offline by the pipeline (`python -m polytrader.cli all`) and stored in
the SQL tables and the `analysis_artifacts` JSON store.

---

## Running

Set the two environment variables first (point the URL at your populated SQLite
file; PostgreSQL URLs work too):

```bash
export POLYTRADER_DATABASE_URL="sqlite:////abs/path/to/polymarket-research/data/polytrader.db"
export PYTHONPATH=/abs/path/to/polymarket-research
```

### API (FastAPI + uvicorn)

```bash
uvicorn api.main:app --reload --port 8000
```

- Interactive Swagger docs: <http://localhost:8000/docs>
- OpenAPI schema: <http://localhost:8000/openapi.json>

The API is self-contained: it reads the same DB the dashboard does, so it does
**not** require the dashboard to be running (and vice-versa).

### Dashboard (Streamlit)

```bash
streamlit run dashboard/app.py
```

The dashboard reads the database **directly** (it imports `polytrader` and the
`dashboard.data` helpers) and does **not** require the API to be running. It
opens on <http://localhost:8501> by default.

---

## Conventions

- All endpoints are `GET` and return `application/json`.
- Money is USD floats. ROI / win-rate / drawdown are fractions (e.g. `0.42` = 42%).
- Non-finite floats (`NaN`/`inf`) coming from pandas are serialised as `null`.
- Timestamps are ISO-8601 strings.
- Addresses are full lowercase hex (`0x…`, 42 chars).
- "Artifact" endpoints read a row from `analysis_artifacts`; if the pipeline has
  not produced that artifact the endpoint returns an empty default (e.g.
  `{"leaders": []}` or `{}`) with HTTP 200 rather than erroring.

---

## Endpoints

### `GET /health`

Liveness + data-source summary.

**Params:** none.

**Example response:**

```json
{
  "status": "ok",
  "data_source": "synthetic",
  "as_of": "2025-09-26T00:00:00",
  "n_wallets_ranked": 384
}
```

---

### `GET /wallets/top`

Top-N ranked wallets (from `wallet_scores`) joined to their headline metrics
(from `wallet_metrics`), ordered by `rank`.

**Params:**

| name | type | default | constraints | description |
|------|------|---------|-------------|-------------|
| `n`  | int  | `100`   | `1 … 1000`  | how many top wallets to return |

**Example response** (`/wallets/top?n=1`):

```json
[
  {
    "rank": 1,
    "address": "0x94b67cf361c9adfdfbb27c099b7cb3e4189d3911",
    "composite_score": 96.97,
    "score_ci_low": 96.04,
    "score_ci_high": 97.24,
    "total_profit_usd": 9270.15,
    "avg_roi": 0.4225,
    "win_rate": 0.6978,
    "sharpe": 0.3198,
    "max_drawdown": 0.0186,
    "n_positions": 225,
    "top_category": "News"
  }
]
```

---

### `GET /wallets/{address}`

Full detail for one wallet: its complete metric row, score row and cluster row
(latest `as_of` for each). Returns **404** if the address has no row in any of
those tables.

**Path params:** `address` — wallet hex address.

**Example response** (truncated):

```json
{
  "address": "0x94b67cf361c9adfdfbb27c099b7cb3e4189d3911",
  "metric": {
    "wallet_address": "0x94b6...",
    "as_of": "2025-09-26T00:00:00",
    "n_positions": 225,
    "total_profit_usd": 9270.15,
    "avg_roi": 0.4225,
    "win_rate": 0.6978,
    "sharpe": 0.3198,
    "max_drawdown": 0.0186,
    "top_category": "News",
    "eligible": true,
    "extra": { "monthly_pnl": { "...": 0.0 } }
  },
  "score": {
    "wallet_address": "0x94b6...",
    "composite_score": 96.97,
    "rank": 1,
    "profitability_score": 99.3,
    "win_rate_score": 99.7,
    "consistency_score": 91.9,
    "risk_score": 99.6,
    "longevity_score": 89.6,
    "score_ci_low": 96.04,
    "score_ci_high": 97.24
  },
  "cluster": {
    "wallet_address": "0x94b6...",
    "kmeans_cluster": 1,
    "hierarchical_cluster": 1,
    "pca_x": 6.54,
    "pca_y": 1.65,
    "community": 3
  }
}
```

**404 example:**

```json
{ "detail": "wallet 0xDEADBEEF not found" }
```

---

### `GET /clusters`

The `cluster_profile/kmeans` artifact (chosen `k`, silhouette, per-cluster
archetype profiles) plus the per-wallet PCA scatter coordinates from
`wallet_clusters`.

**Params:** none.

**Example response** (shape):

```json
{
  "profile": {
    "k": 2,
    "silhouette": 0.41,
    "silhouette_by_k": { "2": 0.41, "3": 0.30 },
    "pca_explained_variance": [0.51, 0.18],
    "features": ["avg_position_size_usd", "position_size_cv", "..."],
    "clusters": [
      {
        "cluster_id": 0,
        "size": 119,
        "label": "Sharp disciplined long-hold early-entrant",
        "mean_composite_score": 74.0,
        "share_profitable": 0.56,
        "profile": { "avg_roi": 0.036, "win_rate": 0.48, "median_hold_hours": 860.5 }
      }
    ]
  },
  "wallets": [
    { "address": "0x1908...", "pca_x": 2.776, "pca_y": -0.738, "kmeans_cluster": 1, "community": 5 }
  ]
}
```

`community = -1` means the wallet is not part of any detected network community.

---

### `GET /network`

The `network_summary` artifact plus the **500 strongest** co-trading edges
(from `wallet_pairs`, ordered by `timing_jaccard` descending) for graph
rendering.

**Params:** none.

**Example response** (shape):

```json
{
  "summary": {
    "universe_size": 100,
    "n_pairs_evaluated": 3165,
    "avg_market_overlap": 0.27,
    "avg_timing_jaccard": 0.18,
    "avg_direction_corr": 0.42,
    "avg_profit_corr": 0.31,
    "top10_avg_market_overlap": 0.40,
    "graph": { "nodes": 100, "edges": 54, "communities": 6, "modularity": 0.7705 },
    "n_leaders_detected": 15,
    "interpretation": { "top_traders_enter_same_markets": true, "leaders_present": true }
  },
  "edges": [
    {
      "wallet_a": "0x1908...",
      "wallet_b": "0x40f0...",
      "timing_jaccard": 0.7358,
      "market_overlap": 0.7453,
      "direction_corr": 1.0,
      "profit_corr": 0.7413,
      "n_shared_markets": 79,
      "lead_lag_hours": 15.42
    }
  ]
}
```

---

### `GET /leaders`

The `leader_follower` artifact — the leader→follower ranking derived from
lead-lag signs over co-trading pairs.

**Params:** none.

**Example response:**

```json
{
  "leaders": [
    {
      "wallet": "0x30bacacb0d1bf3003367077d32e468b70fac4ff5",
      "rank": 2,
      "lead_score": 697.0,
      "n_followers": 4,
      "followers": [["0x43f3...", 6], ["0x1ab6...", 7]]
    }
  ]
}
```

`followers` is a list of `[address, rank]` pairs (capped at 15 per leader).
When no leadership is detected the list is empty.

---

### `GET /backtests`

Copy-trading backtest results: the run metadata plus all four strategy payloads
keyed by strategy name.

**Params:** none.

**Strategies:** `A_copy_top10`, `B_consensus_3plus`, `C_filtered_elite`,
`D_weighted_consensus`.

**Example response** (shape):

```json
{
  "meta": {
    "t_split": "2024-11-18 13:01:25.083863",
    "as_of": "2025-09-26T00:00:00",
    "n_train_wallets": 420,
    "n_test_signals": 9591,
    "train_fraction": 0.6
  },
  "strategies": {
    "A_copy_top10": {
      "n_trades": 127,
      "n_skipped_concurrency": 0,
      "starting_balance": 1000.0,
      "ending_balance": 1575.65,
      "total_return": 0.5757,
      "cagr": 0.71,
      "win_rate": 0.61,
      "avg_trade_return": 0.12,
      "max_drawdown": 0.09,
      "sharpe_per_trade": 0.18,
      "equity_curve": [["2024-11-19 08:06:54.515761", 1000.0], ["...", 1575.65]],
      "description": "Copy every entry of the Top-10 elite",
      "n_selected_wallets": 10
    }
  }
}
```

`equity_curve` is a list of `[timestamp_string, equity_value]` pairs
(downsampled to ≤ ~300 points), suitable for a line chart.

---

### `GET /small-account`

The `small_account/summary` artifact — the $20-bankroll Kelly study: recommended
sizing rule, Kelly fractions, and the per-rule Monte-Carlo sizing tables for two
scenarios (`in_sample_edge` and the robustness `haircut_edge`).

**Params:** none.

**Example response** (shape):

```json
{
  "bankroll_usd": 20.0,
  "constraints": { "min_bet": 1.0, "max_bet": 5.0, "slippage_bps": 100.0, "horizon_trades": 200 },
  "recommended_rule": "half_kelly",
  "recommendation_basis": "max survival-weighted log-growth on the 50%-haircut edge",
  "scenarios": {
    "haircut_edge": {
      "n_trades_in_edge_sample": 2000,
      "mean_per_trade_return": 0.05,
      "std_per_trade_return": 0.9,
      "win_rate": 0.42,
      "full_kelly_fraction": 0.4711,
      "fractional_kelly_used": 0.2356,
      "recommended_bet_at_20": 2.36,
      "sizing_rules": [
        {
          "rule": "flat_1",
          "median_ending": 55.07,
          "mean_ending": 56.53,
          "p10_ending": 39.75,
          "p90_ending": 75.33,
          "prob_survival": 1.0,
          "prob_ruin": 0.0,
          "prob_double": 0.9166,
          "median_max_drawdown": 0.1089,
          "expected_growth_mult": 2.826
        }
      ]
    },
    "in_sample_edge": { "...": "same shape as haircut_edge" }
  }
}
```

Sizing rules: `flat_1`, `flat_2`, `flat_5`, `frac_kelly`, `half_kelly`,
`full_kelly`.

---

### `GET /signals`

The `signals/entry_signals` artifact — what profitable trades look like *at
entry*, sliced for the **elite** (top-ranked wallets) vs the **field**.

**Params:** none.

**Example response** (shape):

```json
{
  "as_of": "2025-09-26T00:00:00",
  "elite": {
    "by_entry_timing": [
      { "bucket": "3-7d", "n": 309, "win_rate": 0.3625, "avg_roi": -0.2603,
        "median_roi": -0.907, "avg_realized_edge": -0.0171 }
    ],
    "by_entry_odds": [
      { "bucket": "0.00-0.10", "n": 671, "win_rate": 0.1252, "avg_roi": 0.3376,
        "median_roi": -1.0, "avg_realized_edge": 0.0023 }
    ]
  },
  "field": { "by_entry_timing": [], "by_entry_odds": [] },
  "findings": {
    "elite_avg_entry_price": 0.4234,
    "field_avg_entry_price": 0.3526,
    "elite_avg_realized_edge": 0.1028,
    "field_avg_realized_edge": -0.1112,
    "elite_rho_lead_vs_roi": 0.1909,
    "elite_rho_entryprice_vs_roi": 0.4083,
    "interpretation": {
      "edge_lives_earlier": true,
      "elite_exploit_mispricing": true,
      "elite_prefer_longshots": false
    }
  }
}
```

---

### `GET /alpha`

The `category_alpha/by_category` artifact — realized performance per market
category for the elite and the field, plus the niche rankings.

**Params:** none.

**Example response** (shape):

```json
{
  "as_of": "2025-09-26T00:00:00",
  "elite_by_category": {
    "News": {
      "n_positions": 1626,
      "n_wallets": 95,
      "avg_roi": 0.1035,
      "median_roi": 0.0724,
      "win_rate": 0.5178,
      "total_profit_usd": 8631.2,
      "roi_std": 1.5715,
      "roi_per_unit_risk": 0.0659,
      "top_decile_profit_share": 0.5592
    }
  },
  "field_by_category": { "...": "same shape, all wallets" },
  "rankings": {
    "most_profitable_niche": "News",
    "most_repeatable_niche": "News",
    "lowest_variance_profitable_niche": "Economics",
    "best_risk_adjusted_niche": "News"
  }
}
```

---

## Endpoint summary

| Method | Path                 | Source                                   |
|--------|----------------------|------------------------------------------|
| GET    | `/health`            | config + `wallet_scores` count           |
| GET    | `/wallets/top`       | `wallet_scores` ⋈ `wallet_metrics`       |
| GET    | `/wallets/{address}` | `wallet_metrics` + `wallet_scores` + `wallet_clusters` |
| GET    | `/clusters`          | `cluster_profile/kmeans` + `wallet_clusters` |
| GET    | `/network`           | `network_summary` + `wallet_pairs` (cap 500) |
| GET    | `/leaders`           | `leader_follower`                        |
| GET    | `/backtests`         | `backtest_meta` + `backtest` (×4)        |
| GET    | `/small-account`     | `small_account/summary`                  |
| GET    | `/signals`           | `signals/entry_signals`                  |
| GET    | `/alpha`             | `category_alpha/by_category`             |
