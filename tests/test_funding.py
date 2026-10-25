"""funding.process_run / rebuild_from_obs: 観測ログの間引きと確定 FR(両モード)、欠測、間隔変更、矛盾判定。"""
from __future__ import annotations

import copy

import pytest

from tests.conftest import RUN1, RUN2, RUN3, T0, Simulator, advance, advance_to, load_doc, real_perps, set_rate
from vfm.funding import (CLOCK_SKEW_SECONDS, KIND_CHANGE, KIND_FIRST, KIND_LAST, ST_CONFIRMED, ST_ESTIMATED, ST_MISSING,
                         STALL_SECONDS, gap_rows_confirmed, is_trivial_rate, missing_rows, process_run, rate_equal,
                         rebuild_from_obs)
from vfm.model import Perp
from vfm.timeutil import parse_iso, to_unix

IV8, IV4, IV1 = 28800, 14400, 3600


def perp(ticker="BTC-PERP", iv=IV8, nt=T0, rate="0.0001", ann=None) -> Perp:
    return Perp(ticker, ticker, "1", "1", "1", "1", "1", iv, nt, rate, ann)


def obs_row(ts, ticker, iv, nt, rate, kind=KIND_FIRST) -> dict:
    """store.load_last_obs が返す形(CSV 由来なので値は文字列)。"""
    return {"obs_ts": ts, "ticker": ticker, "interval_seconds": str(iv), "next_timestamp": str(nt),
            "interval_rate": rate, "annualized_rate": "", "kind": kind}


def kinds(rows, ticker=None):
    return [r["kind"] for r in rows if ticker is None or r["ticker"] == ticker]


def of(rows, ticker):
    return [r for r in rows if r["ticker"] == ticker]


def n_funded(perps):
    return sum(1 for p in perps if p.has_funding)


# ---------------------------------------------------------------- estimated

def test_estimated_run1_all_new_writes_first_rows_and_no_funding():
    doc = load_doc()
    perps = real_perps(doc)
    sim = Simulator(ST_ESTIMATED)
    r1 = sim.run(perps, RUN1)
    assert len(r1.obs_rows) == n_funded(perps) == 12
    assert set(kinds(r1.obs_rows)) == {KIND_FIRST}
    assert all(r["obs_ts"] == RUN1 and int(r["next_timestamp"]) == T0 for r in r1.obs_rows)
    assert "NVDA-PERP" not in {r["ticker"] for r in r1.obs_rows}     # funding: null は無視
    assert r1.funding_rows == [] and r1.anomalies == []
    assert r1.n_new == 12 and r1.n_rollover == 0 and r1.n_checked == 0
    btc = of(r1.obs_rows, "BTC-PERP")[0]
    assert (btc["interval_seconds"], btc["interval_rate"], btc["annualized_rate"]) == (IV8, "0.0001", "0.1095")


def test_estimated_run2_same_window_one_rate_changed_is_exactly_one_change_row():
    doc = load_doc()
    sim = Simulator(ST_ESTIMATED)
    sim.run(real_perps(doc), RUN1)
    doc2 = set_rate(copy.deepcopy(doc), "ETH-PERP", "0.00007")
    r2 = sim.run(real_perps(doc2), RUN2)
    assert len(r2.obs_rows) == 1
    c = r2.obs_rows[0]
    assert (c["kind"], c["ticker"], c["obs_ts"], c["interval_rate"], int(c["next_timestamp"])) == (KIND_CHANGE, "ETH-PERP", RUN2, "0.00007", T0)
    assert r2.funding_rows == [] and r2.anomalies == []
    assert r2.n_new == 0 and r2.n_rollover == 0 and r2.n_checked == 0


def test_estimated_run3_rollover_writes_last_first_and_estimated_row():
    doc = load_doc()
    sim = Simulator(ST_ESTIMATED)
    sim.run(real_perps(doc), RUN1)
    doc2 = set_rate(copy.deepcopy(doc), "ETH-PERP", "0.00007")
    sim.run(real_perps(doc2), RUN2)
    doc3 = advance(copy.deepcopy(doc2), 1)
    r3 = sim.run(real_perps(doc3), RUN3)
    nf = n_funded(real_perps(doc3))
    assert r3.n_rollover == nf and r3.n_new == 0

    btc = of(r3.obs_rows, "BTC-PERP")
    assert kinds(btc) == [KIND_LAST, KIND_FIRST]
    last, first = btc
    assert last["obs_ts"] == RUN2                      # 直前の取得時刻
    assert last["interval_rate"] == "0.0001" and last["annualized_rate"] == "0.1095"
    assert int(last["next_timestamp"]) == T0 and last["interval_seconds"] == IV8
    assert first["obs_ts"] == RUN3 and int(first["next_timestamp"]) == T0 + IV8

    fb = of(r3.funding_rows, "BTC-PERP")
    assert len(fb) == 1
    e = fb[0]
    assert e["status"] == ST_ESTIMATED
    assert e["pay_ts"] == T0                           # 旧 next_timestamp
    assert e["interval_rate"] == "0.0001" and e["annualized_rate"] == "0.1095"
    assert e["basis_obs_ts"] == RUN2                   # 直前の取得時刻
    assert e["interval_seconds"] == IV8

    # ETH は run2 の C 行が最後の観測なので L 行は書かず、推定値は変更後の値、根拠は C 行の時刻
    eth = of(r3.obs_rows, "ETH-PERP")
    assert kinds(eth) == [KIND_FIRST]
    fe = of(r3.funding_rows, "ETH-PERP")[0]
    assert fe["interval_rate"] == "0.00007" and fe["basis_obs_ts"] == RUN2 and fe["status"] == ST_ESTIMATED

    # 1 時間足(ORDER)も窓の中で観測できているので推定値
    fo = of(r3.funding_rows, "ORDER-PERP")[0]
    assert fo["status"] == ST_ESTIMATED and fo["interval_seconds"] == IV1 and fo["interval_rate"] == "-0.002854"

    assert len(r3.funding_rows) == nf
    assert {r["status"] for r in r3.funding_rows} == {ST_ESTIMATED}
    assert all(r["pay_ts"] == T0 for r in r3.funding_rows)
    assert len([r for r in r3.obs_rows if r["kind"] == KIND_LAST]) == nf - 1
    assert len([r for r in r3.obs_rows if r["kind"] == KIND_FIRST]) == nf
    # F から直前の取得まで 12 分: 25 分未満なので検査も矛盾も無い
    assert r3.anomalies == [] and r3.n_checked == 0


