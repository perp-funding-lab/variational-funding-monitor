"""hl: Hyperliquid の銘柄対応と 7 日年率(ネットワークは使わない)。"""
from __future__ import annotations

import pytest

from vfm import hl
from vfm.hl import HLError, apr_7d, fetch_7d, fetch_universe, load_mapping, resolve_one, update_mapping

UNIVERSE = {
    "main": ["BTC", "ETH", "kPEPE", "kFLOKI", "kNEIRO", "MOG", "HYPE"],
    "hip3": {
        "xyz": {"COIN": {"name": "xyz:COIN", "oi_usd": 100.0}, "TSLA": {"name": "xyz:TSLA", "oi_usd": 50.0}},
        "abc": {"TSLA": {"name": "abc:TSLA", "oi_usd": 500.0}},
    },
}


@pytest.mark.parametrize("ticker,expected", [
    ("BTC-PERP", ("BTC", "本体")),
    ("HYPE-PERP", ("HYPE", "本体")),
    ("PEPE-PERP", ("kPEPE", "単位違い(k = 1000 倍)")),
    ("1000FLOKI-PERP", ("kFLOKI", "単位違い(1000 倍 ↔ k)")),
    ("1NEIRO-PERP", ("kNEIRO", "単位違い(1 倍 ↔ k)")),
    ("1000000MOG-PERP", ("MOG", "単位違い(1000000 倍)")),
    ("COIN-PERP", ("xyz:COIN", "HIP-3 xyz")),
    ("ZZZ-PERP", ("", "対応なし")),
    ("1000ZZZ-PERP", ("", "対応なし")),
])
def test_resolve_one(ticker, expected):
    assert resolve_one(ticker, UNIVERSE) == expected


def test_resolve_one_hip3_on_two_dexes_picks_larger_oi_and_notes_count():
    coin, note = resolve_one("TSLA-PERP", UNIVERSE)
    assert coin == "abc:TSLA"
    assert note.startswith("HIP-3 abc") and "2 DEX" in note


def test_resolve_one_main_beats_hip3_and_k_prefix():
    uni = {"main": ["COIN", "kCOIN"], "hip3": {"xyz": {"COIN": {"name": "xyz:COIN", "oi_usd": 1e9}}}}
    assert resolve_one("COIN-PERP", uni) == ("COIN", "本体")


def test_resolve_one_tolerates_missing_keys():
    assert resolve_one("BTC-PERP", {}) == ("", "対応なし")
    assert resolve_one("BTC-PERP", {"main": ["BTC"]}) == ("BTC", "本体")
    assert resolve_one("COIN-PERP", {"hip3": {"xyz": {"COIN": {"name": "xyz:COIN"}}}}) == ("xyz:COIN", "HIP-3 xyz")   # oi_usd 無し


def test_update_mapping_appends_only_new_tickers_and_keeps_hand_edits(tmp_path):
    path = tmp_path / "config" / "hl_mapping.csv"
    mapping, added = update_mapping(path, ["COIN-PERP", "BTC-PERP"], UNIVERSE)
    assert added == ["BTC-PERP", "COIN-PERP"]                      # ソート順
    assert mapping == {"BTC-PERP": {"hl_coin": "BTC", "note": "本体"}, "COIN-PERP": {"hl_coin": "xyz:COIN", "note": "HIP-3 xyz"}}
    text = path.read_text(encoding="utf-8")
    assert text == "ticker,hl_coin,note\nBTC-PERP,BTC,本体\nCOIN-PERP,xyz:COIN,HIP-3 xyz\n"
    # 手で直す
    path.write_text(text.replace("BTC-PERP,BTC,本体", "BTC-PERP,BTCX,手動: 別名"), encoding="utf-8")
    mapping2, added2 = update_mapping(path, ["BTC-PERP", "COIN-PERP", "PEPE-PERP"], UNIVERSE)
    assert added2 == ["PEPE-PERP"]
    assert mapping2["BTC-PERP"] == {"hl_coin": "BTCX", "note": "手動: 別名"}
    assert mapping2["PEPE-PERP"] == {"hl_coin": "kPEPE", "note": "単位違い(k = 1000 倍)"}
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "ticker,hl_coin,note" and len(lines) == 4
    assert lines[1] == "BTC-PERP,BTCX,手動: 別名" and lines[3] == "PEPE-PERP,kPEPE,単位違い(k = 1000 倍)"
    # 何も増えなければファイルは変わらない
    before = path.read_text(encoding="utf-8")
    mapping3, added3 = update_mapping(path, ["BTC-PERP"], UNIVERSE)
    assert added3 == [] and path.read_text(encoding="utf-8") == before and mapping3 == mapping2


def test_update_mapping_records_unresolved_tickers_with_empty_coin(tmp_path):
    path = tmp_path / "m.csv"
    mapping, added = update_mapping(path, ["ZZZ-PERP"], UNIVERSE)
    assert mapping == {"ZZZ-PERP": {"hl_coin": "", "note": "対応なし"}}
    assert path.read_text(encoding="utf-8").splitlines()[1] == "ZZZ-PERP,,対応なし"
    # 空の hl_coin は「対応なし」として残り、次回も解決し直さない
    mapping2, added2 = update_mapping(path, ["ZZZ-PERP"], {"main": ["ZZZ"]})
    assert added2 == [] and mapping2["ZZZ-PERP"]["hl_coin"] == ""


def test_load_mapping(tmp_path):
    assert load_mapping(tmp_path / "none.csv") == {}
    p = tmp_path / "m.csv"
    p.write_text("ticker,hl_coin,note\nBTC-PERP, BTC ,x\nX-PERP,,\n,Y,blank\n", encoding="utf-8")
    assert load_mapping(p) == {"BTC-PERP": {"hl_coin": "BTC", "note": "x"}, "X-PERP": {"hl_coin": "", "note": ""}}


