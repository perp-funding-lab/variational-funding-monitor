"""支払間隔の変更: 窓の途中(短縮・延長)、next_timestamp を変えない変更、窓の境目での変更。両方の funding_mode で、
本番の process_run の結果と、観測ログからの作り直し(rebuild_from_obs)が行単位で一致することも確かめる。

時刻はすべて 2026-10-24(UTC)基準。'00:08+1d' は翌日。rate は窓ごとに変えて、どの観測が根拠になったか分かるようにしている。"""
from __future__ import annotations

import pytest

from tests.conftest import DAY, H, at, hm, play
from tests.conftest import assert_rebuild_matches_live as assert_consistent
from tests.conftest import pay_rows as rows
from tests.conftest import sample as X
from vfm.funding import (CLOCK_SKEW_SECONDS, KIND_CHANGE, KIND_FIRST, KIND_LAST, ST_CONFIRMED, ST_ESTIMATED,
                         is_interval_change)

MODES = (ST_ESTIMATED, ST_CONFIRMED)
R = {i: f"0.000{10 + i}" for i in range(1, 9)}   # R[1] = "0.00011" … R[8] = "0.00018"


# 前段: 8h 窓 08:00–16:00 を 15:40/15:55 に観測し、16:08 に窓 16:00–00:00 へ進む(16:00 の支払を記録済みにする)
PRE_8H = [("15:40", 0, X(8, 16, R[1])), ("15:55", 0, X(8, 16, R[1])), ("16:08", 0, X(8, 24, R[2]))]
PRE_4H = [("15:40", 0, X(4, 16, R[1])), ("15:55", 0, X(4, 16, R[1])), ("16:08", 0, X(4, 20, R[2]))]

