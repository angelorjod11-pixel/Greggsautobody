# Methodology

This document is the statistical core of Polytrader: the exact metric
definitions, the composite scoring formula and its weights, the eligibility
filters, the bootstrap confidence intervals, clustering, correlation/network
analysis, the look-ahead-safe backtest, the Kelly/Monte-Carlo small-account
math, and how each major bias is mitigated. All numeric parameters quoted below
come from `config.yaml` unless stated otherwise.

> **Accounting primitive.** Polymarket outcome tokens settle at exactly $1 (win)
> or $0 (loss). A trade records the implied probability `price ∈ [0,1]` paid for
> `shares` of an outcome token, with `usd_size = price * shares`. This binary
> settlement is what makes cost-basis / ROI accounting clean throughout.

---

## 1. From trades to positions

A **position** is a wallet's net round-trip exposure to one outcome of one
market — the unit of P&L and ROI. `etl/positions.py` rebuilds the `positions`
table from raw `trades` (idempotently: it truncates and recomputes), fully
vectorized:

1. Group all fills by `(wallet_address, market_id, outcome_index)`.
2. Aggregate BUY fills → `bought_shares`, `cost_basis_usd` (Σ usd_size),
   `opened_at` (earliest BUY). Aggregate SELL fills → `sold_shares`,
   `proceeds_usd`, `last_sell` (latest SELL).
3. `net_shares = max(bought_shares − sold_shares, 0)`; residual net shares are
   settled at the market's resolved payout: `settlement_usd = net_shares × 1`
   if the held outcome won (and the market is resolved), else `0`.
4. Realized P&L and ROI (only for resolved markets):

   ```
   realized_pnl_usd = proceeds_usd + settlement_usd − cost_basis_usd
   roi              = realized_pnl_usd / cost_basis_usd
   avg_entry_price  = cost_basis_usd / bought_shares
   duration_hours   = (closed_at − opened_at) in hours
   ```

5. **Status:**
   - `open` — market still unresolved (no realized P&L).
   - `closed` — exited before resolution (`net_shares ≈ 0` and some shares sold);
     `closed_at = last_sell`.
   - `settled` — held to resolution; `closed_at = resolved_at`.

Only `settled` and `closed` positions carry realized performance and feed the
metrics. Positions in still-open markets are excluded — this is the first guard
against look-ahead / unrealized-gain bias.

---

## 2. Per-wallet metrics

`etl/metrics.py` aggregates each wallet's resolved positions into one
`WalletMetric` row. Helper definitions are kept explicit so the methodology is
auditable. `MONTH = 30.4` days. For a wallet with ROI vector `roi`, P&L vector
`pnl` (ordered by `closed_at`), and cost-basis vector `cost`:

### Activity / longevity
| Metric | Definition |
|---|---|
| `n_positions` | count of resolved positions |
| `n_markets` | distinct markets traded (resolved) |
| `active_days` | `max(1, (last closed_at − first opened_at) in days)` |
| `trades_per_active_month` | `n_positions / (active_days / 30.4)` |
| `capital_deployed_usd` | Σ `cost_basis_usd` |

### Profitability
| Metric | Definition |
|---|---|
| `total_profit_usd` | Σ `realized_pnl_usd` (net realized USD) |
| `avg_roi` | mean of per-position `roi` |
| `median_roi` | median of per-position `roi` (robust central tendency) |
| `profit_factor` | gross gains / gross losses = `Σ pnl[pnl>0] / −Σ pnl[pnl<0]`. If there are no losses, reports the gross-gain magnitude (or 0 if no gains). |
| `win_rate` | fraction of resolved positions with `is_win == True` |

### Risk / consistency
| Metric | Definition |
|---|---|
| `sharpe` | per-position Sharpe `= mean(roi) / std(roi, ddof=1)`. **No annualization** — positions are event-driven, not periodic — so this is a unitless reward-to-variability ratio. Returns 0 if `<2` positions or near-zero dispersion. |
| `max_drawdown` | peak-to-trough drawdown of the equity curve `capital + cumsum(pnl_ordered)`, as a fraction in [0,1]. The cost-basis capital base keeps it well-defined when early cumulative P&L is near zero. |
| `roi_stability` | `1 / (1 + cv(monthly_pnl))`, where `cv` is the coefficient of variation of monthly realized P&L. Steady earners → near 1; lumpy earners → near 0. |
| `positive_month_rate` | fraction of active months whose summed realized P&L was `> 0` |

