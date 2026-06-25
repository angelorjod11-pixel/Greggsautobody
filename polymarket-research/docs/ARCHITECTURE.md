# Architecture

## System overview

**Polytrader** is an offline-first research system that analyzes Polymarket
prediction-market traders to find, score, and characterize the top-performing
wallets, and to answer downstream questions (what the best traders have in
common, who is statistically worth following, which copy-trading strategy would
have worked, how a tiny $20 account should size bets, and what entry signals
precede profitable trades). Raw events (wallets, markets, trades) flow into a
SQL database; a deterministic ETL reconstructs round-trip **positions** and a
per-wallet **metric vector**; a ranking engine produces a composite score with
bootstrap confidence intervals; and a fan-out of analytics modules (clustering,
correlation/network, backtest, small-account/Kelly, early-signal, category
alpha) write their outputs back to the database, from which a single Markdown
report is rendered.

**Core design decision — live-ready but synthetic-validated, engine-agnostic
storage.** The pipeline is written to run against the live Polymarket APIs
(`data_source: live`), but because outbound egress to those hosts was blocked in
the build environment, the default mode (`data_source: synthetic`) generates a
*realistic, ground-truth-bearing* dataset so the entire pipeline can be executed
and validated offline. The synthetic generator plants known structure (latent
trader skill, exploitable market mispricing, leader/follower copying) that the
analytics are designed to recover, which is how the methodology is validated end
to end. Storage is **SQLite by default** (zero-config local file) and
**PostgreSQL-ready** by setting `POLYTRADER_DATABASE_URL`; every column type used
in the schema exists on both engines, and all derived tables are rebuilt
idempotently from the raw events, so a run is reproducible from raw data alone.

## Data-flow diagram

```mermaid
flowchart TD
    subgraph INGEST["Ingest (data_source switch)"]
        SYN["synth/generator.py<br/>(synthetic, offline)"]
        LIVE["collect/ (live Polymarket APIs)<br/>gamma / data-api / clob / subgraph"]
    end
    SYN --> RAW
    LIVE --> RAW

    subgraph RAW["Raw event tables"]
        W["wallets"]
        M["markets"]
        T["trades"]
    end

    RAW --> POS["etl/positions.py<br/>build_positions()<br/>round-trip positions"]
    POS --> PT[("positions")]
    PT --> MET["etl/metrics.py<br/>compute_metrics()<br/>per-wallet metric vector + eligibility"]
    MET --> WM[("wallet_metrics")]

    WM --> RANK["ranking/scorer.py<br/>rank_wallets()<br/>composite score + bootstrap CI"]
    RANK --> WS[("wallet_scores")]

    WS --> CLU["clustering/cluster.py<br/>KMeans + Ward + PCA"]
    WS --> COR["correlation/network.py<br/>pairwise metrics + Louvain"]
    WS --> BT["backtest/engine.py<br/>strategies A/B/C/D (train/test)"]
    WS --> SA["smallaccount/kelly.py<br/>edge + Kelly + Monte-Carlo"]
    WS --> SIG["signals/detect.py<br/>entry-time slicing"]
    WS --> ALP["alpha/categories.py<br/>category alpha"]

    CLU --> ART[("analysis_artifacts + wallet_clusters / wallet_pairs")]
    COR --> ART
    BT --> ART
    SA --> ART
    SIG --> ART
    ALP --> ART

    WS --> REP["report/build_report.py<br/>FINAL_REPORT.md"]
    ART --> REP
    WM --> REP

    REP --> OUT["reports/generated/FINAL_REPORT.md"]
    ART -. "read-only JSON" .-> API["api/main.py (FastAPI, GET-only)"]
    WS -. .-> API
    API -. "consumes" .-> DASH["dashboard/ (Streamlit, stub)"]
```

Plain-text flow:

```
ingest (synthetic OR live collectors)
  -> wallets / markets / trades            (raw events)
  -> ETL: positions  -> metrics            (derived: positions, wallet_metrics)
  -> ranking                               (wallet_scores: composite + CI)
  -> { clustering, correlation, backtest,
       small-account, signals, alpha }     (wallet_clusters, wallet_pairs,
                                            analysis_artifacts)
  -> report (Markdown)  [+ read-only FastAPI; Streamlit dashboard stub]
```

## Module map