# name: (steps, 変更が起きた回の添字, 種類, {mode: 期待する行})
#   種類 mid = 窓の途中の変更(旧窓は支払われない)、same = next_timestamp を変えない変更(同じ窓の C 行)、boundary = 境目(通常の窓切り替え)
SCENARIOS = {
    "8h->4h mid-window, new window starts with the old one": (
        PRE_8H + [("17:30", 0, X(8, 24, R[2])), ("18:00", 0, X(4, 20, R[3])), ("19:58", 0, X(4, 20, R[3])),
                  ("20:08", 0, X(4, 24, R[4])), ("23:58", 0, X(4, 24, R[4])), ("00:08", 1, X(4, 28, R[5]))],
        4, "mid",
        {ST_ESTIMATED: [("16:00", "e", 8, R[1], "15:55"), ("20:00", "e", 4, R[3], "19:58"), ("00:00+1d", "e", 4, R[4], "23:58")],
         ST_CONFIRMED: [("08:00", "c", 8, R[1], "15:40"), ("16:00", "c", 8, R[2], "16:08"), ("20:00", "c", 4, R[4], "20:08"),
                        ("00:00+1d", "c", 4, R[5], "00:08+1d")]}),
    "4h->8h mid-window, before the old payment time": (
        PRE_4H + [("17:00", 0, X(4, 20, R[2])), ("18:00", 0, X(8, 24, R[3])), ("23:58", 0, X(8, 24, R[3])),
                  ("00:08", 1, X(8, 32, R[4]))],
        4, "mid",
        {ST_ESTIMATED: [("16:00", "e", 4, R[1], "15:55"), ("00:00+1d", "e", 8, R[3], "23:58")],      # 20:00 は支払われない
         ST_CONFIRMED: [("12:00", "c", 4, R[1], "15:40"), ("16:00", "c", 4, R[2], "16:08"), ("00:00+1d", "c", 8, R[4], "00:08+1d")]}),
    "8h->4h in the second half keeps next_timestamp": (
        PRE_8H + [("21:00", 0, X(4, 24, R[3])), ("23:58", 0, X(4, 24, R[3])), ("00:08", 1, X(4, 28, R[4]))],
        3, "same",
        # 区切り・欠測・鮮度は窓の最初の間隔(8h)で数える(20:00 の偽の欠測を作らない)。記録する間隔は最後に見た間隔(4h)
        {ST_ESTIMATED: [("16:00", "e", 8, R[1], "15:55"), ("00:00+1d", "e", 4, R[3], "23:58")],
         ST_CONFIRMED: [("08:00", "c", 8, R[1], "15:40"), ("16:00", "c", 8, R[2], "16:08"), ("00:00+1d", "c", 4, R[4], "00:08+1d")]}),
    "4h->8h keeps next_timestamp (00:00 is a boundary of both)": (
        [("19:40", 0, X(4, 20, R[1])), ("19:55", 0, X(4, 20, R[1])), ("20:08", 0, X(4, 24, R[2])), ("21:00", 0, X(8, 24, R[3])),
         ("23:58", 0, X(8, 24, R[3])), ("00:08", 1, X(8, 32, R[4])), ("07:58", 1, X(8, 32, R[4])), ("08:08", 1, X(8, 40, R[5]))],
        3, "same",
        {ST_ESTIMATED: [("20:00", "e", 4, R[1], "19:55"), ("00:00+1d", "e", 8, R[3], "23:58"), ("08:00+1d", "e", 8, R[4], "07:58+1d")],
         ST_CONFIRMED: [("16:00", "c", 4, R[1], "19:40"), ("20:00", "c", 4, R[2], "20:08"), ("00:00+1d", "c", 8, R[4], "00:08+1d"),
                        ("08:00+1d", "c", 8, R[5], "08:08+1d")]}),
    "8h->4h exactly at the 00:00 boundary": (
        [("23:40", 0, X(8, 24, R[1])), ("23:58", 0, X(8, 24, R[1])), ("00:08", 1, X(4, 28, R[2])), ("03:58", 1, X(4, 28, R[2])),
         ("04:08", 1, X(4, 32, R[3]))],
        2, "boundary",
        # 旧窓は通常どおり閉じる。確定値型の 00:00 は旧窓(8h)の間隔で記録する(4h にすると年率が 2 倍になる)
        {ST_ESTIMATED: [("00:00+1d", "e", 8, R[1], "23:58"), ("04:00+1d", "e", 4, R[2], "03:58+1d")],
         ST_CONFIRMED: [("16:00", "c", 8, R[1], "23:40"), ("00:00+1d", "c", 8, R[2], "00:08+1d"), ("04:00+1d", "c", 4, R[3], "04:08+1d")]}),
    "4h->8h at a 4h-only boundary (20:00)": (
        [("19:40", 0, X(4, 20, R[1])), ("19:58", 0, X(4, 20, R[1])), ("20:08", 0, X(8, 24, R[2])), ("23:58", 0, X(8, 24, R[2])),
         ("00:08", 1, X(8, 32, R[3]))],
        2, "boundary",
        # 確定値型: 旧 4h 窓の支払時刻 20:00 を旧窓の間隔(4h)で記録する(新しい 8h 窓の開始 16:00 は記録済み)
        {ST_ESTIMATED: [("20:00", "e", 4, R[1], "19:58"), ("00:00+1d", "e", 8, R[2], "23:58")],
         ST_CONFIRMED: [("16:00", "c", 4, R[1], "19:40"), ("20:00", "c", 4, R[2], "20:08"), ("00:00+1d", "c", 8, R[3], "00:08+1d")]}),
    "1h->4h at a 4h boundary": (
        [("19:40", 0, X(1, 20, R[1])), ("19:58", 0, X(1, 20, R[1])), ("20:08", 0, X(4, 24, R[2])), ("23:58", 0, X(4, 24, R[2])),
         ("00:08", 1, X(4, 28, R[3]))],
        2, "boundary",
        {ST_ESTIMATED: [("20:00", "e", 1, R[1], "19:58"), ("00:00+1d", "e", 4, R[2], "23:58")],
         ST_CONFIRMED: [("19:00", "c", 1, R[1], "19:40"), ("20:00", "c", 1, R[2], "20:08"), ("00:00+1d", "c", 4, R[3], "00:08+1d")]}),
    "4h->8h observed 121 s before the old payment is a mid-window change": (
        [("19:40", 0, X(4, 20, R[1])), ("19:50", 0, X(4, 20, R[1])), ("19:57:59", 0, X(8, 24, R[2])), ("23:58", 0, X(8, 24, R[2])),
         ("00:08", 1, X(8, 32, R[3]))],
        2, "mid",
        {ST_ESTIMATED: [("00:00+1d", "e", 8, R[2], "23:58")],
         ST_CONFIRMED: [("16:00", "c", 4, R[1], "19:40"), ("00:00+1d", "c", 8, R[3], "00:08+1d")]}),
    "4h->8h observed 120 s before the old payment is a normal rollover": (
        [("19:40", 0, X(4, 20, R[1])), ("19:50", 0, X(4, 20, R[1])), ("19:58:00", 0, X(8, 24, R[2])), ("23:58", 0, X(8, 24, R[2])),
         ("00:08", 1, X(8, 32, R[3]))],
        2, "boundary",
        {ST_ESTIMATED: [("20:00", "e", 4, R[1], "19:50"), ("00:00+1d", "e", 8, R[2], "23:58")],
         ST_CONFIRMED: [("16:00", "c", 4, R[1], "19:40"), ("20:00", "c", 4, R[2], "19:58"), ("00:00+1d", "c", 8, R[3], "00:08+1d")]}),
}


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("name", list(SCENARIOS))
def test_interval_change_rows_and_rebuild(name, mode):
    steps, i_change, kind, expected = SCENARIOS[name]
    sim = play(mode, steps)
    assert rows(sim) == expected[mode]
    assert_consistent(sim)
    res = sim.results[i_change]
    piv, iv = (int(p.interval_seconds) for p in (steps[i_change - 1][2], steps[i_change][2]))
    assert res.absent == []                                       # どの種類でも銘柄は居る扱い
    if kind == "mid":
        assert res.n_interval_changes == 1 and res.n_rollover == 0
        assert len(res.anomalies) == 1 and f"interval changed {piv}->{iv} mid-window" in res.anomalies[0]
        assert res.funding_rows == [] or mode == ST_CONFIRMED     # 見込み値型は打ち切られた窓に FR も欠測も作らない
        assert res.obs_rows[-1]["kind"] == KIND_FIRST and res.obs_rows[-1]["interval_seconds"] == iv
    elif kind == "same":
        assert res.n_interval_changes == 0 and res.n_rollover == 0 and res.funding_rows == []
        assert [(r["kind"], r["interval_seconds"]) for r in res.obs_rows] == [(KIND_CHANGE, iv)]
        assert res.anomalies == [f"X-PERP: interval {piv}->{iv} inside window {steps[i_change][2].next_timestamp}"]
    else:
        assert res.n_interval_changes == 0 and res.n_rollover == 1 and res.anomalies == []