def test_apr_7d_sums_and_annualizes():
    entries = [{"fundingRate": "0.0001", "time": 1}] * 168
    apr, n = apr_7d(entries)
    assert n == 168 and apr == pytest.approx(0.0168 / 7 * 365)
    apr2, n2 = apr_7d([{"fundingRate": "0.001"}, {"fundingRate": "-0.0005"}, {"fundingRate": "x"}, {"time": 3}, {"fundingRate": None}])
    assert n2 == 2 and apr2 == pytest.approx(0.0005 / 7 * 365)
    assert apr_7d([]) == (None, 0)
    assert apr_7d([{"fundingRate": "abc"}]) == (None, 0)


def test_fetch_7d_respects_deadline(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(hl.time, "monotonic", lambda: clock["t"])

    def sleep(s):
        clock["t"] += s

    calls = []

    def post(payload):
        calls.append(payload)
        return [{"fundingRate": "0.0001"}] * 168

    now_ms = 1_792_886_400_000
    out = fetch_7d(["C", "A", "B"], now_ms, post=post, sleep=sleep, deadline_s=1.0)
    assert [p["coin"] for p in calls] == ["A", "B"]                 # ソート順、3 つ目は諦める
    assert out["_truncated"] == {"after": 2}
    assert set(out) == {"A", "B", "_truncated"}
    assert out["A"] == {"apr7d": pytest.approx(0.0168 / 7 * 365), "n": 168}
    assert calls[0] == {"type": "fundingHistory", "coin": "A", "startTime": now_ms - 7 * 86400 * 1000}


def test_fetch_7d_paces_between_coins_and_dedupes():
    sleeps = []
    out = fetch_7d(["B", "A", "A"], 0, post=lambda p: [{"fundingRate": "0.001"}], sleep=sleeps.append, deadline_s=1000)
    assert sleeps == [hl.PACE_SECONDS]
    assert set(out) == {"A", "B"} and "_truncated" not in out
    assert out["A"] == {"apr7d": pytest.approx(0.001 / 7 * 365), "n": 1}


def test_fetch_7d_handles_429_by_sleeping_and_retrying_once():
    attempts = {"n": 0}
    sleeps = []

    def post(payload):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise HLError("HTTP 429 for fundingHistory")
        return [{"fundingRate": "0.0002"}] * 10

    out = fetch_7d(["A"], 0, post=post, sleep=sleeps.append, deadline_s=1000)
    assert sleeps == [10]
    assert attempts["n"] == 2
    assert out == {"A": {"apr7d": pytest.approx(0.002 / 7 * 365), "n": 10}}


def test_fetch_7d_skips_coin_when_retry_also_429():
    sleeps = []

    def post(payload):
        raise HLError("HTTP 429 for fundingHistory")

    out = fetch_7d(["A", "B"], 0, post=post, sleep=sleeps.append, deadline_s=1000)
    assert out == {}
    assert sleeps == [10, hl.PACE_SECONDS, 10]


def test_fetch_7d_skips_other_errors_and_non_list_responses():
    sleeps = []

    def post(payload):
        if payload["coin"] == "A":
            raise HLError("HTTP 500 for fundingHistory")
        if payload["coin"] == "B":
            return {"error": "x"}
        return []

    out = fetch_7d(["A", "B", "C"], 0, post=post, sleep=sleeps.append, deadline_s=1000)
    assert out == {}                      # C は空の履歴 → apr None → 入れない
    assert sleeps == [hl.PACE_SECONDS, hl.PACE_SECONDS]


def test_fetch_7d_lets_unexpected_exceptions_propagate():
    def post(payload):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        fetch_7d(["A"], 0, post=post, sleep=lambda s: None)


def test_fetch_universe_with_fake_post():
    sleeps = []

    def post(payload):
        t = payload["type"]
        if t == "meta":
            return {"universe": [{"name": "BTC"}, {"name": "OLD", "isDelisted": True}, {"name": "ETH", "isDelisted": False}]}
        if t == "perpDexs":
            return [None, {"name": "xyz"}, {"name": "bad"}, {"nope": 1}]
        if t == "metaAndAssetCtxs" and payload["dex"] == "xyz":
            return [{"universe": [{"name": "xyz:COIN"}, {"name": "xyz:GONE", "isDelisted": True}, {"name": "xyz:TSLA"}]},
                    [{"openInterest": "10", "markPx": "5"}, {"openInterest": "1", "markPx": "1"}, {"openInterest": "x", "markPx": "1"}]]
        raise HLError("HTTP 500 for metaAndAssetCtxs")

    uni = fetch_universe(post=post, sleep=sleeps.append)
    assert uni["main"] == ["BTC", "ETH"]
    assert uni["hip3"] == {"xyz": {"COIN": {"name": "xyz:COIN", "oi_usd": 50.0}, "TSLA": {"name": "xyz:TSLA", "oi_usd": 0.0}}}
    assert sleeps == [hl.PACE_SECONDS, hl.PACE_SECONDS]       # DEX ごとに 1 回
    assert uni["fetched"].endswith("Z")


def test_fetch_universe_without_dexs():
    def post(payload):
        return {"universe": [{"name": "BTC"}]} if payload["type"] == "meta" else None

    uni = fetch_universe(post=post, sleep=lambda s: None)
    assert uni["main"] == ["BTC"] and uni["hip3"] == {}


def test_constants():
    assert hl.MAPPING_HEADER == ["ticker", "hl_coin", "note"]
    assert hl.SEVEN_DAYS_MS == 7 * 86400 * 1000
    assert hl.PACE_SECONDS == pytest.approx(1.1)
    assert not hl.USER_AGENT.lower().startswith("python-urllib")
