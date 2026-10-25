"""model.parse_stats: 固定データの件数、funding の有無、swap の辞書、壊れた形の扱い。"""
from __future__ import annotations

import copy
import json

import pytest

from tests.conftest import (PAD_PREFIX, T0, body, funded_listings, get_listing, load_doc, pad_listings,
                            parse, perp_listings, swap_listings)
from vfm.model import Perp, SchemaError, _int, _num_str, parse_stats


def test_fixture_parses_to_expected_counts(doc):
    padded = pad_listings(copy.deepcopy(doc))
    st = parse_stats(json.dumps(padded).encode())
    assert len(st.perps) == len(perp_listings(padded))
    assert len(st.swaps) == len(swap_listings(padded)) == 2
    assert st.num_markets == doc["num_markets"]
    assert st.skipped == []
    real = [p for p in st.perps if not p.ticker.startswith(PAD_PREFIX)]
    assert len(real) == len(perp_listings(doc)) == 13
    assert sum(p.has_funding for p in real) == len(funded_listings(doc)) == 12
    assert [p.ticker for p in real][:3] == ["BTC-PERP", "ETH-PERP", "SOL-PERP"]   # 並び順は保つ


def test_fixture_covers_required_shapes(doc):
    ivs = {x["funding"]["interval_seconds"] for x in funded_listings(doc)}
    assert ivs == {3600, 14400, 28800}
    assert any(float(x["funding"]["last"]["interval_rate"]) < 0 for x in funded_listings(doc))
    assert get_listing(doc, "BTC-PERP")["funding"]["interval_seconds"] == 28800
    assert get_listing(doc, "NVDA-PERP")["funding"] is None


def test_perp_fields_are_kept_as_strings(doc):
    st = parse(doc)
    btc = next(p for p in st.perps if p.ticker == "BTC-PERP")
    assert btc.name == "Bitcoin"
    assert btc.has_funding
    assert (btc.interval_seconds, btc.next_timestamp) == (28800, T0)
    assert btc.interval_rate == "0.0001" and btc.annualized_rate == "0.1095"
    assert isinstance(btc.mark_price, str) and btc.mark_price.startswith("80433.95")
    assert isinstance(btc.long_oi, str) and isinstance(btc.short_oi, str)
    assert isinstance(btc.volume_24h, str) and isinstance(btc.spread_bps, str)
    order = next(p for p in st.perps if p.ticker == "ORDER-PERP")
    assert order.interval_seconds == 3600 and order.interval_rate == "-0.002854"


def test_perp_with_funding_null_has_no_funding_but_keeps_market_data(doc):
    st = parse(doc)
    nvda = next(p for p in st.perps if p.ticker == "NVDA-PERP")
    assert nvda.has_funding is False
    assert nvda.interval_seconds is None and nvda.next_timestamp is None and nvda.interval_rate is None
    assert nvda.mark_price is not None and nvda.long_oi is not None
    assert not any("NVDA" in s for s in st.skipped)


def test_swaps_keep_last_and_next_dicts(doc):
    st = parse(doc)
    copp = next(s for s in st.swaps if s.ticker == "COPP-SWAP")
    assert copp.name == "Swap on Copper Spot"
    assert copp.interval_seconds == 86400 and copp.next_timestamp is None
    assert copp.last == {"annualized_long_rate": "0.1955", "annualized_short_rate": "0.1955",
                         "interval_long_rate": "0.0005431", "interval_short_rate": "0.0005431"}
    assert copp.next is None   # API は next: null


def test_swap_next_dict_is_kept_and_non_numeric_values_dropped(doc):
    sw = get_listing(doc, "COPP-SWAP")
    sw["funding"]["next"] = {"annualized_long_rate": "0.2", "annualized_short_rate": "0.1", "note": "n/a"}
    sw["funding"]["next_timestamp"] = T0
    sw["funding"]["last"]["interval_long_rate"] = "abc"
    st = parse(doc)
    copp = next(s for s in st.swaps if s.ticker == "COPP-SWAP")
    assert copp.next == {"annualized_long_rate": "0.2", "annualized_short_rate": "0.1"}
    assert "interval_long_rate" not in copp.last and copp.last["interval_short_rate"] == "0.0005431"
    assert copp.next_timestamp == T0


def test_swap_without_funding(doc):
    get_listing(doc, "XAU-SWAP")["funding"] = None
    st = parse(doc)
    xau = next(s for s in st.swaps if s.ticker == "XAU-SWAP")
    assert xau.last is None and xau.next is None and xau.interval_seconds is None


@pytest.mark.parametrize("raw", [b"not json", b"", b"{", "\x00"])
def test_non_json_raises_schema_error(raw):
    with pytest.raises(SchemaError, match="not json"):
        parse_stats(raw)