def test_mid_window_change_writes_last_row_for_the_old_window_seen_in_previous_fetch():
    sim = play(ST_ESTIMATED, SCENARIOS["8h->4h mid-window, new window starts with the old one"][0][:5])
    res = sim.results[-1]                                          # 18:00 の回
    assert [(r["kind"], r["obs_ts"], r["interval_seconds"] // H, hm(int(r["next_timestamp"])), r["interval_rate"]) for r in res.obs_rows] == \
        [(KIND_LAST, at("17:30"), 8, "00:00+1d", R[2]), (KIND_FIRST, at("18:00"), 4, "20:00", R[3])]


def test_mid_window_change_after_an_absence_writes_no_last_row():
    steps = PRE_8H + [("17:00", 0, X(8, 24, R[2])), ("17:30", 0, None), ("18:00", 0, X(4, 20, R[3]))]   # 17:30 は居なかった
    sim = play(ST_ESTIMATED, steps)
    assert [(r["kind"], r["obs_ts"]) for r in sim.results[-2].obs_rows] == [(KIND_LAST, at("17:00"))]   # 居なくなった回に 17:00 を残す
    assert [r["kind"] for r in sim.results[-1].obs_rows] == [KIND_FIRST]    # 直前に居なかったので 17:30 の偽の L 行は作らない
    assert sim.results[-1].n_interval_changes == 1
    assert_consistent(sim)


@pytest.mark.parametrize("mode", MODES)
def test_confirmed_mid_window_change_records_new_window_start_only_after_last_pay(mode):
    # 新規上場の直後に窓の途中で 8h→4h: 確定値型は nt − iv = 16:00 を記録する(まだ何も無いので)
    sim = play(mode, [("17:00", 0, X(8, 24, R[1])), ("18:00", 0, X(4, 20, R[2])), ("19:58", 0, X(4, 20, R[2])), ("20:08", 0, X(4, 24, R[3]))])
    if mode == ST_CONFIRMED:
        # 17:00 の新規で 16:00(8h)。18:00 の変更では pay = 16:00 が記録済みなので何も足さない
        assert rows(sim) == [("16:00", "c", 8, R[1], "17:00"), ("20:00", "c", 4, R[3], "20:08")]
        assert sim.results[1].funding_rows == []
    else:
        assert rows(sim) == [("20:00", "e", 4, R[2], "19:58")]
    assert_consistent(sim)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("glitch", [X(4, 16, R[8]), X(8, 16, R[8])], ids=["other-interval-at-old-window-start", "same-interval-backwards"])
def test_move_to_or_before_old_window_start_is_ignored_and_ticker_absent(mode, glitch):
    steps = PRE_8H + [("17:30", 0, X(8, 24, R[2])), ("18:00", 0, glitch), ("18:10", 0, X(8, 24, R[2])), ("23:58", 0, X(8, 24, R[2])),
                      ("00:08", 1, X(8, 32, R[3]))]
    sim = play(mode, steps)
    res = sim.results[4]
    assert res.absent == ["X-PERP"] and res.n_interval_changes == 0 and res.funding_rows == []
    assert res.anomalies == [f"X-PERP: next_timestamp moved {DAY + 24 * H}->{DAY + 16 * H} before the payment time (ignored)"]
    assert [(r["kind"], r["obs_ts"]) for r in res.obs_rows] == [(KIND_LAST, at("17:30"))]   # 窓 16:00–00:00 の最後の観測
    assert R[8] not in {r["interval_rate"] for r in sim.obs_rows}                            # 無視した値は記録しない
    if mode == ST_ESTIMATED:
        assert rows(sim) == [("16:00", "e", 8, R[1], "15:55"), ("00:00+1d", "e", 8, R[2], "23:58")]
    else:
        assert rows(sim) == [("08:00", "c", 8, R[1], "15:40"), ("16:00", "c", 8, R[2], "16:08"), ("00:00+1d", "c", 8, R[3], "00:08+1d")]
    assert_consistent(sim)


def test_interval_changes_are_counted_per_ticker_in_one_run():
    a, b, c = (lambda iv, nt, r=R[1]: X(iv, nt, r, "A-PERP")), (lambda iv, nt, r=R[1]: X(iv, nt, r, "B-PERP")), \
        (lambda iv, nt, r=R[1]: X(iv, nt, r, "C-PERP"))
    sim = play(ST_ESTIMATED, [("17:00", 0, [a(8, 24), b(4, 20), c(8, 24)]),
                              ("18:00", 0, [a(4, 20), b(8, 24), c(8, 24)])])
    res = sim.results[-1]
    assert res.n_interval_changes == 2 and sorted(x.split(":")[0] for x in res.anomalies) == ["A-PERP", "B-PERP"]
    assert res.absent == [] and res.funding_rows == []


def test_is_interval_change_rules():
    pnt = DAY + 24 * H
    # 8h→4h: next_timestamp が旧窓 16:00–00:00 の中へ前に動く
    assert is_interval_change(DAY + 20 * H, 4 * H, pnt, 8 * H, DAY + 18 * H)
    assert not is_interval_change(DAY + 16 * H, 4 * H, pnt, 8 * H, DAY + 18 * H)      # 旧窓の開始ちょうど(またはそれより前)は違う
    assert not is_interval_change(DAY + 12 * H, 4 * H, pnt, 8 * H, DAY + 18 * H)
    assert not is_interval_change(DAY + 20 * H, 8 * H, pnt, 8 * H, DAY + 18 * H)      # 間隔が同じなら巻き戻り
    # 4h→8h: 旧窓の支払時刻(20:00)の 120 秒より前に後ろへ動く
    pnt4 = DAY + 20 * H
    assert is_interval_change(DAY + 24 * H, 8 * H, pnt4, 4 * H, pnt4 - CLOCK_SKEW_SECONDS - 1)
    assert not is_interval_change(DAY + 24 * H, 8 * H, pnt4, 4 * H, pnt4 - CLOCK_SKEW_SECONDS)
    assert not is_interval_change(DAY + 24 * H, 8 * H, pnt4, 4 * H, pnt4 + 60)
    # 旧窓の開始は窓の最初の観測の間隔で数える(8h で始まり 4h に変わった窓 nt=00:00 の開始は 16:00)
    assert is_interval_change(DAY + 18 * H, H, pnt, 4 * H, DAY + 17 * H + 1800, 8 * H)
    assert not is_interval_change(DAY + 18 * H, H, pnt, 4 * H, DAY + 17 * H + 1800)   # 最後の間隔(4h)なら開始 20:00 より前


# ---------------------------------------------------------------- 途中から始まる窓の前に実在しない支払時刻を作らない(直した不具合の再発防止)

_PHANTOM_AFTER_SHORTENING = ("src: after a mid-window shortening whose new window starts after the old window's start "
                             "(8h->1h at 18:30, 4h->1h at 17:30), the next rollover fills 'missing' rows for pay times inside the "
                             "abandoned window (17:00, 18:00) that never existed")


@pytest.mark.parametrize("steps, expected", [
    (PRE_8H + [("18:20", 0, X(8, 24, R[2])), ("18:30", 0, X(1, 19, R[3])), ("18:58", 0, X(1, 19, R[3])), ("19:08", 0, X(1, 20, R[4]))],
     [("16:00", "e", 8, R[1], "15:55"), ("19:00", "e", 1, R[3], "18:58")]),
    (PRE_4H + [("17:20", 0, X(4, 20, R[2])), ("17:30", 0, X(1, 18, R[3])), ("17:58", 0, X(1, 18, R[3])), ("18:08", 0, X(1, 19, R[4]))],
     [("16:00", "e", 4, R[1], "15:55"), ("18:00", "e", 1, R[3], "17:58")]),
], ids=["8h->1h", "4h->1h"])
def test_mid_window_shortening_to_a_later_start_creates_no_phantom_missing_rows(steps, expected):
    sim = play(ST_ESTIMATED, steps)
    assert_consistent(sim)
    assert rows(sim) == expected