### Behavioral features (also feed clustering)
| Metric | Definition |
|---|---|
| `avg_position_size_usd` | mean `cost_basis_usd` per position |
| `position_size_cv` | coefficient of variation of `cost_basis_usd` (sizing discipline; low = consistent stakes) |
| `median_hold_hours` | median `duration_hours` |
| `entry_lead_days` | mean of `(market end_date − position opened_at)` in days, clipped ≥ 0 (how early the wallet enters before resolution) |
| `markets_per_active_month` | `n_markets / active_months` (breadth) |
| `category_concentration` | **HHI** over category exposure by capital: `Σ pᵢ²` where `pᵢ` is the share of cost basis in category *i*. 1 = single-category specialist; low = diversified. |
| `avg_entry_price` | capital-weighted mean `avg_entry_price` across positions (≈ average implied probability paid; low ⇒ longshot/contrarian buyer, high ⇒ favorite buyer) |
| `top_category` | category with the largest capital share |
| `extra` (JSON) | `{ "category_exposure": {cat: share}, "monthly_pnl": {YYYY-MM: pnl} }` — reused by the correlation engine's `profit_corr` |

---

## 3. Eligibility filters (anti-survivorship / anti-small-sample)

A wallet is **ranked only if it clears ALL** of these (`eligibility:` block);
the gate is applied in `metrics.py` as the boolean `eligible` flag:

| Filter | Value | Bias it fights |
|---|---|---|
| `min_resolved_positions` | 20 | small-sample luck (a 9/10 coin-flipper) |
| `min_distinct_markets` | 10 | one-market specialists masquerading as skilled |
| `min_capital_deployed_usd` | 200 | trivially-sized accounts with noisy ROI |
| `min_active_days` | 14 | flash-in-the-pan accounts with no track record |
| `max_single_position_frac` | 0.95 | "one lucky all-in bet" — drops wallets whose largest position is ≥95% of total capital |

Rationale: the dataset only contains wallets that *traded*, so naive
leaderboards are dominated by survivors who got lucky on few bets. Requiring a
minimum number of independent resolved positions, across multiple markets, with
real capital and a real time span, and capping single-position concentration,
forces the ranking to reward repeatable performance rather than survivorship.

---

## 4. Composite score

`ranking/scorer.py` builds a composite score in **[0,100]** as a weighted blend
of five pillars. Top-level weights are validated to sum to 1.0 at config load.

### Top-level weights (`ranking.weights`)
| Pillar | Weight |
|---|---|
| Profitability | **0.40** |
| Win rate | **0.20** |
| Consistency | **0.20** |
| Risk | **0.10** |
| Longevity | **0.10** |

### Sub-weights (normalized within each pillar)
| Pillar | Component | Sub-weight | Source metric |
|---|---|---|---|
| Profitability (0.40) | total profit | 0.50 | `total_profit_usd` |
| | avg ROI | 0.30 | `avg_roi` |
| | profit factor | 0.20 | `profit_factor` |
| Win rate (0.20) | win rate | — | `win_rate` (single component) |
| Consistency (0.20) | median ROI | 0.40 | `median_roi` |
| | ROI stability | 0.35 | `roi_stability` |
| | positive-month rate | 0.25 | `positive_month_rate` |
| Risk (0.10) | max drawdown (inverted) | 0.50 | `max_drawdown` |
| | Sharpe | 0.50 | `sharpe` |
| Longevity (0.10) | active days | 0.60 | `active_days` |
| | trade count | 0.40 | `n_positions` |

### Formula

For each eligible wallet, every source metric is first **winsorized** then mapped
to its **population percentile** `∈ [0,1]` (lower-is-better metrics — max drawdown
— are inverted before percentiling). Then:

```
profitability = 0.50·pct(total_profit) + 0.30·pct(avg_roi) + 0.20·pct(profit_factor)
win           = pct(win_rate)
consistency   = 0.40·pct(median_roi) + 0.35·pct(roi_stability) + 0.25·pct(positive_month_rate)
risk          = 0.50·pct⁻¹(max_drawdown) + 0.50·pct(sharpe)        # pct⁻¹ = inverted percentile
longevity     = 0.60·pct(active_days) + 0.40·pct(trade_count)

composite = 100 · ( 0.40·profitability + 0.20·win + 0.20·consistency
                  + 0.10·risk + 0.10·longevity )
```

Each pillar sub-score is also stored ×100 in `wallet_scores`
(`profitability_score`, `win_rate_score`, …).

### Why percentile normalization + winsorization

- **Percentile normalization** makes the blend *scale-free*: profit (dollars),
  ROI (ratio), Sharpe (unitless), and counts live on wildly different scales and
  cannot be averaged directly. Mapping each to its rank within the eligible
  population puts everything on a common [0,1] axis and makes the score robust to
  the heavy tails typical of trading P&L — one whale's enormous profit becomes
  "the 100th percentile," not a number that dominates the weighted sum.
- **Winsorization** (`winsorize_pct = 0.01`, i.e. clip to the 1st/99th
  percentile) is applied to the long-tailed dollar/ratio metrics (`total_profit`,
  `avg_roi`, `profit_factor`, `median_roi`, `sharpe`) *before* building the
  percentilers, so extreme outliers don't distort the percentile mapping itself.
  Bounded-range metrics (win rate, stability, positive-month rate, drawdown,
  active days, counts) are percentiled without winsorization.

Implementation note: the percentiler is fit on the **point estimates of the
eligible population**; `pct(x) = searchsorted(sorted_values, x, 'right') / n`.

---

## 5. Bootstrap confidence intervals

To separate skill from small-sample luck, every score carries a **90% bootstrap
confidence interval** (`score_ci_low`, `score_ci_high` = the 5th/95th percentiles
of the bootstrap distribution). `bootstrap_iterations = 1000`.