def test_estimated_gap_of_three_intervals_emits_missing_rows_never_zero():
    sim = Simulator(ST_ESTIMATED)
    p = perp("ETH-PERP", IV8, T0, "0.00006")
    sim.run([p], RUN1)
    sim.run([p], RUN2)
    r3 = sim.run([perp("ETH-PERP", IV8, T0 + 3 * IV8, "0.00005")], "2026-10-26T00:09:00Z")
    rows = r3.funding_rows
    assert [(r["pay_ts"], r["status"]) for r in rows] == [(T0, ST_ESTIMATED), (T0 + IV8, ST_MISSING), (T0 + 2 * IV8, ST_MISSING)]
    assert rows[0]["interval_rate"] == "0.00006" and rows[0]["basis_obs_ts"] == RUN2
    for m in rows[1:]:
        assert m["interval_rate"] is None and m["annualized_rate"] is None and m["basis_obs_ts"] is None
        assert m["interval_seconds"] == IV8 and m["ticker"] == "ETH-PERP"
    assert not any(r["interval_rate"] in (0, 0.0, "0", "0.0") for r in rows)
    assert kinds(r3.obs_rows) == [KIND_LAST, KIND_FIRST]


def test_estimated_old_window_is_missing_when_last_seen_is_before_window_start():
    # 旧窓 T0(開始 16:00)の中で一度も観測できなかった: 直前の観測も直前の取得も 16:00 より前
    prev = obs_row("2026-10-24T14:00:00Z", "ETH-PERP", IV8, T0, "0.00006")
    r = process_run([perp("ETH-PERP", IV8, T0 + IV8, "0.00005")], RUN3, {"ETH-PERP": prev}, {}, "2026-10-24T15:30:00Z", {}, ST_ESTIMATED)
    assert [(x["pay_ts"], x["status"], x["interval_rate"], x["basis_obs_ts"]) for x in r.funding_rows] == [(T0, ST_MISSING, None, None)]
    assert kinds(r.obs_rows) == [KIND_LAST, KIND_FIRST]
    assert r.obs_rows[0]["obs_ts"] == "2026-10-24T15:30:00Z"


def test_estimated_observation_exactly_at_window_start_counts_as_missing():
    prev = obs_row("2026-10-24T16:00:00Z", "ETH-PERP", IV8, T0, "0.00006")
    r = process_run([perp("ETH-PERP", IV8, T0 + IV8, "0.00005")], RUN3, {"ETH-PERP": prev}, {}, None, {}, ST_ESTIMATED)
    assert r.funding_rows[0]["status"] == ST_MISSING
    prev2 = obs_row("2026-10-24T16:00:01Z", "ETH-PERP", IV8, T0, "0.00006")
    r2 = process_run([perp("ETH-PERP", IV8, T0 + IV8, "0.00005")], RUN3, {"ETH-PERP": prev2}, {}, None, {}, ST_ESTIMATED)
    assert r2.funding_rows[0]["status"] == ST_ESTIMATED and r2.funding_rows[0]["basis_obs_ts"] == "2026-10-24T16:00:01Z"


def test_estimated_does_not_rewrite_pay_ts_already_recorded():
    prev = obs_row(RUN2, "BTC-PERP", IV8, T0, "0.0001")
    r = process_run([perp("BTC-PERP", IV8, T0 + IV8)], RUN3, {"BTC-PERP": prev}, {}, RUN2, {"BTC-PERP": T0}, ST_ESTIMATED)
    assert r.funding_rows == []
    assert kinds(r.obs_rows) == [KIND_FIRST]      # prev_fetch_ts == obs_ts なので L 行は無い


def test_estimated_fills_gap_before_old_window_from_last_pay():
    # 最後に記録した pay_ts が 2 窓前なら、その間も missing で埋める
    prev = obs_row(RUN2, "BTC-PERP", IV8, T0, "0.0001")
    r = process_run([perp("BTC-PERP", IV8, T0 + IV8)], RUN3, {"BTC-PERP": prev}, {}, RUN2, {"BTC-PERP": T0 - 3 * IV8}, ST_ESTIMATED)
    assert [(x["pay_ts"], x["status"]) for x in r.funding_rows] == [(T0 - 2 * IV8, ST_MISSING), (T0 - IV8, ST_MISSING), (T0, ST_ESTIMATED)]


# ---------------------------------------------------------------- confirmed

def test_confirmed_first_observation_yields_confirmed_row_immediately():
    doc = load_doc()
    perps = real_perps(doc)
    r1 = Simulator(ST_CONFIRMED).run(perps, RUN1)
    nf = n_funded(perps)
    assert len(r1.obs_rows) == nf and set(kinds(r1.obs_rows)) == {KIND_FIRST}
    assert len(r1.funding_rows) == nf
    assert {r["status"] for r in r1.funding_rows} == {ST_CONFIRMED}
    assert all(r["basis_obs_ts"] == RUN1 for r in r1.funding_rows)
    btc = of(r1.funding_rows, "BTC-PERP")[0]
    assert btc["pay_ts"] == T0 - IV8 and btc["interval_rate"] == "0.0001" and btc["annualized_rate"] == "0.1095"
    order = of(r1.funding_rows, "ORDER-PERP")[0]
    assert order["pay_ts"] == T0 - IV1 and order["interval_rate"] == "-0.002854"
    xag = of(r1.funding_rows, "XAG-PERP")[0]
    assert xag["pay_ts"] == T0 - IV4
    assert r1.anomalies == [] and r1.n_checked == 0


def test_confirmed_change_inside_window_is_change_row_and_anomaly():
    doc = load_doc()
    sim = Simulator(ST_CONFIRMED)
    perps1 = real_perps(doc)
    sim.run(perps1, RUN1)
    doc2 = set_rate(copy.deepcopy(doc), "ETH-PERP", "0.00007")
    r2 = sim.run(real_perps(doc2), RUN2)
    assert kinds(r2.obs_rows) == [KIND_CHANGE] and r2.obs_rows[0]["ticker"] == "ETH-PERP"
    assert r2.anomalies == [f"ETH-PERP: rate changed inside window {T0} (0.00006226->0.00007)"]
    assert r2.n_contradictions == 1
    assert r2.funding_rows == []
    # 確定値型では同じ窓を観測した 0 でも金利成分でもない銘柄すべてが「検査」
    non_trivial = [p for p in perps1 if p.has_funding and not is_trivial_rate(p.interval_rate, p.interval_seconds)]
    assert r2.n_checked == len(non_trivial) == 5


