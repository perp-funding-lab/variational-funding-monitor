"""観測できなかった窓: 窓の間ずっと「居ない」回だけなら、その支払は missing(rate 空欄、0 にしない)。両方の funding_mode で、
居ない理由(funding.last が null / 一覧に無い / next_timestamp の巻き戻り / 時計より先の窓)によらず同じ結果になり、
観測ログからの作り直しとも一致すること。後半は見込み値型の鮮度(古い観測からは推定しない)と、長い停止の後の 1 回。"""
from __future__ import annotations

import copy
import json

import pytest

from tests.conftest import (DAY, H, Simulator, assert_rebuild_matches_live, at, body, funded_listings, hm, load_doc,
                            pay_rows, play, sample)
from vfm.collect import run_once
from vfm.fetch import FetchResult
from vfm.funding import ST_CONFIRMED, ST_ESTIMATED, ST_MISSING
from vfm.store import load_json, read_csv
from vfm.timeutil import parse_iso

MODES = (ST_ESTIMATED, ST_CONFIRMED)
R1, R2, R3, R4, R5 = "0.00011", "0.00012", "0.00013", "0.00014", "0.00015"


def absent_sample(kind: str, iv_h: float, true_nt_h: float, last_nt_h: float):
    """居ない回の標本。true_nt_h はその時刻の本当の窓、last_nt_h は直前に受け付けた窓。"""
    if kind == "funding-null":
        return sample(iv_h, true_nt_h, rate=None)                 # has_funding が False
    if kind == "not-listed":
        return None
    if kind == "backwards-glitch":
        return sample(iv_h, last_nt_h - iv_h, "0.009")            # 直前の窓より前の窓(無視される)
    if kind == "ahead-of-clock":
        return sample(iv_h, true_nt_h + 48, "0.009")              # 窓の開始が取得時刻より 2 日先(無視される)
    raise ValueError(kind)


KINDS = ("funding-null", "not-listed", "backwards-glitch", "ahead-of-clock")
EXPECTED_ANOMALY = {"funding-null": None, "not-listed": None, "backwards-glitch": "before the payment time (ignored)",
                    "ahead-of-clock": "ahead of clock"}


def steps_8h(kind):
    """8h: 窓 16:00–00:00 を 23:50 まで観測、窓 00:00–08:00(翌日)は居ない回だけ、08:08 に窓 08:00–16:00 で戻る。"""
    gone = [(t, 1, absent_sample(kind, 8, 32, 24)) for t in ("00:08", "02:00", "04:00", "06:00", "07:58")]
    return ([("15:50", 0, sample(8, 16, R1)), ("16:08", 0, sample(8, 24, R2)), ("23:50", 0, sample(8, 24, R2))] + gone +
            [("08:08", 1, sample(8, 40, R4)), ("15:58", 1, sample(8, 40, R4)), ("16:08", 1, sample(8, 48, R5))]), range(3, 8)


def steps_1h(kind):
    """1h: 窓 20:00–21:00 を 20:58 まで観測、21:00–00:00 の 3 窓は居ない回だけ、00:08(翌日)に戻る。"""
    gone = [(t, d, absent_sample(kind, 1, nt, 21)) for t, d, nt in (("21:08", 0, 22), ("22:08", 0, 23), ("23:08", 0, 24), ("23:58", 0, 24))]
    return ([("19:58", 0, sample(1, 20, R1)), ("20:08", 0, sample(1, 21, R2)), ("20:58", 0, sample(1, 21, R2))] + gone +
            [("00:08", 1, sample(1, 25, R3)), ("00:58", 1, sample(1, 25, R3)), ("01:08", 1, sample(1, 26, R4))]), range(3, 7)


EXPECTED = {
    ("8h", ST_ESTIMATED): [("16:00", "e", 8, R1, "15:50"), ("00:00+1d", "e", 8, R2, "23:50"), ("08:00+1d", "m", 8, None, None),
                           ("16:00+1d", "e", 8, R4, "15:58+1d")],
    ("8h", ST_CONFIRMED): [("08:00", "c", 8, R1, "15:50"), ("16:00", "c", 8, R2, "16:08"), ("00:00+1d", "m", 8, None, None),
                           ("08:00+1d", "c", 8, R4, "08:08+1d"), ("16:00+1d", "c", 8, R5, "16:08+1d")],
    ("1h", ST_ESTIMATED): [("20:00", "e", 1, R1, "19:58"), ("21:00", "e", 1, R2, "20:58"), ("22:00", "m", 1, None, None),
                           ("23:00", "m", 1, None, None), ("00:00+1d", "m", 1, None, None), ("01:00+1d", "e", 1, R3, "00:58+1d")],
    ("1h", ST_CONFIRMED): [("19:00", "c", 1, R1, "19:58"), ("20:00", "c", 1, R2, "20:08"), ("21:00", "m", 1, None, None),
                           ("22:00", "m", 1, None, None), ("23:00", "m", 1, None, None), ("00:00+1d", "c", 1, R3, "00:08+1d"),
                           ("01:00+1d", "c", 1, R4, "01:08+1d")],
}


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("shape", ["8h", "1h"])
def test_window_with_only_absent_runs_becomes_missing(shape, kind, mode):
    steps, gone_idx = (steps_8h if shape == "8h" else steps_1h)(kind)
    sim = play(mode, steps)
    assert pay_rows(sim) == EXPECTED[(shape, mode)]
    assert_rebuild_matches_live(sim)
    assert not any(r["interval_rate"] in ("0", 0, 0.0) for r in sim.funding_rows)
    for i in gone_idx:
        res = sim.results[i]
        assert res.absent == ["X-PERP"] and res.funding_rows == [] and res.n_rollover == 0
        want = EXPECTED_ANOMALY[kind]
        assert [want in a for a in res.anomalies] == ([True] if want else [])
    # 居なくなった最初の回に、直前に見た時刻を旧窓の最後の観測(L 行)として残す。無視した値は観測ログに入れない
    last_seen_hm, last_seen_day, _ = steps[gone_idx[0] - 1]
    assert [(r["kind"], r["obs_ts"]) for r in sim.results[gone_idx[0]].obs_rows] == [("L", at(last_seen_hm, last_seen_day))]
    assert "0.009" not in {r["interval_rate"] for r in sim.obs_rows}


