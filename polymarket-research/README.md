# Polymarket Trader Research System

A reproducible, end-to-end research system that ingests Polymarket trading
history, ranks the most successful wallets, dissects **what they do that works**,
and answers a concrete question: *could a $20 bankroll be grown by following
them?*

It is built to be evidence-based: every conclusion comes with confidence
intervals, the backtests are look-ahead-safe by construction, and the entire
pipeline is validated against a synthetic dataset with known ground truth.

```
ingest ──► ETL ──► ranking ──► clustering ──► correlation ──► backtest
   │                 │             │              │              │
(synthetic       positions     composite      behavioral     copy-trade
 OR live          & metrics     score+CIs      clusters,      A/B/C/D,
 collectors)                                   networks       $20 Kelly plan
                                                  │              │
                                            signals · alpha · report · API · dashboard
```

> **Note on data source.** This repository was built in an environment whose
> egress policy blocks `*.polymarket.com`, so it ships with a realistic
> **synthetic** data generator and runs fully offline by default. The
> **live collectors** (Gamma, Data-API, CLOB, on-chain subgraph) are fully
> implemented and unit-tested; switch `POLYTRADER_DATA_SOURCE=live` in an
> environment that permits Polymarket egress to run on real data. Every other
> line of code is identical between the two modes.

---

## Quickstart

```bash
cd polymarket-research
python3 -m pip install -r requirements.txt      # or: make install

# Run the entire pipeline on synthetic data (~15s) and generate the report:
make run            # == python -m polytrader.cli all
make top N=25       # print the top-25 ranked wallets
make test           # run the test suite (offline, deterministic)

# Explore the results:
make api            # FastAPI JSON service on http://localhost:8000
make dashboard      # Streamlit dashboard
cat reports/generated/FINAL_REPORT.md
```

Run against **real Polymarket data** (where egress is allowed):

```bash
POLYTRADER_DATA_SOURCE=live make run        # or: make run-live
```

Use **PostgreSQL** instead of the default SQLite file:

```bash
export POLYTRADER_DATABASE_URL=postgresql+psycopg2://user:pass@localhost:5432/polytrader
make run
```

---

## What it produces

| Question (from the brief) | Where it's answered |
|---|---|
| Who are the 100 best Polymarket traders? | `wallet_scores` table · `make top` · report §1 |
| What traits do they share? | clustering + signals · report §2 |
| Which wallets are statistically worth following? | score + tight bootstrap CI + leader status · report §3 |
| Which copy-trading strategy performed best? | backtest A/B/C/D · report §4 |
| How should a $20 account allocate capital? | Kelly / Monte-Carlo plan · report §5 |
| What signals precede profitable trades? | entry-timing/odds/edge + category alpha · report §6 |

---

## The ranking model

A composite score in `[0,100]`, configured in [`config.yaml`](config.yaml):

| Component | Weight | Built from |
|-----------|:------:|------------|
| Profitability | **40%** | total profit, average ROI, profit factor |
| Win rate | **20%** | resolved-position win rate |
| Consistency | **20%** | median ROI, ROI stability, positive-month rate |
| Risk | **10%** | max drawdown (inverted), Sharpe |
| Longevity | **10%** | active days, trade count |

Each raw metric is winsorized and mapped to a **population percentile** before
blending (scale-free, robust to the heavy tails of trading P&L). Every score
carries a **90% bootstrap confidence interval** from resampling that wallet's own
positions — a tight CI means the rank is skill, not a lucky streak. Wallets must
clear minimum-activity **eligibility filters** to be ranked, which is the first
line of defense against survivorship/small-sample bias.

See [`docs/METHODOLOGY.md`](docs/METHODOLOGY.md) for exact formulas and bias
controls.

---

## Bias controls (why you can trust the numbers)

- **Look-ahead / selection bias** — backtests split the timeline chronologically:
  elite wallets are *selected on a training window* and *evaluated on a later
  test window*. Settlement is used only to realize P&L, never to decide entry.
- **Survivorship / small-sample** — eligibility thresholds + bootstrap CIs;
  the report flags how much of the Top-100 is plausibly luck.
- **Overfitting** — fixed, documented weights (not fit to outcomes); robustness
  via an in-sample-edge **haircut** scenario in the $20 plan.
- **Validation** — on synthetic data the ranking's Spearman correlation with the
  planted latent skill is reported, and the network analysis is checked against
  the planted leader→follower structure (see the test suite).

---

## Repository layout

```
polymarket-research/
├── config.yaml              # all analytical parameters (weights, filters, knobs)
├── polytrader/
│   ├── config.py            # config loader (yaml + env overrides)
│   ├── db/                  # SQLAlchemy schema + session (SQLite/PostgreSQL)
│   ├── collect/             # LIVE collectors: gamma, data_api, clob, onchain
│   ├── synth/               # synthetic data generator (offline validation)
│   ├── etl/                 # positions + per-wallet metrics
│   ├── ranking/             # composite score + bootstrap CIs
│   ├── clustering/          # KMeans + hierarchical + PCA
│   ├── correlation/         # pairwise metrics, NetworkX graph, Louvain, leaders
│   ├── backtest/            # look-ahead-safe copy-trade A/B/C/D
│   ├── smallaccount/        # Kelly + Monte-Carlo $20 plan
│   ├── signals/             # early-signal detection
│   ├── alpha/               # category alpha discovery
│   ├── report/              # Markdown final-report generator
│   ├── pipeline.py          # end-to-end orchestration
│   └── cli.py               # `python -m polytrader.cli ...`
├── api/main.py              # FastAPI JSON service
├── dashboard/app.py         # Streamlit dashboard
├── workflows/daily_update.py# daily refresh (cron-friendly)
├── tests/                   # pytest: helpers, collectors, full-pipeline validation
├── docs/                    # ARCHITECTURE, METHODOLOGY, DATA_SCHEMA, API
└── reports/generated/       # FINAL_REPORT.md (built by the pipeline)
```

---

## CLI reference

```bash
python -m polytrader.cli all          # full pipeline
python -m polytrader.cli ingest       # just (re)load data
python -m polytrader.cli positions    # rebuild positions from trades
python -m polytrader.cli metrics      # recompute per-wallet metrics
python -m polytrader.cli rank         # recompute scores + CIs
python -m polytrader.cli cluster      # behavioral clustering
python -m polytrader.cli correlate    # network + communities + leaders
python -m polytrader.cli backtest     # copy-trade strategies A/B/C/D
python -m polytrader.cli smallaccount # $20 Kelly / Monte-Carlo plan
python -m polytrader.cli signals      # early-signal detection
python -m polytrader.cli alpha        # category alpha
python -m polytrader.cli report       # regenerate the Markdown report
python -m polytrader.cli top --n 100  # print the top-N wallets
```

## Configuration

Runtime/connection settings come from environment variables (see
[`.env.example`](.env.example)); analytical parameters live in
[`config.yaml`](config.yaml) so methodology changes are auditable in git.

| Env var | Default | Meaning |
|---------|---------|---------|
| `POLYTRADER_DATABASE_URL` | `sqlite:///data/polytrader.db` | DB connection |
| `POLYTRADER_DATA_SOURCE` | `synthetic` | `synthetic` or `live` |
| `POLYTRADER_SEED` | `42` | global RNG seed (reproducibility) |
| `POLYTRADER_CONFIG` | `config.yaml` | path to the analytical config |

---

## Disclaimer

This is a research tool, not financial advice. Prediction-market trading is
risky; copy-trading adds latency and slippage; past performance does not
guarantee future results. The $20 analysis is an illustrative exercise in
bankroll mathematics, not a recommendation to deploy capital.