def test_confirmed_rollover_yields_next_confirmed_row():
    doc = load_doc()
    sim = Simulator(ST_CONFIRMED)
    sim.run(real_perps(doc), RUN1)
    doc2 = set_rate(copy.deepcopy(doc), "ETH-PERP", "0.00007")
    sim.run(real_perps(doc2), RUN2)
    doc3 = advance(copy.deepcopy(doc2), 1)
    r3 = sim.run(real_perps(doc3), RUN3)
    nf = n_funded(real_perps(doc3))
    assert len(r3.funding_rows) == nf and {r["status"] for r in r3.funding_rows} == {ST_CONFIRMED}
    assert all(r["pay_ts"] == T0 and r["basis_obs_ts"] == RUN3 for r in r3.funding_rows)   # pay_ts = 新 next − interval = 旧 next
    assert of(r3.funding_rows, "BTC-PERP")[0]["interval_rate"] == "0.0001"
    assert of(r3.funding_rows, "ETH-PERP")[0]["interval_rate"] == "0.00007"
    assert kinds(r3.obs_rows, "BTC-PERP") == [KIND_LAST, KIND_FIRST]
    assert kinds(r3.obs_rows, "ETH-PERP") == [KIND_FIRST]
    assert r3.anomalies == []
    # 全銘柄で pay_ts が単調に増えている
    for t in {r["ticker"] for r in sim.funding_rows}:
        pays = [r["pay_ts"] for r in of(sim.funding_rows, t)]
        assert pays == sorted(pays) and len(set(pays)) == len(pays)


def test_confirmed_gap_produces_missing_rows():
    sim = Simulator(ST_CONFIRMED)
    sim.run([perp("ETH-PERP", IV8, T0, "0.00006")], RUN1)
    r = sim.run([perp("ETH-PERP", IV8, T0 + 3 * IV8, "0.00005")], "2026-10-26T00:09:00Z")
    assert [(x["pay_ts"], x["status"], x["interval_rate"]) for x in r.funding_rows] == \
        [(T0, ST_MISSING, None), (T0 + IV8, ST_MISSING, None), (T0 + 2 * IV8, ST_CONFIRMED, "0.00005")]
    assert r.funding_rows[-1]["basis_obs_ts"] == "2026-10-26T00:09:00Z"


def test_confirmed_new_ticker_with_old_last_pay_fills_gap():
    r = process_run([perp("BTC-PERP", IV8, T0, "0.0001")], RUN1, {}, {}, None, {"BTC-PERP": T0 - 3 * IV8}, ST_CONFIRMED)
    assert [(x["pay_ts"], x["status"]) for x in r.funding_rows] == [(T0 - 2 * IV8, ST_MISSING), (T0 - IV8, ST_CONFIRMED)]
    assert r.n_new == 1


def test_confirmed_new_ticker_does_not_duplicate_recorded_pay():
    r = process_run([perp("BTC-PERP", IV8, T0, "0.0001")], RUN1, {}, {}, None, {"BTC-PERP": T0 - IV8}, ST_CONFIRMED)
    assert r.funding_rows == [] and kinds(r.obs_rows) == [KIND_FIRST]


# ---------------------------------------------------------------- 間隔の変更 / 欠測の刻み

def test_interval_change_across_rollover_estimated_uses_new_interval_for_gap():
    sim = Simulator(ST_ESTIMATED)
    sim.run([perp("XAG-PERP", IV4, T0, "0.00005")], RUN1)
    sim.run([perp("XAG-PERP", IV4, T0, "0.00005")], RUN2)
    r = sim.run([perp("XAG-PERP", IV8, T0 + 3 * IV8, "0.0001")], "2026-10-26T00:09:00Z")
    assert [(x["pay_ts"], x["status"], x["interval_seconds"]) for x in r.funding_rows] == \
        [(T0, ST_ESTIMATED, IV4), (T0 + IV8, ST_MISSING, IV8), (T0 + 2 * IV8, ST_MISSING, IV8)]
    assert r.funding_rows[0]["interval_rate"] == "0.00005"
    last, first = r.obs_rows
    assert (last["kind"], last["interval_seconds"]) == (KIND_LAST, IV4)
    assert (first["kind"], first["interval_seconds"]) == (KIND_FIRST, IV8)
    assert r.anomalies == []


def test_interval_change_across_rollover_confirmed_aligns_missing_to_new_pay():
    sim = Simulator(ST_CONFIRMED)
    sim.run([perp("XAG-PERP", IV4, T0, "0.00005")], RUN1)          # confirmed T0 − 4h
    r = sim.run([perp("XAG-PERP", IV8, T0 + 3 * IV8, "0.0001")], "2026-10-26T00:09:00Z")
    # pay = T0 + 2×8h。旧窓の支払 T0 までは旧間隔 4h、その後は新間隔 8h で刻む
    assert [(x["pay_ts"], x["status"], x["interval_seconds"]) for x in r.funding_rows] == \
        [(T0, ST_MISSING, IV4), (T0 + IV8, ST_MISSING, IV8), (T0 + 2 * IV8, ST_CONFIRMED, IV8)]
    assert {_key(x) for x in rebuild_from_obs(sim.obs_rows, ST_CONFIRMED)} == {_key(x) for x in sim.funding_rows}


def test_interval_change_inside_window_is_change_row_and_anomaly():
    prev = obs_row(RUN1, "XAG-PERP", IV4, T0, "0.00005")
    for mode in (ST_ESTIMATED, ST_CONFIRMED):
        r = process_run([perp("XAG-PERP", IV8, T0, "0.00005")], RUN2, {"XAG-PERP": prev}, {}, RUN1, {}, mode)
        assert kinds(r.obs_rows) == [KIND_CHANGE] and r.obs_rows[0]["interval_seconds"] == IV8
        assert r.anomalies == [f"XAG-PERP: interval {IV4}->{IV8} inside window {T0}"]
        assert r.n_contradictions == 0                      # 判定(funding_mode)とは無関係の異常
        assert r.funding_rows == []


