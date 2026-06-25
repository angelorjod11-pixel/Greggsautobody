"""Unit tests for the live-collector normalizers (no network)."""
from __future__ import annotations

from polytrader.collect.data_api import normalize_trade
from polytrader.collect.gamma import (categorize, normalize_market,
                                      winning_outcome_from_prices)
from polytrader.collect.onchain import normalize_filled_order

CATMAP = {"Politics": ["election", "senate"], "Crypto": ["bitcoin", "btc"], "Sports": ["nba"]}


def test_categorize():
    assert categorize("Will BTC hit 100k?", [], None, CATMAP) == "Crypto"
    assert categorize("Senate race", [], None, CATMAP) == "Politics"
    assert categorize("random", [], "Sports", CATMAP) == "Sports"  # explicit wins
    assert categorize("nothing here", [], None, CATMAP) is None


def test_winning_outcome():
    assert winning_outcome_from_prices(["1", "0"], True) == 0
    assert winning_outcome_from_prices(["0", "1"], True) == 1
    assert winning_outcome_from_prices(["0.5", "0.5"], True) is None  # indecisive
    assert winning_outcome_from_prices(["1", "0"], False) is None     # unresolved


def test_normalize_market():
    raw = {"conditionId": "0xabc", "question": "Will BTC be above 100k?",
           "slug": "btc", "outcomePrices": '["1","0"]', "clobTokenIds": '["1","2"]',
           "volumeNum": 50000, "liquidityNum": 9000, "closed": True,
           "startDate": "2024-01-01T00:00:00Z", "endDate": "2024-12-31T00:00:00Z"}
    m = normalize_market(raw, CATMAP)
    assert m["id"] == "0xabc" and m["resolved"] and m["winning_outcome"] == 0
    assert m["category"] == "Crypto" and m["volume_usd"] == 50000
    assert m["_clob_token_ids"] == ["1", "2"]


def test_normalize_market_missing_id():
    assert normalize_market({"question": "x"}, CATMAP) is None


def test_normalize_trade():
    raw = {"proxyWallet": "0xDEAD", "side": "buy", "asset": "111", "conditionId": "0xabc",
           "size": 40.0, "price": 0.25, "timestamp": 1704067200, "outcomeIndex": 0,
           "transactionHash": "0xfeed"}
    t = normalize_trade(raw, seq=1)
    assert t["wallet_address"] == "0xdead" and t["side"] == "BUY"
    assert t["shares"] == 40.0 and abs(t["usd_size"] - 10.0) < 1e-9
    assert t["market_id"] == "0xabc" and t["outcome_index"] == 0


def test_normalize_trade_bad_input():
    assert normalize_trade({"proxyWallet": "0x1"}, seq=0) is None  # missing price/size/ts


def test_normalize_filled_order():
    raw = {"id": "f1", "timestamp": "1704067200", "maker": {"id": "0xBEEF"},
           "market": {"id": "0xabc"}, "side": "SELL", "size": "12", "price": "0.8",
           "transactionHash": "0xtx"}
    o = normalize_filled_order(raw)
    assert o["wallet_address"] == "0xbeef" and o["side"] == "SELL"
    assert abs(o["usd_size"] - 9.6) < 1e-9