def test_absence_kinds_are_indistinguishable_in_the_funding_rows():
    for shape, make in (("8h", steps_8h), ("1h", steps_1h)):
        for mode in MODES:
            results = {kind: pay_rows(play(mode, make(kind)[0])) for kind in KINDS}
            assert len({tuple(v) for v in results.values()}) == 1, (shape, mode)


@pytest.mark.parametrize("mode", MODES)
def test_new_ticker_whose_first_window_is_never_seen_again_writes_only_missing(mode):
    # 16:30 に 1 回だけ見えて、窓 3 つ分いない → 見込み値型は最初の窓(観測が古い)と、見ていない 2 窓が欠測。
    # 確定値型は最初の 1 行と、見ていない 2 窓の欠測と、戻った窓の確定値
    steps = [("16:30", 0, sample(8, 24, R1))] + [(t, d, None) for t, d in (("20:00", 0), ("00:08", 1), ("08:00", 1))] + \
            [("16:08", 1, sample(8, 48, R2))]
    sim = play(mode, steps)
    if mode == ST_ESTIMATED:
        assert pay_rows(sim) == [("00:00+1d", "m", 8, None, None), ("08:00+1d", "m", 8, None, None), ("16:00+1d", "m", 8, None, None)]
    else:
        assert pay_rows(sim) == [("16:00", "c", 8, R1, "16:30"), ("00:00+1d", "m", 8, None, None),
                                 ("08:00+1d", "m", 8, None, None), ("16:00+1d", "c", 8, R2, "16:08+1d")]
    assert_rebuild_matches_live(sim)


# ---------------------------------------------------------------- 見込み値型の鮮度: 窓の終わりの近くで観測できていなければ推定しない

@pytest.mark.parametrize("last_obs, status", [
    ("21:00", "m"),        # 収集が窓の途中で止まった(3 時間前の値は窓の終わりの値ではない)
    ("23:29:59", "m"),     # 支払時刻の 30 分 1 秒前
    ("23:30", "e"),        # ちょうど 30 分前
    ("23:58", "e"),
])
def test_stale_last_observation_gives_missing_and_rebuild_agrees(last_obs, status):
    steps = [("16:08", 0, sample(8, 24, R1)), (last_obs, 0, sample(8, 24, R2)), ("00:08", 1, sample(8, 32, R3))]
    for mode in MODES:
        sim = play(mode, steps)
        assert_rebuild_matches_live(sim)
        if mode == ST_ESTIMATED:
            row = pay_rows(sim)[0]
            assert row[:3] == ("00:00+1d", status, 8)
            assert row[3:] == ((R2, hm(DAY + _secs(last_obs))) if status == "e" else (None, None))
        else:   # 確定値型は各窓の最初の値なので鮮度に関係ない
            assert pay_rows(sim) == [("16:00", "c", 8, R1, "16:08"), ("00:00+1d", "c", 8, R3, "00:08+1d")]


def _secs(hms: str) -> int:
    h, m, *s = (int(x) for x in hms.split(":"))
    return h * H + m * 60 + (s[0] if s else 0)


def test_frozen_value_after_the_payment_time_is_a_fresh_basis():
    # 支払時刻を過ぎても API がしばらく同じ窓(next_timestamp = 00:00)を返す(凍結)。その値で推定してよい
    steps = [("16:08", 0, sample(8, 24, R1)), ("21:00", 0, sample(8, 24, R1)), ("00:01:30", 1, sample(8, 24, R2)),
             ("00:11", 1, sample(8, 32, R3))]
    sim = play(ST_ESTIMATED, steps)
    assert pay_rows(sim) == [("00:00+1d", "e", 8, R2, "00:01+1d")]
    assert sim.funding_rows[0]["basis_obs_ts"] == at("00:01:30", 1)
    assert_rebuild_matches_live(sim)