def test_missing_rows_align_to_before_pay_ts_minus_k_step():
    rows = missing_rows("X", 100, 1000, 300)
    assert [r["pay_ts"] for r in rows] == [400, 700]                      # 1000 − 2×300, 1000 − 300
    assert all(r["status"] == ST_MISSING and r["interval_rate"] is None and r["annualized_rate"] is None
               and r["basis_obs_ts"] is None and r["interval_seconds"] == 300 and r["ticker"] == "X" for r in rows)
    assert missing_rows("X", 100, 1000, 450) and [r["pay_ts"] for r in missing_rows("X", 100, 1000, 450)] == [550]
    assert missing_rows("X", None, 1000, 300) == []     # 初めて見る銘柄
    assert missing_rows("X", 100, 1000, 0) == []
    assert missing_rows("X", 700, 1000, 300) == []      # 隣の窓
    assert missing_rows("X", 1000, 1000, 300) == []
    assert missing_rows("X", 1300, 1000, 300) == []     # 逆転
    assert [r["pay_ts"] for r in missing_rows("X", 99, 1000, 300)] == [100, 400, 700]


# ---------------------------------------------------------------- その他の分岐

def test_same_window_same_rate_writes_nothing_in_both_modes():
    prev = obs_row(RUN1, "ONG-PERP", IV4, T0, "0.000050")
    for mode in (ST_ESTIMATED, ST_CONFIRMED):
        r = process_run([perp("ONG-PERP", IV4, T0, "0.00005000")], RUN2, {"ONG-PERP": prev}, {}, RUN1, {}, mode)
        assert r.obs_rows == [] and r.funding_rows == [] and r.anomalies == []


def test_next_timestamp_going_backwards_restarts_window_without_funding():
    prev = obs_row(RUN1, "BTC-PERP", IV8, T0 + IV8, "0.0001")
    for mode in (ST_ESTIMATED, ST_CONFIRMED):
        r = process_run([perp("BTC-PERP", IV8, T0)], RUN2, {"BTC-PERP": prev}, {}, RUN1, {}, mode)
        # 巻き戻った値は記録しない。この回は不在扱い(prev の行が直前の取得そのものなので L 行も要らない)
        assert r.obs_rows == []
        assert r.absent == ["BTC-PERP"]
        assert r.funding_rows == [] and r.n_rollover == 0
        assert r.anomalies == [f"BTC-PERP: next_timestamp went backwards {T0 + IV8}->{T0}"]
        assert r.n_contradictions == 0


def test_perps_without_funding_are_ignored():
    p = Perp("NVDA-PERP", "NVIDIA", "1", "1", "1", "1", "1")
    for mode in (ST_ESTIMATED, ST_CONFIRMED):
        r = process_run([p], RUN1, {}, {}, None, {}, mode)
        assert r.obs_rows == [] and r.funding_rows == [] and r.n_new == 0


def test_unknown_mode_raises():
    with pytest.raises(ValueError):
        process_run([], RUN1, {}, {}, None, {}, "guess")
    with pytest.raises(ValueError):
        rebuild_from_obs([], "guess")


def test_rate_equal_compares_numerically():
    assert rate_equal("0.000050", "0.00005000") is True
    assert rate_equal("0", "0.0") is True
    assert rate_equal("-0.002854", "-0.002854000") is True
    assert rate_equal("0.0001", "0.00012") is False
    assert rate_equal(None, None) is True
    assert rate_equal(None, "0") is False and rate_equal("0", None) is False
    assert rate_equal("abc", "abc") is True and rate_equal("abc", "0") is False


@pytest.mark.parametrize("rate,iv,expected", [
    ("0.0001", 28800, True), ("0.00005", 14400, True), ("0.000050", 14400, True), ("0.0000125", 3600, True),
    ("0.00001250", 3600, True), ("0", 28800, True), ("0", 14400, True), ("0.0", 3600, True), (None, 28800, True),
    ("0.00012", 28800, False), ("0.0001", 14400, False), ("-0.0001", 28800, False), ("0.00006226", 28800, False),
    ("0.000101", 28800, False), ("abc", 28800, False),
])
def test_is_trivial_rate(rate, iv, expected):
    assert is_trivial_rate(rate, iv) is expected


# ---------------------------------------------------------------- 見込み値型の矛盾判定(検査した窓 / 動かなかった窓)

def test_estimated_flat_non_trivial_window_is_checked_and_anomalous():
    prev = obs_row("2026-10-24T23:00:00Z", "ETH-PERP", IV8, T0, "0.00006226")      # F から直前の取得まで 31.5 分
    r = process_run([perp("ETH-PERP", IV8, T0 + IV8, "0.00006")], RUN3, {"ETH-PERP": prev}, {}, RUN2, {}, ST_ESTIMATED)
    assert r.n_checked == 1 and r.n_contradictions == 1
    assert r.anomalies == [f"ETH-PERP: rate never moved inside window {T0} (0.00006226)"]
    assert r.funding_rows[0]["status"] == ST_ESTIMATED      # 矛盾でも推定値は記録する


def test_estimated_window_with_one_change_row_is_checked_but_not_anomalous():
    # F 23:00 → C 23:20 → 直前の取得 23:31:30。prev は C 行なので窓の最初の時刻は prev_first_ts で渡す
    prev = obs_row("2026-10-24T23:20:00Z", "ETH-PERP", IV8, T0, "0.00007", KIND_CHANGE)
    r = process_run([perp("ETH-PERP", IV8, T0 + IV8, "0.00006")], RUN3, {"ETH-PERP": prev}, {"ETH-PERP": 1}, RUN2, {}, ST_ESTIMATED,
                    prev_first_ts={"ETH-PERP": "2026-10-24T23:00:00Z"})
    assert r.n_checked == 1 and r.anomalies == [] and r.n_contradictions == 0
    assert r.funding_rows[0]["interval_rate"] == "0.00007" and r.funding_rows[0]["basis_obs_ts"] == RUN2


def test_estimated_change_row_without_first_ts_is_not_checked():
    # 窓の最初の時刻が分からない C 行(保持期間の外など)は検査に数えない
    prev = obs_row("2026-10-24T23:20:00Z", "ETH-PERP", IV8, T0, "0.00007", KIND_CHANGE)
    r = process_run([perp("ETH-PERP", IV8, T0 + IV8, "0.00006")], RUN3, {"ETH-PERP": prev}, {"ETH-PERP": 1}, RUN2, {}, ST_ESTIMATED)
    assert r.n_checked == 0 and r.anomalies == []


def test_estimated_trivial_rate_window_is_not_checked():
    for rate, iv in (("0.0001", IV8), ("0", IV4), ("0.000050", IV4), ("0.0000125", IV1)):
        prev = obs_row("2026-10-24T22:00:00Z", "BTC-PERP", iv, T0, rate)
        r = process_run([perp("BTC-PERP", iv, T0 + iv, rate)], RUN3, {"BTC-PERP": prev}, {}, RUN2, {}, ST_ESTIMATED)
        assert r.n_checked == 0 and r.anomalies == [], rate