Procedure (per wallet, seeded RNG):
1. Resample the wallet's **own resolved positions** with replacement, `B = 1000`
   times (each replicate has the same `n` as the wallet's real position count).
2. From each resample recompute the *sampling-dependent* profitability and
   win/consistency-ROI components: bootstrapped `total_profit`, `avg_roi`,
   `win_rate`, `median_roi`, and `profit_factor`, each pushed through the **same
   fixed population percentilers** built in step 4.
3. Components that are structural rather than per-trade-sampling — `roi_stability`,
   `positive_month_rate` (consistency), and the entire `risk` and `longevity`
   pillars — are held at their point-estimate percentiles within the bootstrap
   (they are not re-drawn). This isolates the uncertainty that actually comes
   from finite trade samples.
4. Recompute the composite for each replicate; take the 5th/95th percentiles.

Wallets with `<3` resolved positions get a degenerate CI equal to their point
score. **Interpretation:** a *tight* CI means the rank is not luck; a wide CI or
a collapsing lower bound flags a wallet whose score is sampling-fragile. The
report's "worth following" section explicitly prefers **high score + tight CI**.

---

## 6. Behavioral clustering

`clustering/cluster.py` segments **eligible** wallets on behavior (not identity)
to answer "what do similar traders look like?". Three complementary views:

- **Feature set** (`clustering.features`), z-scored with `StandardScaler`:
  `avg_position_size_usd`, `position_size_cv`, `median_hold_hours`,
  `entry_lead_days`, `markets_per_active_month`, `category_concentration`,
  `avg_entry_price`, `win_rate`, `avg_roi`.
- **K-means**, with **k chosen by silhouette** over the range `[2, 10]`: every k
  is fit (`n_init=10`, seeded), scored by `silhouette_score`, and the k with the
  highest silhouette wins. This is data-driven model selection rather than a
  hand-picked k.
- **Agglomerative (Ward)** hierarchical clustering at the chosen k — a second
  opinion robust to non-spherical groups; agreement with k-means is a stability
  check.
- **PCA(2)** — a 2-D embedding (`pca_x`, `pca_y`) for the dashboard scatter;
  explained-variance ratios are stored.

Each k-means cluster gets a profile (size, mean of every feature + win/ROI/
profit/score) and an **auto-generated human label** from heuristics on the mean
profile — e.g. *Sharp / Break-even / Losing* (by `avg_roi`),
*disciplined / erratic* (by `position_size_cv`), *long-hold / fast-flip*,
*early-entrant*, *specialist / diversified* (by `category_concentration`),
*longshot-buyer / favorite-buyer* (by `avg_entry_price`). Profiles are written to
the `cluster_profile` artifact; community labels are added later by the
correlation stage.

---

## 7. Correlation & network analysis

`correlation/network.py` builds relationships over the **top-N ranked wallets**
(`correlation.top_n = 100`). For each wallet, per `(wallet, market)` it takes the
**earliest** entry time and its outcome side. For every pair `(a, b)` that
co-traded at least `min_shared_markets = 5` markets it computes:

| Pairwise measure | Definition |
|---|---|
| `market_overlap` | Jaccard over traded-market sets: `|A∩B| / |A∪B|` |
| `timing_jaccard` | co-entries on the **same side within** `cofollow_window_hours = 48` h, divided by the size of the **union** of their markets — the "move together" signal that seeds graph edges |
| `direction_corr` | net same-side agreement over shared markets: `(same − opposite) / n_shared` ∈ [−1, 1] |
| `profit_corr` | Pearson correlation of the two wallets' monthly-P&L vectors over ≥3 common months (from `wallet_metrics.extra.monthly_pnl`) |
| `lead_lag_hours` | mean `(entry_b − entry_a)` over same-side shared markets; **positive ⇒ a enters first ⇒ a leads b** |

**Network graph + Louvain.** A NetworkX graph is built with the top-N wallets as
nodes and an edge for every pair whose `timing_jaccard ≥ edge_threshold = 0.15`
(weight = `timing_jaccard`). **Louvain community detection**
(`louvain_communities`, `resolution = 1.0`, seeded) partitions the graph;
communities with `>1` member are counted, and modularity is reported. Community
ids are written back to `wallet_clusters.community`.

**Leader/follower detection.** For pairs with strong co-following
(`timing_jaccard ≥ threshold` **and** `co_follow ≥ min_shared`), the consistent
lead-lag sign decides direction: if mean `lead_lag_hours > 6` then *a* leads
*b* (and `a`'s lead score accrues `co_follow`); if `< −6` then *b* leads *a*.
Wallets are ranked by accumulated lead score into a leader→follower table. A
network summary artifact aggregates averages (overlap, timing, direction, profit
corr) and boolean interpretations (e.g. "top traders enter same markets",
"leaders present", "coordinated communities").

---

## 8. Backtesting — look-ahead-safe by construction

`backtest/engine.py` simulates copy-trading. The **entire point** is to avoid
look-ahead and selection bias; three controls enforce it:

1. **Chronological train/test split.** The timeline is cut at the
   `train_fraction = 0.6` quantile of position *entry* times (`t_split`). Elite
   wallets are *selected* using **only** positions that resolved in the TRAIN
   window (`closed_at ≤ t_split`); strategies are then simulated **only** on
   entries that occur strictly **after** `t_split` (the TEST window). You can
   never pick a wallet *because* it won the very trades you then score it on.
2. **Decisions use only past information.** Whether to copy a signal depends on
   the selecting wallet's *train-window* statistics and on how many elite have
   entered a market *so far*. A market's settlement is used **only** to realize
   P&L after a copied position is held to resolution — never to decide entry.
3. **Costs are modeled.** Copiers fill at the signal wallet's entry price
   **plus slippage** (`slippage_bps = 75`, i.e. 0.75% adverse) and configurable
   fees (`fee_bps = 0`; Polymarket's trading fee is currently zero but kept
   configurable). Per-dollar held-to-resolution return is `(payout − ep) / ep`.

**Train-window selection.** `_train_metrics` computes, per wallet over resolved
train positions: `n_resolved`, `win_rate`, `avg_roi`, `total_profit`, `sharpe`,
`last_active`. A composite **train_score** is the mean of the percentile ranks of
`total_profit`, `avg_roi`, `win_rate`, `sharpe`.

**Strategies** (`backtest.strategies`):
| ID | Type | Rule |
|---|---|---|
| **A** | `copy_top` (`top_n=10`) | copy every test-window entry of the Top-10 elite (by train_score) |
| **B** | `consensus` (`top_n=25`, `min_elite_in_market=3`) | among the Top-25 elite, enter a `(market, outcome)` once the **3rd** distinct elite joins the same side, filling at that k-th elite's price |
| **C** | `filtered` (`min_win_rate=0.65`, `min_avg_roi=0.20`, `min_resolved=100`) | copy wallets passing hard train filters (high win rate **and** ROI **and** ≥100 resolved positions) |
| **D** | `weighted_consensus` (`top_n=25`, `recency_halflife_days=90`) | accumulate elite *weight* per `(market, outcome)`; weight = `train_score × 0.5^(age/halflife)` (recency-decayed); enter when accumulated weight crosses a trigger (≈ 2 average-weight elite), sizing ∝ accumulated weight (capped ×3) |

**Event-driven simulator** (`_simulate`). Fixed-fractional staking from
`starting_capital_usd = 1000`: each entry stakes `per_trade_fraction = 0.04`
(×`size_mult`) of current cash, subject to `max_concurrent_positions = 25` and
available cash. A time-ordered event queue settles positions whose `resolved_at`
has passed (freeing capital) before processing each new entry. Outputs:
ending balance, total return, CAGR, per-trade win rate, max drawdown,
per-trade Sharpe, and a downsampled equity curve. One order per `(market,
outcome)` (earliest qualifying entry) prevents double-counting.

---

## 9. Small-account ($20) optimization — Kelly + Monte-Carlo

`smallaccount/kelly.py` answers: given a `$20` bankroll, `$1–$5` per-bet limits,
and heavier small-order slippage (`slippage_bps = 100`, 1%), how should bets be
sized when copying the elite?

1. **Empirical edge distribution.** Replay the **Top-10** elite's resolved
   positions as held-to-resolution `$-for-$` copies, charged small-account
   slippage: per-dollar return `r = (payout − ep) / ep` with
   `ep = clip(avg_entry_price·(1+slip) + fee, 0.01, 0.99)`. This is the empirical
   reward distribution. Because it is **in-sample (optimistic)**, a parallel
   **haircut scenario** shrinks each trade's edge 50% toward zero (`r_hair =
   0.5·r`) for robustness.
2. **Kelly fraction.** Numerically maximize the expected log-growth
   `E[log(1 + f·r)]` over `f ∈ [0, 0.999]` (`scipy.optimize.minimize_scalar`,
   bounded) to get the **full-Kelly** fraction `f*`. Returns 0 if mean edge ≤ 0.
   The *deployed* rule uses **fractional (quarter-) Kelly**, `kelly_fraction =
   0.25`, clamped to the `$1–$5` band — full Kelly is famously over-aggressive
   out of sample.
3. **Monte-Carlo.** Each sizing rule is simulated over `monte_carlo_paths =
   20000` bootstrapped paths of `horizon_trades = 200` draws from `r`. Rules
   compared: `flat_1`, `flat_2`, `flat_5`, `frac_kelly` (= `f*·0.25·bankroll`),
   `half_kelly` (= `f*·0.5·bankroll`), `full_kelly` (= `f*·bankroll`); every stake
   is clamped to `[$1,$5]` and capped at the current bankroll. Reported per rule:
   median/mean/p10/p90 ending balance, **P(survival)** (never falling below
   `ruin_threshold_usd = 5`), **P(ruin)**, **P(double to $40)** (`double_target_
   usd = 40`), median max drawdown, expected growth multiple.

**Recommendation rule.** The recommended sizing is chosen on the **haircut**
scenario by maximizing *survival-weighted log-growth*,
`P(survival) · log(expected_growth_mult)` — explicitly trading raw growth for
ruin avoidance, appropriate for a bankroll only ~20 units of `$1` risk deep.

---

## 10. Early-signal detection & category alpha

**Signals** (`signals/detect.py`). "What do profitable trades have in common at
*entry*, before the outcome is known?" Resolved positions are sliced by features
observable at entry — `entry_lead_days` (buckets `[0,1,3,7,14,30,90,9999]`) and
`avg_entry_price` (odds buckets `[0,.1,.25,.4,.6,.75,.9,1]`) — and per bucket
(min `30` positions) it reports win rate, avg/median ROI, and **realized edge**
`= payout − entry_price` (positive ⇒ bought below fair value). Everything is
computed separately for **elite** (top-100) vs **field**. Headline findings use
Spearman correlations (robust to nonlinearity): `ρ(days-before-resolution, ROI)`
and `ρ(entry_price, ROI)` for the elite, plus elite-vs-field average entry odds
and realized edge. **No look-ahead:** every slicing key is known at entry; only
the realized return is measured after the fact.

**Category alpha** (`alpha/categories.py`). "Where does the edge live?" Per
category, for field and elite, it computes avg/median ROI, win rate, total
profit, ROI dispersion (`roi_std`), a risk-adjusted ratio
`roi_per_unit_risk = avg_roi / roi_std`, and **profit concentration** — the share
of (positive) category profit captured by the top-decile wallets (high ⇒ alpha
concentrated in few hands). Niches are ranked by the **elite** view (min 30
positions): most profitable (avg ROI), most repeatable (win rate),
lowest-variance-profitable, and best risk-adjusted.

---

## 11. How each bias is specifically mitigated

| Bias | Mitigation |
|---|---|
| **Look-ahead bias** | (a) Only *resolved* positions carry realized P&L; open positions are excluded from every metric. (b) The backtest's settlement is used *only* to realize P&L after a held-to-resolution position closes, *never* to decide entry. (c) Signal/alpha slicing keys (entry timing, odds, liquidity, category) are all observable at entry; only outcomes are measured ex-post. |
| **Selection / data-snooping bias** | The backtest *selects* elite wallets on the TRAIN window and *evaluates* on a strictly-later TEST window (chronological split at the 0.6 entry-time quantile). A wallet can never be chosen because of the very trades used to score it. |
| **Survivorship bias** | Eligibility filters require a real, repeatable track record (≥20 resolved positions, ≥10 markets, ≥$200 deployed, ≥14 active days) before a wallet is ranked, so the leaderboard isn't dominated by survivors who got lucky on a handful of bets. |
| **Small-sample luck** | (a) Same eligibility floors. (b) The 0.95 single-position cap removes "one lucky all-in" wallets. (c) Per-wallet bootstrap CIs surface scores that are sampling-fragile, so consumers can prefer tight-CI ranks. |
| **Overfitting** | (a) Out-of-sample backtest evaluation (train/test). (b) Percentile + winsorized scoring resists fitting to heavy-tailed outliers. (c) k chosen by silhouette + a Ward second opinion rather than a hand-tuned k. (d) The small-account plan uses a 50% edge haircut and fractional (quarter) Kelly, and the recommendation maximizes survival-weighted growth rather than in-sample return. |
| **Scale dominance / fat tails** | Every scoring metric is winsorized (long-tailed ones) and percentile-normalized, so no single dollar figure can dominate the composite. |

---

## 12. Limitations

- **Synthetic vs live.** The default, validated run uses the bundled *synthetic*
  dataset because live Polymarket API egress was blocked in the build
  environment. The synthetic data is designed to *recover* known ground truth
  (the report checks `Spearman(composite score, latent skill)`), and the code
  paths are identical to a live run — but the **specific numbers are illustrative
  only**. Real-market magnitudes (and any real edge) require `data_source: live`.
- **Copy-trade latency & fills.** The backtest models slippage and entry at the
  signal wallet's (or k-th elite's) fill price, but real copying incurs
  additional latency, partial fills, and price impact that a historical
  simulation cannot perfectly capture. The small-account edge is estimated
  *in-sample* and is therefore optimistic even after the 50% haircut.
- **Sybil / identity uncertainty.** On-chain wallets can be Sybil/linked or
  controlled by one entity. The network/community/leader-follower analysis flags
  *coordinated* clusters from co-trading behavior but **cannot prove identity**;
  apparent leaders may be the same actor across addresses, or unrelated traders
  reacting to the same public information.
- **Confidence intervals capture sampling, not regime change.** Bootstrap CIs
  quantify uncertainty due to a *finite sample of a wallet's own trades* only.
  They say nothing about non-stationarity — a wallet's edge can decay, markets
  can change regime, and the historical distribution may not hold forward.
- **Past ≠ future.** Rankings, strategy results, and Kelly sizes are descriptive
  of the analyzed window. Past performance need not persist; treat all outputs as
  research signal, not investment advice.