@pytest.mark.parametrize("obj", [{"num_markets": 1}, {"listings": {"a": 1}}, [1, 2, 3], "str", None, {"listings": None}])
def test_missing_listings_raises_schema_error(obj):
    with pytest.raises(SchemaError, match="listings missing"):
        parse_stats(json.dumps(obj))


def test_too_few_listings_raises_schema_error(doc):
    assert len(doc["listings"]) < 50
    with pytest.raises(SchemaError, match="too few listings"):
        parse_stats(body(doc, pad=False))


def test_exactly_fifty_listings_is_accepted(doc):
    padded = pad_listings(copy.deepcopy(doc), 50)
    assert len(padded["listings"]) == 50
    assert len(parse_stats(json.dumps(padded).encode()).perps) == 48


def test_no_perpetual_listings_raises(doc):
    swaps = swap_listings(doc)
    listings = []
    for i in range(50):
        x = copy.deepcopy(swaps[i % 2])
        x["ticker"] = f"S{i}-SWAP"
        listings.append(x)
    with pytest.raises(SchemaError, match="no perpetual"):
        parse_stats(json.dumps({"listings": listings}))


def test_non_numeric_interval_rate_is_reported_in_skipped(doc):
    get_listing(doc, "ETH-PERP")["funding"]["last"]["interval_rate"] = "abc"
    st = parse(doc)
    eth = next(p for p in st.perps if p.ticker == "ETH-PERP")
    assert eth.has_funding is False     # funding は使わないが銘柄自体は残る(スナップショット用)
    assert eth.interval_rate is None
    assert st.skipped == ["ETH-PERP: funding shape"]


@pytest.mark.parametrize("mutate", [
    lambda f: f.__setitem__("interval_seconds", "28800"),   # 文字列は不可
    lambda f: f.__setitem__("interval_seconds", 0),
    lambda f: f.__setitem__("interval_seconds", True),
    lambda f: f.__setitem__("next_timestamp", None),
    lambda f: f.__setitem__("next_timestamp", "1792886400"),
    lambda f: f.__setitem__("last", {"annualized_rate": "0.1"}),   # interval_rate 無し
])
def test_broken_funding_shape_is_skipped(doc, mutate):
    mutate(get_listing(doc, "SOL-PERP")["funding"])
    st = parse(doc)
    sol = next(p for p in st.perps if p.ticker == "SOL-PERP")
    assert sol.has_funding is False
    assert st.skipped == ["SOL-PERP: funding shape"]


def test_funding_without_last_is_not_an_error(doc):
    get_listing(doc, "SOL-PERP")["funding"] = {"interval_seconds": 28800, "next_timestamp": T0}
    st = parse(doc)
    sol = next(p for p in st.perps if p.ticker == "SOL-PERP")
    assert sol.has_funding is False and st.skipped == []


def test_unknown_contract_type_and_bad_listing_are_skipped(doc):
    get_listing(doc, "WIF-PERP")["contract_type"] = "option"
    doc["listings"].append({"contract_type": "perpetual", "name": "no ticker"})
    doc["listings"].append("junk")
    st = parse(doc)
    assert "WIF-PERP" not in {p.ticker for p in st.perps}
    assert "WIF-PERP: contract_type='option'" in st.skipped
    assert len(st.skipped) == 3


def test_missing_name_falls_back_to_ticker_and_bad_numbers_become_none(doc):
    x = get_listing(doc, "SOL-PERP")
    del x["name"]
    x["mark_price"] = "not-a-number"
    x["open_interest"] = "oops"
    st = parse(doc)
    sol = next(p for p in st.perps if p.ticker == "SOL-PERP")
    assert sol.name == "SOL-PERP"
    assert sol.mark_price is None and sol.long_oi is None and sol.short_oi is None
    assert sol.has_funding


def test_num_markets_absent_is_none(doc):
    del doc["num_markets"]
    assert parse(doc).num_markets is None


@pytest.mark.parametrize("v,expected", [
    (None, None), (True, None), (False, None), (5, "5"), (1.5, "1.5"), ("0.0001", "0.0001"),
    ("-0.002854", "-0.002854"), ("1e-5", "1e-5"), ("abc", None), ("", None), ([1], None), ({"a": 1}, None),
])
def test_num_str(v, expected):
    assert _num_str(v) == expected


@pytest.mark.parametrize("v,expected", [(28800, 28800), (28800.0, 28800), (True, None), ("28800", None), (None, None)])
def test_int(v, expected):
    assert _int(v) == expected


def test_perp_has_funding_property():
    p = Perp("X-PERP", "X", None, None, None, None, None)
    assert p.has_funding is False
    p.interval_seconds, p.next_timestamp, p.interval_rate = 3600, T0, "0"
    assert p.has_funding is True