def test_estimated_short_observation_is_not_checked():
    prev = obs_row("2026-10-24T23:10:00Z", "ETH-PERP", IV8, T0, "0.00006226")      # 21.5 分
    r = process_run([perp("ETH-PERP", IV8, T0 + IV8, "0.00006")], RUN3, {"ETH-PERP": prev}, {}, RUN2, {}, ST_ESTIMATED)
    assert r.n_checked == 0 and r.anomalies == []


def test_estimated_stall_threshold_is_inclusive_25_minutes():
    assert STALL_SECONDS == 25 * 60
    prev = obs_row("2026-10-24T23:06:30Z", "ETH-PERP", IV8, T0, "0.00006226")      # ちょうど 25 分
    r = process_run([perp("ETH-PERP", IV8, T0 + IV8, "0.00006")], RUN3, {"ETH-PERP": prev}, {}, RUN2, {}, ST_ESTIMATED)
    assert r.n_checked == 1 and len(r.anomalies) == 1
    prev = obs_row("2026-10-24T23:06:31Z", "ETH-PERP", IV8, T0, "0.00006226")      # 1 秒足りない
    r = process_run([perp("ETH-PERP", IV8, T0 + IV8, "0.00006")], RUN3, {"ETH-PERP": prev}, {}, RUN2, {}, ST_ESTIMATED)
    assert r.n_checked == 0 and r.anomalies == []


def test_estimated_anomaly_through_simulator_sequence():
    # ETH は動かず、SOL(金利成分)は対象外。F 23:00 → 同じ値 23:31:30 → 窓が進む
    sim = Simulator(ST_ESTIMATED)
    perps = [perp("ETH-PERP", IV8, T0, "0.00006226"), perp("SOL-PERP", IV8, T0, "0.0001"), perp("PUFFER-PERP", IV8, T0, "-0.00001976")]
    sim.run(perps, "2026-10-24T23:00:00Z")
    sim.run([perps[0], perps[1], perp("PUFFER-PERP", IV8, T0, "-0.00002")], "2026-10-24T23:10:00Z")   # PUFFER だけ C 行
    sim.run(perps, RUN2)
    r = sim.run([perp("ETH-PERP", IV8, T0 + IV8, "0.00006"), perp("SOL-PERP", IV8, T0 + IV8, "0.0001"),
                 perp("PUFFER-PERP", IV8, T0 + IV8, "-0.00002")], RUN3)
    assert r.n_checked == 2                                 # ETH と PUFFER(どちらも 25 分以上観測)
    assert r.anomalies == [f"ETH-PERP: rate never moved inside window {T0} (0.00006226)"]
    assert of(r.funding_rows, "PUFFER-PERP")[0]["interval_rate"] == "-0.00001976"   # 旧窓の最後の値(23:31:30 に見た値)


# ---------------------------------------------------------------- rebuild_from_obs

def _four_runs(mode):
    """F → C → 窓の切り替わり → 丸 1 日以上の欠測、の 4 回。"""
    doc = load_doc()
    sim = Simulator(mode)
    sim.run(real_perps(doc), RUN1)
    doc2 = set_rate(copy.deepcopy(doc), "ETH-PERP", "0.00007")
    sim.run(real_perps(doc2), RUN2)
    doc3 = advance(copy.deepcopy(doc2), 1)
    sim.run(real_perps(doc3), RUN3)
    t4 = "2026-10-26T00:10:00Z"
    doc4 = advance_to(set_rate(copy.deepcopy(doc3), "BTC-PERP", "0.00013"), to_unix(parse_iso(t4)))
    sim.run(real_perps(doc4), t4)
    return sim


def _key(r):
    return (int(r["pay_ts"]), r["ticker"], r["status"], r["interval_rate"])


@pytest.mark.parametrize("mode", [ST_ESTIMATED, ST_CONFIRMED])
def test_rebuild_from_obs_matches_live_funding_rows(mode):
    sim = _four_runs(mode)
    live = sim.funding_rows
    assert any(r["status"] == ST_MISSING for r in live)              # 欠測がある
    assert sum(r.n_rollover for r in sim.results) > 0
    rebuilt = rebuild_from_obs(sim.obs_rows, mode)
    assert {_key(r) for r in rebuilt} == {_key(r) for r in live}
    assert len(rebuilt) == len(live)                                  # 重複も無い
    assert len({_key(r) for r in live}) == len(live)
    # 根拠の時刻と間隔も一致する
    full = lambda r: (int(r["pay_ts"]), r["ticker"], r["status"], r["interval_rate"], r["annualized_rate"], int(r["interval_seconds"]), r["basis_obs_ts"])
    assert {full(r) for r in rebuilt} == {full(r) for r in live}
    # 出力は (pay_ts, ticker) 順
    assert [(r["pay_ts"], r["ticker"]) for r in rebuilt] == sorted((r["pay_ts"], r["ticker"]) for r in rebuilt)


def test_rebuild_from_obs_accepts_csv_string_rows():
    sim = _four_runs(ST_ESTIMATED)
    as_csv = [{k: ("" if v is None else str(v)) for k, v in r.items()} for r in sim.obs_rows]
    rebuilt = rebuild_from_obs(as_csv, ST_ESTIMATED)
    assert {_key(r) for r in rebuilt} == {_key(r) for r in sim.funding_rows}


def test_rebuild_estimated_skips_open_window_and_confirmed_includes_it():
    rows = [
        {"obs_ts": RUN1, "ticker": "BTC-PERP", "interval_seconds": IV8, "next_timestamp": T0, "interval_rate": "0.0001", "annualized_rate": "0.1095", "kind": "F"},
        {"obs_ts": RUN2, "ticker": "BTC-PERP", "interval_seconds": IV8, "next_timestamp": T0, "interval_rate": "0.00011", "annualized_rate": "", "kind": "C"},
        {"obs_ts": RUN3, "ticker": "BTC-PERP", "interval_seconds": IV8, "next_timestamp": T0 + IV8, "interval_rate": "0.00012", "annualized_rate": "", "kind": "F"},
    ]
    est = rebuild_from_obs(rows, ST_ESTIMATED)
    assert [(r["pay_ts"], r["status"], r["interval_rate"], r["basis_obs_ts"]) for r in est] == [(T0, ST_ESTIMATED, "0.00011", RUN2)]
    con = rebuild_from_obs(rows, ST_CONFIRMED)
    assert [(r["pay_ts"], r["status"], r["interval_rate"], r["basis_obs_ts"]) for r in con] == \
        [(T0 - IV8, ST_CONFIRMED, "0.0001", RUN1), (T0, ST_CONFIRMED, "0.00012", RUN3)]
    assert con[0]["annualized_rate"] == "0.1095" and con[1]["annualized_rate"] is None