def test_stale_basis_after_an_absence_gives_missing_and_rebuild_agrees():
    # 21:00 に居て、21:10 から居なくなり、窓が閉じた後に戻る。最後に見たのは 21:00(支払の 3 時間前)なので欠測
    steps = [("16:08", 0, sample(8, 24, R1)), ("21:00", 0, sample(8, 24, R1)), ("21:10", 0, None), ("23:58", 0, None),
             ("00:08", 1, sample(8, 32, R2))]
    sim = play(ST_ESTIMATED, steps)
    assert pay_rows(sim) == [("00:00+1d", "m", 8, None, None)]
    assert [(r["kind"], r["obs_ts"]) for r in sim.results[2].obs_rows] == [("L", at("21:00"))]
    assert_rebuild_matches_live(sim)


# ---------------------------------------------------------------- 6 日間止まった後の 1 回: 間の窓はすべて欠測(推定も 0 も無し)

OUTAGE_TICKERS = (("A-PERP", 1), ("B-PERP", 4), ("C-PERP", 8))


def _outage_sim(mode):
    """20:05 と 20:20 に観測して止まり(どの銘柄も支払時刻の 30 分以上前)、6 日後の 21:08 に 1 回だけ取得する。"""
    def at_hour(t_h):
        return [sample(iv, (int(t_h // iv) + 1) * iv, R1, name) for name, iv in OUTAGE_TICKERS]
    sim = Simulator(mode)
    sim.run(at_hour(20 + 5 / 60), at("20:05"))
    sim.run(at_hour(20 + 20 / 60), at("20:20"))
    back = sim.run(at_hour(6 * 24 + 21 + 8 / 60), at("21:08", 6))
    return sim, back


@pytest.mark.parametrize("mode", MODES)
def test_six_day_outage_emits_missing_for_every_window_in_between(mode):
    sim, back = _outage_sim(mode)
    assert_rebuild_matches_live(sim)
    assert back.n_rollover == 3 and back.absent == []
    for name, iv in OUTAGE_TICKERS:
        mine = sorted((r for r in back.funding_rows if r["ticker"] == name), key=lambda r: r["pay_ts"])
        pays = [r["pay_ts"] for r in mine]
        assert all(b - a == iv * H for a, b in zip(pays, pays[1:])), name          # 1 窓も飛ばさない
        assert all(r["interval_seconds"] == iv * H for r in mine)
        if mode == ST_ESTIMATED:
            # 最初の窓(最後の観測が古い)から、今の窓の 1 つ前まで。今の窓は閉じていないので無い
            now_window_start = (int((6 * 24 + 21 + 8 / 60) // iv) + 1) * iv - iv
            assert pays[0] == DAY + (int(20 // iv) + 1) * iv * H and pays[-1] == DAY + now_window_start * H
            assert {r["status"] for r in mine} == {ST_MISSING}
            assert len(mine) == {1: 145, 4: 36, 8: 18}[iv]
        else:
            # 間の窓はすべて欠測、最後に今の窓の確定値 1 つ
            assert [r["status"] for r in mine] == [ST_MISSING] * (len(mine) - 1) + [ST_CONFIRMED]
            assert len(mine) - 1 == {1: 144, 4: 35, 8: 17}[iv]
        assert all(r["interval_rate"] is None for r in mine if r["status"] == ST_MISSING)
    assert not any(r["status"] == ST_ESTIMATED for r in back.funding_rows)


def test_six_day_outage_through_collect_writes_only_missing_rows(tmp_path):
    """収集(run_once)を通しても同じ: 6 日後の 1 回の FR はすべて欠測で、間隔ごとの件数が合う。"""
    doc = load_doc()

    def at_unix(t):
        d = copy.deepcopy(doc)
        for x in funded_listings(d):
            iv = x["funding"]["interval_seconds"]
            x["funding"]["next_timestamp"] = (t // iv + 1) * iv
        return body(d)

    data = tmp_path / "data"
    settings = {"funding_mode": "estimated"}
    for ts in ("2026-10-24T20:05:00Z", "2026-10-24T20:20:00Z", "2026-10-30T21:08:00Z"):
        t = parse_iso(ts)
        b = at_unix(int(t.timestamp()))
        row = run_once(data, settings, now=t, fetch=lambda b=b: FetchResult(200, b, 1, 0.0), project_root=tmp_path)
    funding = read_csv(data / "funding" / "2026-10.csv")
    assert funding and {r["status"] for r in funding} == {ST_MISSING}
    assert all(r["interval_rate"] == "" and r["basis_obs_ts"] == "" for r in funding)
    by_iv: dict[str, dict[str, int]] = {}
    for r in funding:
        by_iv.setdefault(r["interval_seconds"], {}).setdefault(r["ticker"], 0)
        by_iv[r["interval_seconds"]][r["ticker"]] += 1
    assert {iv: set(c.values()) for iv, c in by_iv.items()} == {"3600": {145}, "14400": {36}, "28800": {18}}
    assert int(row["n_funding_rows"]) == len(funding)
    assert load_json(data / "meta.json", None)["last_success_ts"] == "2026-10-30T21:08:00Z"
    assert json.loads((data / "names.json").read_text(encoding="utf-8"))["ORDER-PERP"] == "Orderly"
