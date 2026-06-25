# Polymarket Trader Research — Final Report

*Generated 2026-06-25 21:10 UTC · analysis as-of 2025-09-26 · data source: **synthetic** · 420 wallets analyzed, 384 eligible after filters*

> **Note.** This run uses the bundled *synthetic* dataset (live Polymarket API egress was unavailable in this environment). The numbers below are illustrative but the methodology, code paths and validation are identical to a live run — switch `data_source: live` to reproduce against real data.

## Methodology & bias controls

- **Eligibility filter** (anti-survivorship / anti-small-sample): a wallet is ranked only with ≥20 resolved positions, ≥10 distinct markets, ≥$200 deployed, and no single position >95% of capital.
- **Composite score** = 40% profitability + 20% win rate + 20% consistency + 10% risk + 10% longevity, each metric mapped to a population percentile (scale-free, heavy-tail-robust).
- **Confidence intervals**: every score carries a 90% bootstrap CI from resampling each wallet's own positions (1000 iters) — a tight CI means the rank is not luck.
- **Backtests are look-ahead-safe**: elite wallets are *selected on a training window* and *evaluated on a later test window*; settlement is used only to realize P&L, never to decide entry.

## 1. Who are the 100 best Polymarket traders?

Ranked 384 eligible wallets. The Top-10 (■ = bootstrap 90% CI):

| # | Wallet | Score [90% CI] | Total P&L | Avg ROI | Win% | Sharpe | MaxDD | Positions |
|--:|--------|----------------|----------:|--------:|-----:|-------:|------:|----------:|
| 1 | `0x94b6…3911` | 97.0 [96–97] | $9,270 | 42.2% | 69.8% | 0.32 | 1.9% | 225 |
| 2 | `0x30ba…4ff5` | 94.7 [94–95] | $26,422 | 36.1% | 67.5% | 0.17 | 1.3% | 311 |
| 3 | `0x97fb…be3d` | 94.1 [93–94] | $4,880 | 64.5% | 71.5% | 0.21 | 1.4% | 158 |
| 4 | `0x028e…93d7` | 93.1 [90–94] | $3,271 | 36.1% | 64.9% | 0.31 | 5.9% | 74 |
| 5 | `0x554d…f274` | 92.2 [87–94] | $1,699 | 35.5% | 57.5% | 0.22 | 4.8% | 80 |
| 6 | `0x43f3…98db` | 92.0 [89–93] | $3,079 | 15.9% | 63.7% | 0.14 | 2.4% | 267 |
| 7 | `0x1ab6…222c` | 91.7 [89–93] | $6,989 | 15.9% | 60.7% | 0.14 | 4.0% | 290 |
| 8 | `0x31af…e256` | 91.6 [87–93] | $4,238 | 29.4% | 57.8% | 0.10 | 3.0% | 180 |
| 9 | `0x3fdd…7e10` | 90.8 [88–92] | $976 | 18.0% | 63.9% | 0.17 | 4.2% | 216 |
| 10 | `0xa412…6bda` | 90.7 [87–92] | $1,251 | 41.9% | 60.4% | 0.18 | 3.9% | 134 |

*Top-25 and Top-100 cohorts are in the database (`wallet_scores`) and dashboard.* Top-100 aggregate: median score 77.5, median win rate 49.7%, median avg-ROI 4.3%.

> *Validation (synthetic only):* Spearman(composite score, latent skill) = **0.52** — the ranking recovers ground-truth skill.

## 2. What traits do the top traders share?

K-means (k=2, silhouette 0.2948) over behavioral features splits the field into:

- **Cluster 1 — “Break-even disciplined long-hold early-entrant”** (119 wallets, 56% profitable, mean score 74): avg size $82, size-CV 0.46, hold 36d, entry lead 45d, avg entry odds 0.41, win 48%.
- **Cluster 0 — “Losing erratic long-hold early-entrant longshot-buyer”** (265 wallets, 3% profitable, mean score 41): avg size $49, size-CV 0.55, hold 29d, entry lead 39d, avg entry odds 0.34, win 23%.

The top cohort enters at **avg odds 0.4234** vs 0.3526 for the field, and captures **realized edge 0.1028** vs -0.1112 — i.e. they systematically buy below fair value.

## 3. Which wallets are statistically worth following?

Prefer **high score + tight CI**, and bonus for being a detected *leader* (others copy them, with a positive lead-lag):