def test_rebuild_estimated_marks_window_missing_when_only_seen_before_it_started():
    rows = [
        {"obs_ts": "2026-10-24T14:00:00Z", "ticker": "X-PERP", "interval_seconds": IV8, "next_timestamp": T0, "interval_rate": "0.0002", "annualized_rate": "", "kind": "F"},
        {"obs_ts": RUN3, "ticker": "X-PERP", "interval_seconds": IV8, "next_timestamp": T0 + IV8, "interval_rate": "0.0002", "annualized_rate": "", "kind": "F"},
    ]
    assert [(r["pay_ts"], r["status"], r["interval_rate"]) for r in rebuild_from_obs(rows, ST_ESTIMATED)] == [(T0, ST_MISSING, None)]


def test_rebuild_empty_input():
    assert rebuild_from_obs([], ST_ESTIMATED) == [] and rebuild_from_obs([], ST_CONFIRMED) == []


# ---------------------------------------------------------------- 不在だった銘柄(absent)と「最後に見た時刻」

def _absent_sequence(mode, first_obs_ts="2026-10-24T20:08:00Z", seen_again=True):
    """4h 銘柄 X: first_obs_ts に F(窓 20:00–00:00、nt = T0)。seen_again なら 20:18 にも居て同じ値。
    その後 20:28 から何時間も居ない。翌朝 08:08 に nt = T0 + 8h(窓 04:00–08:00)で戻る。"""
    sim = Simulator(mode)
    x = perp("X-PERP", IV4, T0, "0.00012")                        # 0 でも金利成分でもない
    sim.run([x], first_obs_ts)
    if seen_again:
        sim.run([x], "2026-10-24T20:18:00Z")
    gone = sim.run([], "2026-10-24T20:28:00Z")
    for ts in ("2026-10-24T23:58:00Z", "2026-10-25T03:58:00Z", "2026-10-25T07:58:00Z"):
        later = sim.run([], ts)
        assert later.absent == ["X-PERP"] and later.obs_rows == [] and later.funding_rows == []
    back = sim.run([perp("X-PERP", IV4, T0 + 2 * IV4, "0.00013")], "2026-10-25T08:08:00Z")
    return sim, gone, back


def test_absent_ticker_gets_last_row_at_the_fetch_it_was_last_seen():
    sim, gone, _ = _absent_sequence(ST_ESTIMATED)
    assert gone.absent == ["X-PERP"] and gone.funding_rows == []
    assert [(r["kind"], r["obs_ts"], int(r["next_timestamp"]), r["interval_rate"]) for r in gone.obs_rows] == \
        [(KIND_LAST, "2026-10-24T20:18:00Z", T0, "0.00012")]
    assert sim.absent == []                                       # 戻った後は不在ではない


def test_returning_absent_ticker_estimated_uses_real_last_observation_not_global_prev_fetch():
    sim, _, back = _absent_sequence(ST_ESTIMATED)
    assert back.absent == [] and back.n_rollover == 1
    assert kinds(back.obs_rows) == [KIND_FIRST]                   # 07:58 の偽の L 行は作らない
    assert back.obs_rows[0]["obs_ts"] == "2026-10-25T08:08:00Z"
    assert [(r["pay_ts"], r["status"], r["interval_rate"], r["basis_obs_ts"], r["interval_seconds"]) for r in back.funding_rows] == \
        [(T0, ST_ESTIMATED, "0.00012", "2026-10-24T20:18:00Z", IV4), (T0 + IV4, ST_MISSING, None, None, IV4)]
    assert back.anomalies == [] and back.n_checked == 0 and back.n_contradictions == 0      # 20:08→20:18 の 10 分では検査しない
    assert {_key(r) for r in rebuild_from_obs(sim.obs_rows, ST_ESTIMATED)} == {_key(r) for r in sim.funding_rows}


def test_returning_absent_ticker_never_seen_twice_uses_first_row_as_basis():
    sim, gone, back = _absent_sequence(ST_ESTIMATED, seen_again=False)
    assert gone.obs_rows == []                                    # F 行の直後に消えた: prev_fetch_ts == obs_ts なので L 行なし
    assert back.funding_rows[0]["status"] == ST_ESTIMATED and back.funding_rows[0]["basis_obs_ts"] == "2026-10-24T20:08:00Z"
    assert back.anomalies == [] and back.n_checked == 0


def test_returning_absent_ticker_with_only_observation_before_window_start_is_missing():
    # 19:59 の F 行(API が窓を 1 分早く出した。許容 120 秒以内)。窓 20:00 の中では一度も見ていない
    sim, _, back = _absent_sequence(ST_ESTIMATED, first_obs_ts="2026-10-24T19:59:00Z", seen_again=False)
    assert [(r["pay_ts"], r["status"], r["interval_rate"]) for r in back.funding_rows] == [(T0, ST_MISSING, None), (T0 + IV4, ST_MISSING, None)]
    assert back.anomalies == [] and back.n_checked == 0
    assert {_key(r) for r in rebuild_from_obs(sim.obs_rows, ST_ESTIMATED)} == {_key(r) for r in sim.funding_rows}


def test_returning_absent_ticker_confirmed_fills_gap_and_confirms_new_window():
    sim, gone, back = _absent_sequence(ST_CONFIRMED)
    assert [(r["pay_ts"], r["status"]) for r in sim.results[0].funding_rows] == [(T0 - IV4, ST_CONFIRMED)]
    assert kinds(gone.obs_rows) == [KIND_LAST]
    assert [(r["pay_ts"], r["status"], r["interval_rate"], r["basis_obs_ts"]) for r in back.funding_rows] == \
        [(T0, ST_MISSING, None, None), (T0 + IV4, ST_CONFIRMED, "0.00013", "2026-10-25T08:08:00Z")]
    assert back.anomalies == [] and back.n_checked == 0
    assert {_key(r) for r in rebuild_from_obs(sim.obs_rows, ST_CONFIRMED)} == {_key(r) for r in sim.funding_rows}


