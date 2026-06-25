"""Copy-trading backtester — event-driven and look-ahead-safe.

Anti-bias design (the whole point)
----------------------------------
1. **Chronological split.**  The timeline is cut at ``train_fraction``.  Elite
   wallets are *selected* using only positions that resolved in the TRAIN window;
   strategies are then simulated only on entries that occur in the TEST window.
   You can never pick a wallet *because* it won the very trades you score it on.
2. **Decisions use only past info.**  Whether to copy depends on the selecting
   wallet's train stats and on how many elite have entered *so far*.  A market's
   settlement is used **only** to realize P&L after the position is held to
   resolution — never to decide entry.
3. **Costs modeled.**  Copiers enter at the signal wallet's fill price *plus*
   slippage; fees are configurable (Polymarket trading fee is currently 0).

Strategies (from config.backtest.strategies):
  A  copy every entry of the Top-N elite
  B  copy a market only once ≥ ``min_elite_in_market`` distinct elite are on the
     same side (consensus), entering at the threshold-crossing fill
  C  copy entries from wallets passing hard train filters (win-rate / ROI / count)
  D  weighted consensus — elite weighted by train performance × recency; size and
     trigger scale with the accumulated weight behind a market
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from sqlalchemy import delete, select

from ..config import get_config
from ..db.models import AnalysisArtifact, Market, Position
from ..db.session import session_scope


# --------------------------------------------------------------------------- #
# train-period wallet selection (look-ahead-safe)
# --------------------------------------------------------------------------- #
def _train_metrics(pos: pd.DataFrame, t_split: datetime) -> pd.DataFrame:
    """Per-wallet performance computed ONLY from positions resolved before t_split."""
    tr = pos[(pos["closed_at"].notna()) & (pos["closed_at"] <= t_split)
             & pos["status"].isin(["settled", "closed"])]
    if tr.empty:
        return pd.DataFrame()
    g = tr.groupby("wallet_address")
    out = g.agg(
        n_resolved=("roi", "size"),
        win_rate=("is_win", lambda s: float((s == True).mean())),  # noqa: E712
        avg_roi=("roi", "mean"),
        total_profit=("realized_pnl_usd", "sum"),
        sharpe=("roi", lambda s: float(s.mean() / s.std(ddof=1)) if s.std(ddof=1) > 1e-9 else 0.0),
        last_active=("closed_at", "max"),
    ).reset_index()
    # composite train score = mean of percentile ranks of the headline metrics
    for c in ["total_profit", "avg_roi", "win_rate", "sharpe"]:
        out[c + "_pct"] = out[c].rank(pct=True)
    out["train_score"] = out[[c + "_pct" for c in
                              ["total_profit", "avg_roi", "win_rate", "sharpe"]]].mean(axis=1)
    return out.sort_values("train_score", ascending=False).reset_index(drop=True)


def _wallet_weights(train: pd.DataFrame, t_split: datetime, halflife_days: float) -> Dict[str, float]:
    """Strategy-D weights: train_score × recency decay (more recent activity weighs more)."""
    if train.empty:
        return {}
    age_days = (t_split - pd.to_datetime(train["last_active"])).dt.total_seconds() / 86400.0
    recency = np.power(0.5, age_days / max(halflife_days, 1.0))
    w = train["train_score"].to_numpy() * recency.to_numpy()
    return dict(zip(train["wallet_address"], w))


# --------------------------------------------------------------------------- #
# common event-driven simulator
# --------------------------------------------------------------------------- #
def _simulate(orders: List[dict], cfg) -> Dict[str, object]:
    """orders: [{t, market, outcome, entry_price, resolved_at, payout, size_mult}].
    Runs fixed-fractional staking with cash & concurrency limits."""
    bt = cfg.backtest
    start = float(bt["starting_capital_usd"])
    f = float(bt["per_trade_fraction"])
    max_conc = int(bt["max_concurrent_positions"])
    slip = float(bt["slippage_bps"]) / 1e4
    fee = float(bt["fee_bps"]) / 1e4

    # build a time-ordered event queue (entries + their eventual exits)
    events = []
    for o in orders:
        events.append((pd.Timestamp(o["t"]), 0, "entry", o))
    events.sort(key=lambda e: (e[0], e[1]))

    cash = start
    open_positions = []  # (resolved_at, stake, payout_per_dollar)
    closed = []          # realized per-trade returns
    equity_curve = []    # (timestamp, equity)
    n_skipped = 0

    def _settle_due(now):
        nonlocal cash
        still = []
        for res_at, stake, ppd in open_positions:
            if res_at <= now:
                cash += stake * (1.0 + ppd)
                closed.append(ppd)
            else:
                still.append((res_at, stake, ppd))
        open_positions[:] = still

    for t, _, _, o in events:
        _settle_due(t)  # free capital from anything resolved by now
        # entry price worsened by slippage; copier holds to resolution
        ep = float(np.clip(o["entry_price"] * (1 + slip) + fee, 0.01, 0.99))
        payout = float(o["payout"])               # 1 if outcome won else 0
        ppd = (payout - ep) / ep                   # per-dollar P&L if held to resolution
        if len(open_positions) >= max_conc:
            n_skipped += 1
        else:
            stake = f * o.get("size_mult", 1.0) * cash
            stake = min(stake, cash)
            if stake > 1e-6 and cash > 1e-6:
                cash -= stake
                open_positions.append((pd.Timestamp(o["resolved_at"]), stake, ppd))
        equity = cash + sum(s for _, s, _ in open_positions)
        equity_curve.append((t, equity))

    # settle the remainder
    last_t = max([pd.Timestamp(o["resolved_at"]) for o in orders], default=None)
    if last_t is not None:
        _settle_due(last_t + timedelta(days=1))
        equity_curve.append((last_t, cash))

    ending = cash
    rets = np.array(closed, dtype=float)
    eq = pd.DataFrame(equity_curve, columns=["t", "equity"]).set_index("t")["equity"]
    if len(eq) > 1:
        running_max = eq.cummax()
        mdd = float(((running_max - eq) / running_max).max())
    else:
        mdd = 0.0
    days = ((eq.index.max() - eq.index.min()).total_seconds() / 86400.0) if len(eq) > 1 else 0.0
    cagr = ((ending / start) ** (365.0 / days) - 1.0) if days > 5 and ending > 0 else 0.0
    sharpe = float(rets.mean() / rets.std(ddof=1)) if len(rets) > 1 and rets.std(ddof=1) > 1e-9 else 0.0

    # downsample equity curve for storage
    if len(eq) > 300:
        eq = eq.iloc[:: max(1, len(eq) // 300)]
    return {
        "n_trades": int(len(rets)), "n_skipped_concurrency": n_skipped,
        "starting_balance": round(start, 2), "ending_balance": round(ending, 2),
        "total_return": round(ending / start - 1.0, 4),
        "cagr": round(cagr, 4), "win_rate": round(float((rets > 0).mean()) if len(rets) else 0.0, 4),
        "avg_trade_return": round(float(rets.mean()) if len(rets) else 0.0, 4),
        "max_drawdown": round(mdd, 4), "sharpe_per_trade": round(sharpe, 4),
        "equity_curve": [[str(t), round(float(v), 2)] for t, v in eq.items()],
    }


# --------------------------------------------------------------------------- #
# strategy order builders
# --------------------------------------------------------------------------- #
def _first_per_market(signals: pd.DataFrame) -> pd.DataFrame:
    """One order per (market, outcome): the earliest qualifying elite entry."""
    return (signals.sort_values("t").groupby(["market_id", "outcome_index"], as_index=False).first())


def _orders_from(signals: pd.DataFrame, size_mult=1.0) -> List[dict]:
    s = _first_per_market(signals)
    return [dict(t=r.t, market=r.market_id, outcome=r.outcome_index, entry_price=r.entry_price,
                 resolved_at=r.resolved_at, payout=r.payout, size_mult=size_mult)
            for r in s.itertuples(index=False)]


def _consensus_orders(signals: pd.DataFrame, min_elite: int) -> List[dict]:
    """Enter once the k-th distinct elite joins a (market, outcome); fill at that entry."""
    orders = []
    for (mid, oc), g in signals.sort_values("t").groupby(["market_id", "outcome_index"]):
        distinct = g.drop_duplicates("wallet_address")
        if len(distinct) >= min_elite:
            trig = distinct.iloc[min_elite - 1]  # the k-th elite's fill
            orders.append(dict(t=trig.t, market=mid, outcome=oc, entry_price=trig.entry_price,
                               resolved_at=trig.resolved_at, payout=trig.payout, size_mult=1.0))
    return orders


def _weighted_consensus_orders(signals: pd.DataFrame, weights: Dict[str, float],
                               trigger: float, cap: float) -> List[dict]:
    """Accumulate elite weight per (market, outcome); enter when it crosses `trigger`,
    sizing ∝ accumulated weight (capped)."""
    orders = []
    for (mid, oc), g in signals.sort_values("t").groupby(["market_id", "outcome_index"]):
        cum = 0.0
        fired = False
        for r in g.itertuples(index=False):
            cum += weights.get(r.wallet_address, 0.0)
            if not fired and cum >= trigger:
                size_mult = float(min(cum / trigger, cap))
                orders.append(dict(t=r.t, market=mid, outcome=oc, entry_price=r.entry_price,
                                   resolved_at=r.resolved_at, payout=r.payout, size_mult=size_mult))
                fired = True
    return orders


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #
def run_backtests(as_of: Optional[datetime] = None) -> Dict[str, object]:
    cfg = get_config()
    bt = cfg.backtest
    strat = bt["strategies"]

    with session_scope() as session:
        pos = pd.read_sql(select(Position.__table__), session.bind)
        mk = pd.read_sql(select(Market.__table__.c.id, Market.__table__.c.winning_outcome,
                                Market.__table__.c.resolved_at), session.bind
                         ).rename(columns={"id": "market_id"})
        if pos.empty:
            return {"note": "no positions"}
        for c in ("opened_at", "closed_at"):
            pos[c] = pd.to_datetime(pos[c])
        mk["resolved_at"] = pd.to_datetime(mk["resolved_at"])
        pos = pos.merge(mk, on="market_id", how="left")
        resolved = pos[pos["status"].isin(["settled", "closed"])].copy()

        # chronological split on entry time
        t_split = resolved["opened_at"].quantile(float(bt["train_fraction"]))
        t_split = pd.Timestamp(t_split)
        if as_of is None:
            as_of = resolved["closed_at"].max().to_pydatetime()

        train = _train_metrics(resolved, t_split)
        if train.empty:
            return {"note": "insufficient train data"}

        # TEST-window entry signals (entries strictly after the split)
        test = resolved[resolved["opened_at"] > t_split].copy()
        test["payout"] = (test["outcome_index"] == test["winning_outcome"]).astype(float)
        test = test.rename(columns={"opened_at": "t"})
        sig_cols = ["t", "wallet_address", "market_id", "outcome_index",
                    "avg_entry_price", "resolved_at", "payout"]
        test = test[sig_cols].rename(columns={"avg_entry_price": "entry_price"})

        def elite_signals(addrs):
            return test[test["wallet_address"].isin(set(addrs))].copy()

        # ----- strategy A: copy Top-N elite -----
        a_top = train.head(int(strat["A"]["top_n"]))["wallet_address"].tolist()
        orders_A = _orders_from(elite_signals(a_top))

        # ----- strategy B: consensus of >= k top-25 elite -----
        b_universe = train.head(int(strat["B"]["top_n"]))["wallet_address"].tolist()
        orders_B = _consensus_orders(elite_signals(b_universe), int(strat["B"]["min_elite_in_market"]))

        # ----- strategy C: hard train filters -----
        c = strat["C"]
        c_mask = ((train["win_rate"] >= float(c["min_win_rate"]))
                  & (train["avg_roi"] >= float(c["min_avg_roi"]))
                  & (train["n_resolved"] >= int(c["min_resolved"])))
        c_wallets = train[c_mask]["wallet_address"].tolist()
        orders_C = _orders_from(elite_signals(c_wallets))

        # ----- strategy D: weighted consensus -----
        d = strat["D"]
        d_universe = train.head(int(d["top_n"]))["wallet_address"].tolist()
        weights = _wallet_weights(train[train["wallet_address"].isin(d_universe)],
                                  t_split, float(d["recency_halflife_days"]))
        wsum = sum(weights.values()) or 1.0
        trigger = 2.0 * (wsum / max(len(weights), 1))  # ~2 average-weight elite
        orders_D = _weighted_consensus_orders(elite_signals(d_universe), weights, trigger, cap=3.0)

        runs = {
            "A_copy_top10": (orders_A, f"Copy every entry of the Top-{strat['A']['top_n']} elite"),
            "B_consensus_3plus": (orders_B,
                f"Copy when ≥{strat['B']['min_elite_in_market']} of the Top-{strat['B']['top_n']} elite share a market side"),
            "C_filtered_elite": (orders_C,
                f"Copy wallets with train win>{c['min_win_rate']:.0%}, ROI>{c['min_avg_roi']:.0%}, ≥{c['min_resolved']} resolved"),
            "D_weighted_consensus": (orders_D, "Weighted consensus (train performance × recency decay)"),
        }

        session.execute(delete(AnalysisArtifact).where(AnalysisArtifact.kind == "backtest"))
        results = {}
        for name, (orders, desc) in runs.items():
            if not orders:
                res = {"note": "no qualifying signals", "n_trades": 0,
                       "ending_balance": float(bt["starting_capital_usd"])}
            else:
                res = _simulate(orders, cfg)
            res["description"] = desc
            res["n_selected_wallets"] = {
                "A_copy_top10": len(a_top), "B_consensus_3plus": len(b_universe),
                "C_filtered_elite": len(c_wallets), "D_weighted_consensus": len(d_universe)}[name]
            results[name] = res
            session.add(AnalysisArtifact(as_of=as_of, kind="backtest", name=name, payload=res))

        meta = {"t_split": str(t_split), "as_of": as_of.isoformat(),
                "n_train_wallets": int(len(train)), "n_test_signals": int(len(test)),
                "train_fraction": float(bt["train_fraction"])}
        session.add(AnalysisArtifact(as_of=as_of, kind="backtest_meta", name="meta", payload=meta))
        return {"meta": meta, "results": {k: {kk: v[kk] for kk in
                ("n_trades", "ending_balance", "total_return", "cagr", "win_rate",
                 "max_drawdown", "sharpe_per_trade", "n_selected_wallets")
                if kk in v} for k, v in results.items()}}
