"""Unit tests for the core numeric helpers (no database)."""
from __future__ import annotations

import numpy as np

from polytrader.etl.metrics import _cv, _hhi, _max_drawdown, _profit_factor, _sharpe
from polytrader.ranking.scorer import _Percentiler, _winsorize
from polytrader.smallaccount.kelly import _kelly_fraction


def test_sharpe_known():
    assert _sharpe(np.array([0.1])) == 0.0           # too few points
    r = np.array([0.1, 0.1, 0.1])
    assert _sharpe(r) == 0.0                          # zero variance -> 0
    r2 = np.array([0.2, -0.1, 0.3, 0.0])
    assert _sharpe(r2) == np.mean(r2) / np.std(r2, ddof=1)


def test_profit_factor():
    assert _profit_factor(np.array([10.0, -5.0, 5.0])) == 15.0 / 5.0
    assert _profit_factor(np.array([1.0, 2.0])) == 3.0      # all wins
    assert _profit_factor(np.array([-1.0, -2.0])) == 0.0    # all losses


def test_max_drawdown():
    # equity rises then falls: capital 100, pnl path
    pnl = np.array([10.0, 10.0, -40.0, 5.0])   # equity: 110,120,80,85 ; peak 120 -> dd=(120-80)/120
    dd = _max_drawdown(pnl, capital=100.0)
    assert abs(dd - (120 - 80) / 120) < 1e-9
    assert _max_drawdown(np.array([1.0, 2.0]), capital=100.0) >= 0.0
    assert _max_drawdown(np.array([]), 100.0) == 0.0


def test_cv_and_hhi():
    assert _cv(np.array([5.0])) == 0.0
    assert abs(_cv(np.array([2.0, 2.0, 2.0]))) < 1e-9
    # HHI: fully concentrated -> 1, evenly split over 4 -> 0.25
    assert abs(_hhi(np.array([10.0, 0, 0])) - 1.0) < 1e-9
    assert abs(_hhi(np.array([1.0, 1.0, 1.0, 1.0])) - 0.25) < 1e-9


def test_percentiler():
    p = _Percentiler(np.array([0.0, 1.0, 2.0, 3.0]))
    assert p(np.array([3.0]))[0] == 1.0      # top value -> 100th pct
    assert 0.0 < p(np.array([1.5]))[0] < 1.0
    pinv = _Percentiler(np.array([0.0, 1.0, 2.0, 3.0]), invert=True)
    assert pinv(np.array([3.0]))[0] == 0.0   # inverted: max value -> worst


def test_winsorize():
    x = np.array([1.0, 2, 3, 4, 100])
    w = _winsorize(x, 0.2)
    assert w.max() < 100                      # extreme tail clipped


def test_kelly_fraction_favorable():
    # 60% chance to win +1, 40% to lose -1 -> Kelly ~ 0.2 (edge/odds)
    r = np.array([1.0] * 60 + [-1.0] * 40)
    f = _kelly_fraction(r)
    assert 0.1 < f < 0.3
    # no edge -> zero stake
    assert _kelly_fraction(np.array([1.0] * 50 + [-1.0] * 50)) == 0.0
    assert _kelly_fraction(np.array([])) == 0.0
