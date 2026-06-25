"""Synthetic Polymarket data generator.

Why this exists
---------------
Live collection requires outbound access to the Polymarket API hosts.  When that
is unavailable (sandboxes, CI, offline dev) this module produces a *realistic*
dataset so the entire downstream pipeline — ETL, ranking, clustering,
correlation, backtest, signals, alpha, dashboard — can be run and validated.

Generative model (the important part)
-------------------------------------
The data is **not** random noise; it encodes a ground-truth structure the
analytics are supposed to recover, which is how we validate them:

* Each market has a hidden ``true_prob`` for outcome-0 and a per-market
  ``mispricing`` bias — the crowd is systematically off early, and the price
  converges to the realized outcome as resolution approaches.
* Each wallet has a latent ``skill`` in [0,1].  Skill = a *tighter* estimate of
  ``true_prob``.  A wallet only trades when its estimate disagrees with the
  market price by more than its ``margin``; skilled wallets therefore
  systematically buy underpriced outcomes and earn positive expected ROI, while
  unskilled wallets trade noise and bleed edge + slippage.
* Edge is larger the **earlier** you enter (more mispricing remains), which
  plants the "early-signal" effect the signals module later detects.
* A handful of **leader** wallets are skilled & disciplined; **follower**
  wallets copy a subset of a leader's trades at a lag and worse price — planting
  the correlation/network/lead-lag structure the correlation engine recovers.

The latent skill is persisted to an ``analysis_artifacts`` row (kind=
``ground_truth``) so tests can confirm the ranking actually surfaces skill.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import numpy as np

from ..config import get_config
from ..db.models import Market, Trade, Wallet, AnalysisArtifact
from ..db.session import init_db, session_scope

CATEGORY_TEMPLATES = {
    "Politics": "Will {who} win the {year} {what}?",
    "Sports": "Will {who} beat {what} on {date}?",
    "Crypto": "Will {who} be above ${what} by {date}?",
    "Economics": "Will the {who} {what} in {year}?",
    "News": "Will {who} {what} before {date}?",
}
_FILL = {
    "who": ["the incumbent", "the challenger", "Team A", "BTC", "ETH", "the Fed",
            "the central bank", "the favorite", "the underdog", "the nominee"],
    "what": ["election", "65000", "championship", "raise rates", "cut rates",
             "be confirmed", "100000", "the final", "the primary", "3500"],
}


def _addr(i: int, seed: int) -> str:
    h = hashlib.sha1(f"wallet-{seed}-{i}".encode()).hexdigest()
    return "0x" + h[:40]


@dataclass
class GenParams:
    n_wallets: int = 420
    n_markets: int = 650
    n_leaders: int = 6
    n_followers: int = 24
    skilled_fraction: float = 0.30      # fraction of population with real edge
    start: datetime = datetime(2024, 1, 1)
    months: int = 18
    base_markets_per_wallet: float = 90  # mean markets a wallet considers
    early_exit_prob: float = 0.25        # chance a position is closed before resolution
    categories: List[str] = field(default_factory=lambda:
                                  ["Politics", "Sports", "Crypto", "Economics", "News"])


class SyntheticGenerator:
    def __init__(self, params: Optional[GenParams] = None, seed: Optional[int] = None):
        cfg = get_config()
        self.p = params or GenParams()
        self.rng = np.random.default_rng(seed if seed is not None else cfg.seed)
        self.end = self.p.start + timedelta(days=int(self.p.months * 30.4))

    # ---------------------------------------------------------------- markets
    def _make_markets(self) -> List[dict]:
        rng = self.rng
        markets = []
        span_days = (self.end - self.p.start).days
        for i in range(self.p.n_markets):
            cat = rng.choice(self.p.categories)
            created = self.p.start + timedelta(days=int(rng.uniform(0, span_days - 30)))
            duration = int(rng.uniform(7, 120))
            end = created + timedelta(days=duration)
            true_prob = float(np.clip(rng.beta(2.2, 2.2), 0.04, 0.96))  # P(outcome 0 wins)
            # per-market crowd bias on outcome-0 price (the exploitable mispricing)
            mispricing = float(rng.normal(0, 0.06))
            w0 = int(rng.random() < true_prob)             # 1 => outcome 0 won
            winning_outcome = 0 if w0 else 1
            volume = float(np.exp(rng.normal(10.5, 1.3)))  # ~ $36k median, heavy tail
            tmpl = CATEGORY_TEMPLATES[cat]
            q = tmpl.format(who=rng.choice(_FILL["who"]), what=rng.choice(_FILL["what"]),
                            year=created.year, date=end.strftime("%b%d"))
            markets.append(dict(
                id=f"m{i:05d}", slug=f"market-{i:05d}", question=q, category=cat,
                created_at=created, end_date=end, resolved=True, resolved_at=end,
                winning_outcome=winning_outcome, volume_usd=volume,
                liquidity_usd=volume * float(rng.uniform(0.05, 0.2)),
                _true_prob=true_prob, _mispricing=mispricing, _w0=w0,
            ))
        return markets

    def _price0(self, m: dict, t_frac: float, noise: float) -> float:
        """Displayed price of outcome-0 at normalized time t_frac in [0,1]."""
        conv = t_frac ** 1.3                       # convergence to realized outcome
        fair = (1 - conv) * (m["_true_prob"] + m["_mispricing"]) + conv * m["_w0"]
        return float(np.clip(fair + noise, 0.02, 0.98))

    # ---------------------------------------------------------------- wallets
    def _make_wallets(self) -> List[dict]:
        rng = self.rng
        n = self.p.n_wallets
        wallets = []
        # archetype assignment
        leader_ids = set(range(self.p.n_leaders))
        follower_ids = set(range(self.p.n_leaders, self.p.n_leaders + self.p.n_followers))
        for i in range(n):
            if i in leader_ids:
                skill = float(np.clip(rng.uniform(0.80, 0.97), 0, 1)); arch = "leader"
            elif i in follower_ids:
                skill = float(np.clip(rng.uniform(0.45, 0.7), 0, 1)); arch = "follower"
            else:
                skilled = rng.random() < self.p.skilled_fraction
                skill = float(np.clip(rng.beta(5, 2) if skilled else rng.beta(2, 5), 0, 1))
                arch = "skilled" if skilled else "noise"
            disciplined = arch in ("leader",) or (rng.random() < 0.4 + 0.4 * skill)
            # `info` = genuine forecasting skill: how far the wallet's estimate is
            # pulled toward the realized outcome before the price reveals it. This,
            # not knowledge of the prior probability, is what generates real edge.
            info_by_arch = {"leader": rng.uniform(0.26, 0.40),
                            "skilled": rng.uniform(0.14, 0.28),
                            "follower": rng.uniform(0.08, 0.18),
                            "noise": rng.uniform(0.0, 0.06)}
            wallets.append(dict(
                idx=i, address=_addr(i, get_config().seed), archetype=arch, skill=skill,
                info=float(info_by_arch[arch]),
                est_sigma=float(0.02 + (1 - skill) * 0.16),       # estimate error
                margin=float(rng.uniform(0.04, 0.12)),            # required edge to act
                base_size=float(np.exp(rng.normal(3.0 + 1.4 * skill, 0.6))),  # ~$20-300
                size_cv=float(0.15 if disciplined else rng.uniform(0.5, 1.1)),
                activity=float(rng.uniform(0.4, 1.0) * (1.3 if arch in ("leader", "skilled") else 1.0)),
                cat_pref=rng.dirichlet(np.ones(len(self.p.categories)) *
                                       rng.uniform(0.4, 1.5)),
                hold_to_res=float(0.85 if disciplined else rng.uniform(0.4, 0.8)),
            ))
        return wallets

    # ------------------------------------------------------------- one trade
    def _wallet_trade_in_market(self, w: dict, m: dict, trades: List[dict],
                                forced_outcome: Optional[int] = None,
                                t_frac_override: Optional[float] = None,
                                price_penalty: float = 0.0) -> Optional[dict]:
        """Possibly open (and maybe close early) a position. Appends trade dicts.
        Returns the opening trade dict (for follower copying) or None if skipped."""
        rng = self.rng
        # entry time: skilled enter earlier (more edge); bounded inside market life
        if t_frac_override is not None:
            t_frac = float(np.clip(t_frac_override, 0.01, 0.97))
        else:
            base = rng.beta(1.6, 3.0)            # skew early
            t_frac = float(np.clip(base * (0.7 + 0.5 * (1 - w["skill"])), 0.01, 0.97))
        noise = rng.normal(0, 0.03 + 0.05 * (1 - w["skill"]))
        q0 = self._price0(m, t_frac, noise)
        # Forecast estimate: prior probability pulled toward the realized outcome by
        # `info` (foresight), plus idiosyncratic estimation error. Skilled wallets
        # have high info + low sigma, so they identify the winning side early —
        # before the price (which only converges to the outcome over time) reflects it.
        est0 = float(np.clip(
            m["_true_prob"] + w["info"] * (m["_w0"] - m["_true_prob"])
            + rng.normal(0, w["est_sigma"]), 0.01, 0.99))

        if forced_outcome is not None:
            outcome = forced_outcome
        else:
            edge0 = est0 - q0            # buy outcome0 if it looks underpriced
            edge1 = (1 - est0) - (1 - q0)
            if max(edge0, edge1) < w["margin"]:
                return None              # no clear edge -> sit out
            outcome = 0 if edge0 >= edge1 else 1

        entry_price = q0 if outcome == 0 else (1 - q0)
        # bid-ask spread / slippage paid on entry -> erodes edge, pushes pure-noise
        # traders to negative expectancy (only genuine `info` overcomes it).
        spread = 0.004 + abs(rng.normal(0, 0.004))
        entry_price = float(np.clip(entry_price + spread + price_penalty, 0.02, 0.98))
        # position sizing
        size_usd = float(max(1.0, rng.lognormal(np.log(max(w["base_size"], 1.0)), w["size_cv"])))
        shares = size_usd / entry_price
        t_created = m["created_at"]
        life = (m["end_date"] - t_created).total_seconds()
        ts = t_created + timedelta(seconds=t_frac * life)

        open_trade = dict(
            id=f"{w['address']}-{m['id']}-{outcome}-o-{len(trades)}",
            wallet_address=w["address"], market_id=m["id"], outcome_index=outcome,
            side="BUY", timestamp=ts, price=entry_price, shares=shares,
            usd_size=entry_price * shares, tx_hash=None,
        )
        trades.append(open_trade)

        # maybe exit early before resolution
        if rng.random() > w["hold_to_res"]:
            t_exit = float(np.clip(t_frac + rng.uniform(0.1, 0.6), t_frac + 0.05, 0.99))
            exit_noise = rng.normal(0, 0.02)
            q0_exit = self._price0(m, t_exit, exit_noise)
            exit_price = q0_exit if outcome == 0 else (1 - q0_exit)
            exit_price = float(np.clip(exit_price, 0.02, 0.98))
            ts_exit = t_created + timedelta(seconds=t_exit * life)
            trades.append(dict(
                id=f"{w['address']}-{m['id']}-{outcome}-x-{len(trades)}",
                wallet_address=w["address"], market_id=m["id"], outcome_index=outcome,
                side="SELL", timestamp=ts_exit, price=exit_price, shares=shares,
                usd_size=exit_price * shares, tx_hash=None,
            ))
        return open_trade

    # ----------------------------------------------------------------- driver
    def generate(self, drop: bool = True) -> Dict[str, int]:
        rng = self.rng
        cfg = get_config()
        markets = self._make_markets()
        wallets = self._make_wallets()
        m_by_cat: Dict[str, List[dict]] = {c: [] for c in self.p.categories}
        for m in markets:
            m_by_cat[m["category"]].append(m)

        trades: List[dict] = []
        leader_trades: Dict[int, List[dict]] = {}  # leader idx -> opening trades

        for w in wallets:
            n_consider = int(max(8, rng.normal(self.p.base_markets_per_wallet * w["activity"],
                                               self.p.base_markets_per_wallet * 0.3)))
            # choose markets weighted by category preference
            chosen = []
            cats = rng.choice(self.p.categories, size=n_consider,
                              p=w["cat_pref"] / w["cat_pref"].sum())
            for c in cats:
                pool = m_by_cat[c]
                if pool:
                    chosen.append(pool[int(rng.integers(0, len(pool)))])
            opened = []
            for m in chosen:
                ot = self._wallet_trade_in_market(w, m, trades)
                if ot is not None:
                    opened.append(ot)
            if w["archetype"] == "leader":
                leader_trades[w["idx"]] = opened

        # followers copy a random leader's subset, lagged & at a worse price
        leaders = [w for w in wallets if w["archetype"] == "leader"]
        followers = [w for w in wallets if w["archetype"] == "follower"]
        for f in followers:
            if not leaders:
                break
            lead = leaders[int(rng.integers(0, len(leaders)))]
            src = leader_trades.get(lead["idx"], [])
            if not src:
                continue
            k = int(len(src) * rng.uniform(0.4, 0.8))
            for ot in rng.permutation(src)[:k]:
                m = next((mm for mm in markets if mm["id"] == ot["market_id"]), None)
                if m is None:
                    continue
                # follower enters later (lag) on the same outcome, slightly worse price
                life = (m["end_date"] - m["created_at"]).total_seconds()
                base_frac = (ot["timestamp"] - m["created_at"]).total_seconds() / max(life, 1)
                lag_frac = base_frac + rng.uniform(0.01, 0.08)
                self._wallet_trade_in_market(
                    f, m, trades, forced_outcome=ot["outcome_index"],
                    t_frac_override=lag_frac, price_penalty=float(abs(rng.normal(0.01, 0.01))))

        # ----- write to DB -----
        init_db(drop=drop)
        with session_scope() as s:
            s.bulk_save_objects([Market(**{k: v for k, v in m.items()
                                           if not k.startswith("_")}) for m in markets])
            wallet_rows, first_seen, last_seen = {}, {}, {}
            for t in trades:
                a = t["wallet_address"]
                first_seen[a] = min(first_seen.get(a, t["timestamp"]), t["timestamp"])
                last_seen[a] = max(last_seen.get(a, t["timestamp"]), t["timestamp"])
            arch_by_addr = {w["address"]: w["archetype"] for w in wallets}
            for w in wallets:
                a = w["address"]
                if a in first_seen:  # only persist wallets that actually traded
                    wallet_rows[a] = Wallet(address=a, label=arch_by_addr[a],
                                            first_seen=first_seen[a], last_seen=last_seen[a])
            s.bulk_save_objects(list(wallet_rows.values()))
            s.bulk_insert_mappings(Trade, [t for t in trades
                                           if t["wallet_address"] in wallet_rows])
            # persist ground truth (latent skill) for validation
            s.add(AnalysisArtifact(
                as_of=self.end, kind="ground_truth", name="wallet_skill",
                payload={w["address"]: {"skill": w["skill"], "archetype": w["archetype"]}
                         for w in wallets if w["address"] in wallet_rows}))
        return {"markets": len(markets), "wallets": len(wallet_rows), "trades":
                len([t for t in trades if t["wallet_address"] in wallet_rows])}


def generate_synthetic(drop: bool = True, **kwargs) -> Dict[str, int]:
    params = GenParams(**{k: v for k, v in kwargs.items() if k in GenParams.__annotations__})
    return SyntheticGenerator(params).generate(drop=drop)