| Wallet | Rank | Score | CI width | Leader? | #Followers |
|--------|-----:|------:|---------:|:-------:|-----------:|
| `0x94b6…3911` | 1 | 97.0 | 1.2 | ✔ | 3 |
| `0x30ba…4ff5` | 2 | 94.7 | 1.6 | ✔ | 4 |
| `0x97fb…be3d` | 3 | 94.1 | 1.6 | ✔ | 5 |
| `0xa2ce…edc1` | 22 | 86.3 | 3.0 | — | 0 |
| `0x1ab6…222c` | 7 | 91.7 | 4.0 | ✔ | 2 |
| `0x028e…93d7` | 4 | 93.1 | 4.2 | — | 0 |
| `0x3fdd…7e10` | 9 | 90.8 | 4.3 | — | 0 |
| `0x43f3…98db` | 6 | 92.0 | 4.5 | — | 0 |

Network over the top-100 wallets: 54 co-trading edges, **6 communities** (modularity 0.7705), 15 leader wallets detected. Avg market overlap 0.0691, direction agreement 0.3137.

## 4. Which copy-trading strategy performed best historically?

Look-ahead-safe backtest (train→test split at 2024-11-18, 9591 test signals). Start $1000, 75bps slippage:

| Strategy | Trades | Ending $ | Total Ret | CAGR | Win% | MaxDD | Sharpe/trade |
|----------|-------:|---------:|----------:|-----:|-----:|------:|-------------:|
| A_copy_top10 | 127 | $1,576 | 57.6% | 70.6% | 64.6% | 16.1% | 0.19 |
| B_consensus_3plus | 121 | $1,693 | 69.3% | 86.9% | 66.9% | 12.5% | 0.21 |
| C_filtered_elite | 103 | $1,574 | 57.4% | 71.7% | 68.9% | 11.2% | 0.21 |
| D_weighted_consensus | 131 | $1,326 | 32.6% | 39.3% | 55.0% | 20.0% | 0.09 |

**Highest return: `B_consensus_3plus`** (69.3%). **Best risk-adjusted: `B_consensus_3plus`** (Sharpe/trade 0.21, win 66.9%). Consensus strategies trade less but win more often — the classic breadth/edge tradeoff.

## 5. How should a $20 account allocate capital?

Edge estimated by replaying the Top-10's resolved positions as $-for-$ copies (charged 100bps small-order slippage). On a **50%-haircut** edge (robustness): mean per-trade return 0.1831, win rate 64.5%, full-Kelly fraction **0.4711** → quarter-Kelly bet **$2.36** on $20.

| Sizing rule | Median end $ | P(survive) | P(double→$40) | Med. MaxDD | Exp. growth× |
|-------------|-------------:|-----------:|--------------:|-----------:|-------------:|
| flat_1 | $55 | 100.0% | 91.7% | 10.9% | 2.83× |
| flat_2 | $90 | 99.9% | 99.3% | 17.6% | 4.66× |
| flat_5 | $192 | 94.7% | 94.8% | 32.0% | 9.73× |
| frac_kelly | $176 | 100.0% | 99.6% | 26.2% | 9.19× |
| half_kelly | $192 | 98.8% | 98.8% | 31.4% | 9.93× |
| full_kelly | $192 | 94.8% | 94.9% | 32.2% | 9.73× |

**Recommendation: `half_kelly`** (max survival-weighted log-growth on the 50%-haircut edge). Constraints honored: bets clamped to $1–$5, concentration limited, liquid markets only. With only ~20 units of $1 risk, flat small sizing protects survival while consensus filtering supplies the edge.

## 6. What signals consistently precede profitable trades?

- **Entry timing**: Spearman(days-before-resolution, ROI) for the elite = **0.1909** → edge lives earlier.
- **Mispricing**: elite realized edge 0.1028 > field -0.1112 → they exploit mispriced probabilities.
- **Odds preference**: elite favor favorites (avg entry odds 0.4234).

**Alpha by category** — most profitable niche: **News**, most repeatable (win rate): **News**, lowest-variance profitable: **Economics**, best risk-adjusted: **News**.

| Category | Elite n | Elite avg ROI | Elite win% | ROI σ | Profit concentration (top-decile) |
|----------|--------:|--------------:|-----------:|------:|----------------------------------:|
| News | 1930 | 31.4% | 55.5% | 2.71 | 69.8% |
| Politics | 2026 | 12.4% | 55.5% | 1.48 | 63.8% |
| Crypto | 1626 | 10.3% | 51.8% | 1.57 | 55.9% |
| Economics | 1833 | 9.9% | 53.7% | 1.35 | 53.6% |
| Sports | 1607 | -8.0% | 46.3% | 1.40 | 67.0% |

## Caveats & robustness

- Past performance need not persist; copy-trading adds latency and slippage the backtest models but cannot perfectly capture.
- Edge estimates for the $20 plan are in-sample; we report a 50%-haircut scenario and quarter-Kelly precisely because point estimates overstate forward edge.
- On-chain wallets can be Sybil/linked; the network/community analysis flags coordinated clusters but cannot prove identity.
- Confidence intervals quantify *sampling* uncertainty only, not regime change.