def test_absent_tickers_are_sorted_and_get_one_last_row_only():
    prev = {"B-PERP": obs_row(RUN1, "B-PERP", IV8, T0, "0.0001"), "A-PERP": obs_row(RUN1, "A-PERP", IV8, T0, "0.0002")}
    r = process_run([], RUN3, prev, {}, RUN2, {}, ST_ESTIMATED)
    assert r.absent == ["A-PERP", "B-PERP"]
    assert sorted((x["ticker"], x["kind"], x["obs_ts"]) for x in r.obs_rows) == [("A-PERP", KIND_LAST, RUN2), ("B-PERP", KIND_LAST, RUN2)]
    assert r.funding_rows == [] and r.anomalies == []
    # 2 回目以降(absent_prev に居る)は L 行を書かない
    prev2 = {t: obs_row(RUN2, t, IV8, T0, p["interval_rate"], KIND_LAST) for t, p in prev.items()}
    r2 = process_run([], "2026-10-25T00:19:00Z", prev2, {}, RUN3, {}, ST_ESTIMATED, absent_prev=["A-PERP", "B-PERP"])
    assert r2.absent == ["A-PERP", "B-PERP"] and r2.obs_rows == []
    # 直前の取得時刻が無い(初回)なら L 行は書けない
    r3 = process_run([], RUN3, prev, {}, None, {}, ST_ESTIMATED)
    assert r3.absent == ["A-PERP", "B-PERP"] and r3.obs_rows == []


def test_absent_prev_ticker_present_in_same_window_writes_nothing_extra():
    prev = obs_row(RUN1, "X-PERP", IV8, T0, "0.0001")
    r = process_run([perp("X-PERP", IV8, T0, "0.0001")], RUN3, {"X-PERP": prev}, {}, RUN2, {}, ST_ESTIMATED, absent_prev=["X-PERP"])
    assert r.obs_rows == [] and r.funding_rows == [] and r.absent == []


# ---------------------------------------------------------------- 未来の窓(時計より先の next_timestamp)

def test_future_window_guard_boundary_is_120_seconds():
    fetch_unix = to_unix(parse_iso(RUN1))
    ok = process_run([perp("X-PERP", IV8, fetch_unix + CLOCK_SKEW_SECONDS + IV8, "0.0001")], RUN1, {}, {}, None, {}, ST_ESTIMATED)
    assert kinds(ok.obs_rows) == [KIND_FIRST] and ok.n_new == 1 and ok.anomalies == []
    bad = process_run([perp("X-PERP", IV8, fetch_unix + CLOCK_SKEW_SECONDS + 1 + IV8, "0.0001")], RUN1, {}, {}, None, {}, ST_CONFIRMED)
    assert bad.obs_rows == [] and bad.funding_rows == [] and bad.n_new == 0 and bad.absent == []
    assert len(bad.anomalies) == 1 and "ahead of clock" in bad.anomalies[0] and bad.n_contradictions == 0


@pytest.mark.parametrize("mode", [ST_ESTIMATED, ST_CONFIRMED])
def test_one_off_future_window_glitch_fabricates_nothing(mode):
    sim = Simulator(mode)
    sim.run([perp("BTC-PERP", IV8, T0, "0.0001")], RUN1)
    glitch = sim.run([perp("BTC-PERP", IV8, T0 + 2 * 86400, "0.0001")], RUN2)          # next_timestamp が 2 日先に飛んだ
    assert glitch.obs_rows == [] and glitch.funding_rows == [] and glitch.absent == ["BTC-PERP"]
    assert len(glitch.anomalies) == 1 and "ahead of clock" in glitch.anomalies[0]
    assert glitch.n_rollover == 0 and glitch.n_contradictions == 0
    normal = sim.run([perp("BTC-PERP", IV8, T0, "0.0001")], "2026-10-24T23:41:30Z")   # 元に戻る(同じ窓・同じ値)
    assert normal.obs_rows == [] and normal.funding_rows == [] and normal.anomalies == [] and normal.absent == []
    roll = sim.run([perp("BTC-PERP", IV8, T0 + IV8, "0.00011")], RUN3)
    if mode == ST_ESTIMATED:
        assert [(r["pay_ts"], r["status"], r["interval_rate"], r["basis_obs_ts"]) for r in roll.funding_rows] == \
            [(T0, ST_ESTIMATED, "0.0001", "2026-10-24T23:41:30Z")]
        assert [(r["kind"], r["obs_ts"]) for r in roll.obs_rows] == [(KIND_LAST, "2026-10-24T23:41:30Z"), (KIND_FIRST, RUN3)]
    else:
        assert [(r["pay_ts"], r["status"], r["interval_rate"], r["basis_obs_ts"]) for r in roll.funding_rows] == \
            [(T0, ST_CONFIRMED, "0.00011", RUN3)]
    assert all(r["pay_ts"] <= T0 for r in sim.funding_rows)                    # 未来の日付の欠測は無い
    assert all(r["status"] != ST_MISSING for r in sim.funding_rows)
    assert {_key(r) for r in rebuild_from_obs(sim.obs_rows, mode)} == {_key(r) for r in sim.funding_rows}


# ---------------------------------------------------------------- 確定値型の欠測(間隔変更をまたぐ)

def test_gap_rows_confirmed_steps_old_interval_up_to_pnt_then_new_interval():
    rows = gap_rows_confirmed("X", T0 - IV4, T0, IV4, T0 + 2 * IV8, IV8)
    assert [(r["pay_ts"], r["interval_seconds"]) for r in rows] == [(T0, IV4), (T0 + IV8, IV8)]
    rows = gap_rows_confirmed("X", T0 - 3 * IV4, T0, IV4, T0 + 2 * IV8, IV8)
    assert [(r["pay_ts"], r["interval_seconds"]) for r in rows] == [(T0 - 2 * IV4, IV4), (T0 - IV4, IV4), (T0, IV4), (T0 + IV8, IV8)]
    assert all(r["status"] == ST_MISSING and r["interval_rate"] is None for r in rows)
    assert gap_rows_confirmed("X", None, T0, IV4, T0 + IV8, IV8) == []                     # 初めて見る銘柄
    assert gap_rows_confirmed("X", T0 - IV8, T0, IV8, T0, IV4) == []                        # 8h → 4h: 欠測なし
    assert gap_rows_confirmed("X", T0 - IV8, T0, IV8, T0, IV8) == []                        # 隣の窓(pay = 旧 next)
    assert [r["pay_ts"] for r in gap_rows_confirmed("X", T0 - IV8, T0, IV8, T0 + IV8, IV8)] == [T0]   # 1 窓飛んだ
    assert [r["pay_ts"] for r in gap_rows_confirmed("X", T0 - IV8, T0, IV8, T0 + 2 * IV8, IV8)] == [T0, T0 + IV8]
    assert gap_rows_confirmed("X", T0 - IV4, T0, IV4, T0 - IV4, IV8) == []                  # 4h → 8h で pay が戻る: 何も作らない