| Module / package | Responsibility |
|---|---|
| `polytrader/config.py` | Loads and validates configuration. Two layers: analytical parameters from `config.yaml` (versioned, auditable) and runtime/connection settings from `POLYTRADER_*` env vars. Validates that ranking weights sum to 1.0. Exposes a seeded numpy RNG. |
| `config.yaml` | Single source of truth for all analytical parameters: eligibility filters, ranking weights/sub-weights, clustering/correlation/backtest/small-account/signals knobs, and category keyword routing. |
| `polytrader/db/models.py` | SQLAlchemy ORM schema (the 9 tables). Engine-agnostic types; raw-event tables vs derived tables; `JSON` columns for nested payloads. |
| `polytrader/db/session.py` | Engine & session management. Creates the SQLite (default) or PostgreSQL engine, sets SQLite PRAGMAs (`foreign_keys=ON`, `journal_mode=WAL`), provides `init_db()` and a transactional `session_scope()`. |
| `polytrader/synth/generator.py` | Synthetic Polymarket data generator (offline mode). Encodes latent skill, per-market mispricing, and leader→follower copying as ground truth, and persists latent skill as a `ground_truth` artifact for validation. |
| `polytrader/collect/` | Live-collection package, used when `data_source == "live"`. `pipeline.collect_all` orchestrates Gamma (`gamma.py`, market metadata) → Data API (`data_api.py`, per-market trades → wallet universe), writing the same raw-event schema the synthetic generator targets. `onchain.py` is an alternative subgraph (Goldsky/GraphQL `OrderFilled`) cross-check; `clob.py` pulls price history (optional signal enrichment); `rate_limit.py` provides a token-bucket + tenacity-retry HTTP client. Hosts and rate limits come from the `collect:` block in `config.yaml`. |
| `polytrader/etl/positions.py` | Reconstructs round-trip positions from raw trades: nets BUY/SELL fills per `(wallet, market, outcome)`, settles residual shares at the resolved payout, computes realized P&L / ROI / duration / status. Idempotent (truncate-and-rebuild). |
| `polytrader/etl/metrics.py` | Aggregates *resolved* positions into the per-wallet metric vector (profitability, win rate, risk, consistency, behavioral features) and applies the eligibility gate. |
| `polytrader/ranking/scorer.py` | Composite score in [0,100] from the metric vector using `config.yaml` weights, percentile normalization, winsorization, and per-wallet bootstrap confidence intervals. |
| `polytrader/clustering/cluster.py` | Behavioral segmentation: KMeans with k chosen by silhouette, Agglomerative (Ward) as a second opinion, PCA(2) embedding, and auto-labeled cluster profiles. |
| `polytrader/correlation/network.py` | Pairwise relationship metrics over the top-N wallets, a NetworkX graph, Louvain communities, and leader→follower detection from lead-lag signs. |
| `polytrader/backtest/engine.py` | Look-ahead-safe copy-trading backtester: chronological train/test split, four strategies (A/B/C/D), event-driven fixed-fractional simulator with a cost model. |
| `polytrader/smallaccount/kelly.py` | $20-bankroll optimization: empirical edge from replaying elite copies, full/fractional Kelly solver, Monte-Carlo survival/ruin/doubling, and a 50%-haircut robustness scenario. |
| `polytrader/signals/detect.py` | Early-signal detection: slices resolved positions by entry-time features (days-before-resolution, entry odds) and compares elite vs field realized edge. |
| `polytrader/alpha/categories.py` | Category alpha: per-category realized performance (ROI, win rate, dispersion, risk-adjusted ratio, profit concentration) for field and elite, and niche rankings. |
| `polytrader/pipeline.py` | End-to-end orchestration. `run_all()` runs ingest → positions → metrics → rank → cluster → correlate → backtest → small-account → signals → alpha → report in dependency order. |
| `polytrader/cli.py` | Command-line entrypoint (`python -m polytrader.cli ...`): `all`, `initdb`, every single stage, and `top --n`. |
| `polytrader/report/build_report.py` | Renders the final Markdown report from `wallet_scores` + all analysis artifacts, with CIs and explicit bias caveats. |
| `api/main.py` | Read-only FastAPI service (`app`) over the populated database + artifacts. Serializes rankings, clusters, network, backtests, small-account sizing, signals and category alpha as JSON; every endpoint is a `GET` (nothing writes). |
| `dashboard/` | Streamlit dashboard package — stub in this build (only `__init__.py`; the `dashboard` optional extra declares Streamlit/Plotly). Intended to consume the read-only API. |
| `tests/` | Pytest suite: collector normalizers (`test_collectors.py`), metric/percentile/Kelly math (`test_helpers.py`), and a full populated-DB pipeline run (`test_pipeline.py`) asserting eligibility, skill recovery, configured-weight scoring, clustering, network/leaders, **backtest look-ahead safety**, small-account and signals/alpha. |
| `workflows/` | Placeholder directory (empty in this build). |

## How to run

### Install

```bash
cd polymarket-research
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# or: pip install -e .            (console script: polytrader)
# optional extras: pip install -e ".[dashboard]"   pip install -e ".[postgres]"
```

Configuration defaults to the bundled `config.yaml` and a local SQLite file at
`data/polytrader.db`. Copy `.env.example` to `.env` to override runtime settings.

### Full pipeline

```bash
python -m polytrader.cli all          # synthetic by default; rebuilds from scratch
python -m polytrader.cli all --keep   # do not drop existing data first
# console-script equivalent (after pip install -e .):
polytrader all
```

`all` runs `run_all()`: it initializes the DB, ingests data, and runs every
stage in order, printing per-stage timing and a brief result summary.

### Single stages

Each stage reads from / writes to the DB, so stages can be run independently
(useful after changing weights in `config.yaml`):

