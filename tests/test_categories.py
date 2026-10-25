"""categories.classify の優先順位と補助関数。"""
from __future__ import annotations

import pytest

from vfm import categories as cat
from vfm.categories import base_symbol, classify, keyword_category, load_overrides

NONE: set[str] = set()


def test_override_beats_keyword():
    assert classify("AGLD-PERP", "Adventure Gold", {"AGLD-PERP": "crypto"}, NONE, NONE) == "crypto"
    assert classify("AGLD-PERP", "Adventure Gold", {}, NONE, NONE) == "commodity"


def test_override_can_assign_any_category():
    assert classify("OPENAI-PERP", "OpenAI", {"OPENAI-PERP": "preipo"}, NONE, NONE) == "preipo"
    assert classify("PAXG-PERP", "PAX Gold", {"PAXG-PERP": "commodity"}, {"PAXG"}, NONE) == "commodity"


def test_keyword_equity_beats_hl_main_membership():
    assert classify("COIN-PERP", "Coinbase Global, Inc.", {}, {"COIN"}, NONE) == "equity"


@pytest.mark.parametrize("name,expected", [
    ("WTI Crude Oil", "commodity"),
    ("Gold", "commodity"),
    ("Silver", "commodity"),
    ("Natural Gas", "commodity"),
    ("S&P 500 Index", "index"),
    ("Nasdaq 100", "index"),
    ("Tesla, Inc.", "equity"),
    ("NVIDIA Corporation", "equity"),
    ("SPDR Gold Trust ETF", "equity"),      # 株式・ETF の語が優先
    ("Apple Inc.", "equity"),
    ("Bitcoin", None),
    ("Ontology Gas", None),                 # Gas 単独はコモディティではない
    ("Golden Retriever", None),             # 単語境界: Golden は Gold に一致しない
    ("SPX6900", None),                      # S&P の語ではない
    ("", None),
])
def test_keyword_category(name, expected):
    assert keyword_category(name) == expected


def test_ontology_gas_is_crypto():
    assert classify("ONG-PERP", "Ontology Gas", {}, NONE, NONE) == "crypto"
    assert classify("ONG-PERP", "Ontology Gas", {}, {"ONG"}, NONE) == "crypto"


def test_commodity_and_index_by_keyword():
    assert classify("CL-PERP", "WTI Crude Oil", {}, NONE, {"CL"}) == "commodity"
    assert classify("SPX500-PERP", "S&P 500 Index", {}, NONE, NONE) == "index"


def test_hl_main_membership_is_crypto():
    assert classify("HYPE-PERP", "Hyperliquid", {}, {"HYPE"}, {"HYPE"}) == "crypto"
    assert classify("PEPE-PERP", "Pepe", {}, {"kPEPE"}, {"PEPE"}) == "crypto"            # k 付き
    assert classify("1000FLOKI-PERP", "FLOKI", {}, {"FLOKI"}, {"1000FLOKI"}) == "crypto"   # 1000 倍
    assert classify("1000FLOKI-PERP", "FLOKI", {}, {"kFLOKI"}, NONE) == "crypto"           # 1000 倍 ↔ k


def test_hip3_ticker_without_keyword_is_equity():
    assert classify("TSLA-PERP", "Tesla", {}, NONE, {"TSLA"}) == "equity"
    assert classify("CVX-PERP", "Convex Finance", {}, NONE, {"CVX"}) == "equity"          # 上書きが無ければ HIP-3 を信じる
    assert classify("CVX-PERP", "Convex Finance", {"CVX-PERP": "crypto"}, NONE, {"CVX"}) == "crypto"


def test_default_is_crypto():
    assert classify("FOO-PERP", "Foo Protocol", {}, NONE, NONE) == "crypto"
    assert classify("FOO-PERP", "", {}, NONE, NONE) == "crypto"


def test_load_overrides_ignores_unknown_categories_and_blank_rows(tmp_path):
    p = tmp_path / "ov.csv"
    p.write_text("ticker,category,reason\nAGLD-PERP,crypto,game\nX-PERP,stocks,unknown\n,crypto,blank\n"
                 "Y-PERP, index ,spaces\nZ-PERP,,empty\n", encoding="utf-8")
    assert load_overrides(p) == {"AGLD-PERP": "crypto", "Y-PERP": "index"}


def test_load_overrides_missing_file(tmp_path):
    assert load_overrides(tmp_path / "none.csv") == {}


def test_load_overrides_real_config_is_valid():
    from pathlib import Path
    ov = load_overrides(Path(__file__).resolve().parents[1] / "config" / "category_overrides.csv")
    assert ov["AGLD-PERP"] == "crypto" and ov["XAUT-PERP"] == "commodity" and ov["OPENAI-PERP"] == "preipo"
    assert set(ov.values()) <= set(cat.CATEGORIES)


@pytest.mark.parametrize("ticker,expected", [
    ("BTC-PERP", "BTC"), ("XAU-SWAP", "XAU"), ("1000PEPE-PERP", "1000PEPE"), ("ABC", "ABC"),
    ("PERP-PERP", "PERP"), ("A-PERP-PERP", "A-PERP"),
])
def test_base_symbol(ticker, expected):
    assert base_symbol(ticker) == expected


def test_category_constants_and_labels():
    assert cat.CATEGORIES == ("crypto", "equity", "commodity", "index", "preipo", "other")
    assert set(cat.LABEL_JA) == set(cat.CATEGORIES)