def test_interval_shortening_across_rollover_confirmed_has_no_spurious_missing():
    sim = Simulator(ST_CONFIRMED)
    sim.run([perp("X-PERP", IV8, T0, "0.0001")], RUN1)                                       # confirmed T0 − 8h
    sim.run([perp("X-PERP", IV8, T0, "0.0001")], RUN2)
    r = sim.run([perp("X-PERP", IV4, T0 + IV4, "0.00005")], RUN3)                            # 8h → 4h
    assert [(x["pay_ts"], x["status"], x["interval_seconds"], x["interval_rate"]) for x in r.funding_rows] == [(T0, ST_CONFIRMED, IV4, "0.00005")]
    assert [(x["pay_ts"], x["status"]) for x in sim.funding_rows] == [(T0 - IV8, ST_CONFIRMED), (T0, ST_CONFIRMED)]
    assert {_key(x) for x in rebuild_from_obs(sim.obs_rows, ST_CONFIRMED)} == {_key(x) for x in sim.funding_rows}


def test_interval_shortening_across_rollover_estimated():
    sim = Simulator(ST_ESTIMATED)
    sim.run([perp("X-PERP", IV8, T0, "0.0001")], RUN1)
    sim.run([perp("X-PERP", IV8, T0, "0.0001")], RUN2)
    r = sim.run([perp("X-PERP", IV4, T0 + IV4, "0.00005")], RUN3)
    assert [(x["pay_ts"], x["status"], x["interval_seconds"]) for x in r.funding_rows] == [(T0, ST_ESTIMATED, IV8)]
    assert {_key(x) for x in rebuild_from_obs(sim.obs_rows, ST_ESTIMATED)} == {_key(x) for x in sim.funding_rows}


def test_interval_lengthening_whose_pay_goes_back_records_nothing_confirmed():
    sim = Simulator(ST_CONFIRMED)
    sim.run([perp("X-PERP", IV4, T0, "0.00005")], RUN1)                                      # confirmed T0 − 4h
    r = sim.run([perp("X-PERP", IV8, T0 + IV4, "0.0001")], RUN3)                             # 4h → 8h: pay = T0 − 4h(既に記録済み)
    assert r.funding_rows == [] and kinds(r.obs_rows) == [KIND_FIRST]
    assert {_key(x) for x in rebuild_from_obs(sim.obs_rows, ST_CONFIRMED)} == {_key(x) for x in sim.funding_rows}


# ---------------------------------------------------------------- 見込み値型: 既に記録した支払より前に欠測を作らない

def test_estimated_missing_rows_after_rollover_start_at_max_of_pnt_and_last_pay():
    prev = obs_row(RUN2, "X-PERP", IV8, T0, "0.0001")
    r = process_run([perp("X-PERP", IV8, T0 + 3 * IV8, "0.0001")], "2026-10-26T00:09:00Z", {"X-PERP": prev}, {}, RUN2, {"X-PERP": T0 + IV8}, ST_ESTIMATED)
    assert [(x["pay_ts"], x["status"]) for x in r.funding_rows] == [(T0 + 2 * IV8, ST_MISSING)]      # T0, T0 + 8h は作らない
    r2 = process_run([perp("X-PERP", IV8, T0 + 3 * IV8, "0.0001")], "2026-10-26T00:09:00Z", {"X-PERP": prev}, {}, RUN2, {"X-PERP": T0 + 2 * IV8}, ST_ESTIMATED)
    assert r2.funding_rows == []


# ---------------------------------------------------------------- 巻き戻り(next_timestamp が一度戻る)後も rebuild と一致

def _backwards_glitch_sequence(mode):
    sim = Simulator(mode)
    sim.run([perp("X-PERP", IV8, T0 - IV8, "0.0002")], "2026-10-24T15:50:00Z")        # 窓 08:00–16:00
    sim.run([perp("X-PERP", IV8, T0, "0.0003")], RUN1)                                 # 窓 16:00–00:00
    glitch = sim.run([perp("X-PERP", IV8, T0 - IV8, "0.0009")], RUN2)                  # 戻った
    assert glitch.funding_rows == [] and KIND_FIRST not in kinds(glitch.obs_rows) and glitch.n_contradictions == 0
    assert sim.ticker in glitch.absent if hasattr(sim, "ticker") else True
    assert any("went backwards" in a for a in glitch.anomalies)
    sim.run([perp("X-PERP", IV8, T0, "0.0004")], "2026-10-24T23:41:30Z")              # 元に戻る
    sim.run([perp("X-PERP", IV8, T0 + IV8, "0.0005")], RUN3)
    return sim


@pytest.mark.parametrize("mode", [ST_ESTIMATED, ST_CONFIRMED])
def test_rebuild_matches_live_after_single_backwards_glitch(mode):
    sim = _backwards_glitch_sequence(mode)
    live = sim.funding_rows
    pays = [r["pay_ts"] for r in live]
    assert len(set(pays)) == len(pays)                                                   # 重複なし
    assert all(r["status"] != ST_MISSING for r in live)
    if mode == ST_ESTIMATED:
        assert [(r["pay_ts"], r["interval_rate"], r["basis_obs_ts"]) for r in live] == \
            [(T0 - IV8, "0.0002", "2026-10-24T15:50:00Z"), (T0, "0.0004", "2026-10-24T23:41:30Z")]
    else:
        assert [(r["pay_ts"], r["interval_rate"]) for r in live] == [(T0 - 2 * IV8, "0.0002"), (T0 - IV8, "0.0003"), (T0, "0.0005")]
    rebuilt = rebuild_from_obs(sim.obs_rows, mode)
    full = lambda r: (int(r["pay_ts"]), r["ticker"], r["status"], r["interval_rate"], int(r["interval_seconds"]), r["basis_obs_ts"])
    assert {full(r) for r in rebuilt} == {full(r) for r in live}
    assert len(rebuilt) == len(live)