```bash
python -m polytrader.cli initdb       # create tables
python -m polytrader.cli ingest       # synthetic generate (or live collect)
python -m polytrader.cli positions    # rebuild round-trip positions
python -m polytrader.cli metrics      # recompute per-wallet metrics + eligibility
python -m polytrader.cli rank         # recompute composite scores + CIs
python -m polytrader.cli cluster
python -m polytrader.cli correlate
python -m polytrader.cli backtest
python -m polytrader.cli smallaccount
python -m polytrader.cli signals
python -m polytrader.cli alpha
python -m polytrader.cli report       # regenerate FINAL_REPORT.md only
```

Inspect the leaderboard directly:

```bash
python -m polytrader.cli top --n 25
```

Point any command at an alternate config with `--config path/to/config.yaml`.

### Live mode

```bash
POLYTRADER_DATA_SOURCE=live python -m polytrader.cli all
```

This swaps the ingest step from the synthetic generator to the live collectors
under `polytrader/collect/` (`collect_all`): discover resolved, liquid markets
via Gamma, then pull each market's trades via the Data API; the union of
`proxyWallet` addresses is the trader universe. All collector hosts and rate
limits come from the `collect:` block in `config.yaml`. Live ingest requires
outbound network access to `*.polymarket.com` (and the Goldsky subgraph); in
sandboxes that block those hosts this step raises a network error by design — use
the default synthetic source there.

### Database selection

```bash
# default (SQLite, zero-config)
# nothing to set; data/polytrader.db is created automatically

# PostgreSQL
export POLYTRADER_DATABASE_URL="postgresql+psycopg2://user:pass@localhost:5432/polytrader"
python -m polytrader.cli all
```

### API

A read-only FastAPI service exposes the populated database and analysis
artifacts as JSON (the `dashboard` optional extra provides FastAPI/uvicorn):

```bash
export POLYTRADER_DATABASE_URL="sqlite:////abs/path/to/data/polytrader.db"
export PYTHONPATH=/abs/path/to/polymarket-research
uvicorn api.main:app --reload --port 8000
# interactive docs at http://localhost:8000/docs
```

Endpoints (all `GET`, read-only): `/health`, `/wallets/top?n=`,
`/wallets/{address}`, `/clusters`, `/network`, `/leaders`, `/backtests`,
`/small-account`, `/signals`, `/alpha`. Each maps to the corresponding table /
artifact (see `DATA_SCHEMA.md`); floats are made strictly JSON-safe
(NaN/inf → null) and timestamps ISO-8601. The API never writes — all data is
produced offline by `python -m polytrader.cli all`.

### Dashboard

The `dashboard/` package is a stub in this build (only `__init__.py`); the
`dashboard` extra declares Streamlit/Plotly. It is intended to consume the
read-only API above. The shipped, fully-rendered deliverable is the Markdown
report at `reports/generated/FINAL_REPORT.md`, produced by the `report` stage
from the same artifacts the API serves.

### Daily / repeated workflow

The default `all` run drops and rebuilds everything for a clean, reproducible
snapshot. To refresh against new data while keeping prior raw events, run
`ingest --keep`-style updates and then re-run the derived stages
(`positions → metrics → rank → ... → report`). Because every derived table is
idempotent (truncate-and-rebuild from raw events), re-running the downstream
stages on an updated raw layer always yields a consistent analysis as-of the
latest trade timestamp (or the `as_of` pin in `config.yaml`).

## Extensibility

- **Swap synthetic → live.** Set `data_source: live` (or
  `POLYTRADER_DATA_SOURCE=live`). The ingest step is the only thing that changes;
  every downstream stage operates on the same raw-event schema.
  `polytrader/collect/pipeline.collect_all(drop=...)` already populates the
  `wallets`/`markets`/`trades` tables from the hosts configured under `collect:`.
  To add or swap a source (e.g. the on-chain subgraph in `onchain.py`), write a
  pure normalizer to the `trades`/`markets` shape and wire it into `collect_all`;
  the normalizers are unit-tested offline.
- **Change the scoring methodology.** Edit the `ranking:` block in `config.yaml`
  (top-level weights and per-block sub-weights). Top-level weights are validated
  to sum to 1.0 at load; sub-weights are normalized internally. No code change is
  needed — re-run `metrics`/`rank`/`report`. Because the weights live in version
  control, methodology changes are auditable in git.
- **Add a backtest strategy.** Add an entry under `backtest.strategies` in
  `config.yaml` and a corresponding order-builder + run entry in
  `polytrader/backtest/engine.py` (`run_backtests`), reusing the shared
  `_simulate()` event-driven engine and cost model.
- **Tune any stage.** Eligibility filters, clustering feature set, correlation
  thresholds, small-account constraints, and signal/odds buckets are all
  config-driven; modules read them through `get_config()`.
- **Add a table or artifact.** New wide outputs become ORM models in
  `db/models.py`; ad-hoc run-level outputs can be stored as
  `analysis_artifacts` rows (generic `kind`/`name`/`payload` JSON) without a
  schema migration.
