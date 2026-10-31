"""aggregate: 期間の累積・年率(cum_stat)、日/週/月のまとめ(agg_payments)、月ごとの累積(monthly_sums)。
ページの JS と同じ定義(突き合わせは tests/test_cross_lang.py)。ここでは定義そのものを小さな例で固定する。"""
from __future__ import annotations

import datetime as dt

import pytest

from vfm import aggregate as ag
from vfm.aggregate import agg_payments, cum_stat, monthly_sums
from vfm.timeutil import annualize

H, DAY = 3600, 86400
SAT = 1792800000          # 2026-10-24(土)00:00Z
SUN = SAT + DAY           # 2026-10-25(日)
MON = SAT + 2 * DAY       # 2026-10-26(月)
NOW = SAT + 10 * DAY      # 2026-11-03T00:00:00Z
MS = 1000


def ms(ts: int) -> int:
    return ts * MS


# ---------------------------------------------------------------- cum_stat

def test_window_is_half_open_now_minus_days_excluded_now_included():
    start = NOW - 7 * DAY
    pays = [(start, 0.001, "c"), (start + 1, 0.002, "c"), (NOW, 0.004, "c"), (NOW + 1, 0.008, "c")]
    s = cum_stat(pays, NOW, 7, 8 * H)
    assert s["n"] == 2 and s["sum"] == pytest.approx(0.006)              # start ちょうどと now より後は入らない
    assert cum_stat([(start, 0.001, "c")], NOW, 7, 8 * H) is None


def test_denominator_uses_each_payments_own_interval():
    # 8h の古い支払い(4 つ目に間隔)と、今の間隔 4h の支払い 2 つ: 観測できた日数 = 8h + 4h + 4h
    pays = [(NOW - 20 * H, 0.0002, "c", 8 * H), (NOW - 12 * H, 0.0001, "e"), (NOW - 8 * H, 0.0001, "e")]
    s = cum_stat(pays, NOW, 7, 4 * H)
    assert s["days"] == pytest.approx(16 * H / DAY)
    assert s["sum"] == pytest.approx(0.0004)
    assert s["apr"] == pytest.approx(0.0004 * 365 / (16 / 24))
    # 間隔が 0 / None なら引数の間隔
    same = [(NOW - 12 * H, 0.0001, "e", 0), (NOW - 8 * H, 0.0001, "e", None)]
    assert cum_stat(same, NOW, 7, 4 * H)["days"] == pytest.approx(8 * H / DAY)


@pytest.mark.parametrize("iv", [H, 4 * H, 8 * H])
def test_constant_rate_annualizes_exactly_whatever_the_coverage(iv):
    for n in (1, 2, 5, 40):
        pays = [(NOW - k * iv, 0.0001, "e") for k in reversed(range(n))]
        for days in (7, 30, None):
            assert cum_stat(pays, NOW, days, iv)["apr"] == pytest.approx(annualize(0.0001, iv)), (iv, n, days)


def test_missing_windows_are_counted_not_added_and_do_not_dilute_the_apr():
    iv = 8 * H
    pays = [(NOW - 3 * iv, 0.0003, "c"), (NOW - 2 * iv, None, "m"), (NOW - iv, None, "missing"), (NOW, 0.0003, "estimated")]
    s = cum_stat(pays, NOW, 7, iv)
    assert s["n"] == 2 and s["n_missing"] == 2
    assert s["sum"] == pytest.approx(0.0006)
    assert s["days"] == pytest.approx(2 * iv / DAY)                     # 欠測の窓は分母に入れない
    assert s["apr"] == pytest.approx(annualize(0.0003, iv))
    # 期間の外の欠測は数えない。rate が無い行は status が何でも欠測
    more = [(NOW - 30 * DAY, None, "m")] + pays + [(NOW - 1, None, "e")]
    assert cum_stat(sorted(more), NOW, 7, iv)["n_missing"] == 3


def test_days_is_not_rounded():
    s = cum_stat([(NOW, 0.0001, "c")], NOW, 7, 8 * H)
    assert s["days"] == 8 * H / DAY and s["days"] != round(s["days"], 2)


def test_partial_uses_the_interval_argument_and_the_first_valid_payment():
    iv, start = 8 * H, NOW - 7 * DAY
    assert cum_stat([(start + iv, 0.0001, "c"), (NOW, 0.0001, "c")], NOW, 7, iv)["partial"] is False
    assert cum_stat([(start + iv + 1, 0.0001, "c"), (NOW, 0.0001, "c")], NOW, 7, iv)["partial"] is True
    # 先頭の欠測は開始に数えない(最初の「値のある」支払いで判定)
    lead = [(start - DAY, None, "m"), (start + iv + 1, 0.0001, "c"), (NOW, 0.0001, "c")]
    assert cum_stat(lead, NOW, 7, iv)["partial"] is True
    assert cum_stat(lead, NOW, None, iv)["partial"] is False             # 全期間は ※ にしない


def test_start_is_the_date_of_the_first_valid_payment_even_outside_the_window():
    pays = [(SAT - 40 * DAY, None, "m"), (SAT - 20 * DAY, 0.0001, "c"), (NOW, 0.0001, "c")]
    s = cum_stat(pays, NOW, 7, 8 * H)
    assert s["start"] == "2026-10-04" and s["n"] == 1


def test_status_spellings_are_equivalent():
    long = [(NOW - 2 * H, 0.0001, "confirmed"), (NOW - H, 0.0002, "estimated"), (NOW, None, "missing")]
    short = [(t, r, {"confirmed": "c", "estimated": "e", "missing": "m"}[s]) for t, r, s in long]
    assert cum_stat(long, NOW, 7, H) == cum_stat(short, NOW, 7, H)


def test_none_without_valid_payments_in_the_window():
    assert cum_stat([], NOW, 7, H) is None
    assert cum_stat([(NOW, None, "m")], NOW, 7, H) is None
    assert cum_stat([(NOW + H, 0.0001, "c")], NOW, None, H) is None      # 未来の支払いだけ


def test_build_site_reexports_the_same_cum_stat():
    from vfm import build_site
    assert build_site.cum_stat is ag.cum_stat


# ---------------------------------------------------------------- 区切り

def test_bucket_starts():
    t = SAT + 13 * H + 5
    assert ag.day_start(ms(t)) == ms(SAT)
    assert ag.week_start(ms(SAT + 5 * H)) == ms(SAT - 5 * DAY)             # 土曜 → その週の月曜(10-19)
    assert ag.week_start(ms(SUN + 23 * H)) == ms(SAT - 5 * DAY)            # 日曜はまだ前の週
    assert ag.week_start(ms(MON)) == ms(MON)                               # 月曜はじまり
    assert ag.month_start(ms(SAT + 5 * H)) == ms(int(dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc).timestamp()))


def test_next_bucket_steps_days_weeks_and_months_across_the_year_end():
    dec1 = int(dt.datetime(2026, 12, 1, tzinfo=dt.timezone.utc).timestamp())
    jan1 = int(dt.datetime(2027, 1, 1, tzinfo=dt.timezone.utc).timestamp())
    assert ag.next_bucket(ms(SAT), "D") == ms(SUN)
    assert ag.next_bucket(ms(MON), "W") == ms(MON + 7 * DAY)
    assert ag.next_bucket(ms(dec1), "M") == ms(jan1)


# ---------------------------------------------------------------- agg_payments

def test_raw_is_one_point_per_payment_in_percent_with_missing_as_none():
    pays = [(SAT + 8 * H, 0.0002, "e"), (SAT, 0.0001, "c"), (SAT + 16 * H, None, "m")]   # 並びは問わない
    assert agg_payments(pays, "raw") == [
        {"x": ms(SAT), "y": pytest.approx(0.01), "est": False, "n": 1, "miss": 0},
        {"x": ms(SAT + 8 * H), "y": pytest.approx(0.02), "est": True, "n": 1, "miss": 0},
        {"x": ms(SAT + 16 * H), "y": None, "est": False, "n": 0, "miss": 1},
    ]


def test_daily_buckets_sum_fill_gaps_with_none_and_flag_estimates():
    pays = [(SAT, 0.0001, "c"), (SAT + 8 * H, 0.0002, "e"), (SAT + 16 * H, None, "m"),     # 土: 2 つ + 欠測 1
            (MON + 8 * H, None, "m"), (MON + 16 * H, None, "m"),                            # 月: 欠測だけ
            (MON + DAY, 0.0003, "c")]                                                      # 火
    out = agg_payments(pays, "D")
    assert [p["x"] for p in out] == [ms(SAT), ms(SUN), ms(MON), ms(MON + DAY)]              # 日曜は支払いが無くても埋める
    sat, sun, mon, tue = out
    assert sat == {"x": ms(SAT), "y": pytest.approx(0.03), "est": True, "n": 2, "miss": 1}
    assert sun == {"x": ms(SUN), "y": None, "est": False, "n": 0, "miss": 0}
    assert mon == {"x": ms(MON), "y": None, "est": False, "n": 0, "miss": 2}               # 欠測だけの日は 0 ではなく None
    assert tue == {"x": ms(MON + DAY), "y": pytest.approx(0.03), "est": False, "n": 1, "miss": 0}


def test_weekly_buckets_start_on_monday():
    pays = [(SUN + 12 * H, 0.0001, "c"), (MON + H, 0.0002, "c"), (MON + 15 * DAY, 0.0004, "e")]
    out = agg_payments(pays, "W")
    assert [p["x"] for p in out] == [ms(SAT - 5 * DAY), ms(MON), ms(MON + 7 * DAY), ms(MON + 14 * DAY)]
    assert [p["n"] for p in out] == [1, 1, 0, 1]
    assert out[2]["y"] is None and out[3]["est"] is True
    assert out[0]["y"] == pytest.approx(0.01) and out[1]["y"] == pytest.approx(0.02)


def test_monthly_buckets_cross_the_year_end_and_fill_the_empty_month():
    nov = int(dt.datetime(2026, 11, 30, 16, tzinfo=dt.timezone.utc).timestamp())
    jan = int(dt.datetime(2027, 1, 1, 0, tzinfo=dt.timezone.utc).timestamp())
    out = agg_payments([(nov, 0.0001, "c"), (jan, 0.0002, "e"), (jan + 8 * H, None, "m")], "M")
    starts = [int(dt.datetime(y, m, 1, tzinfo=dt.timezone.utc).timestamp()) for y, m in ((2026, 11), (2026, 12), (2027, 1))]
    assert [p["x"] for p in out] == [ms(s) for s in starts]
    assert out[1] == {"x": ms(starts[1]), "y": None, "est": False, "n": 0, "miss": 0}
    assert out[2]["y"] == pytest.approx(0.02) and out[2]["miss"] == 1 and out[2]["est"] is True


def test_agg_payments_empty_and_all_missing():
    assert agg_payments([], "D") == [] and agg_payments([], "raw") == []
    out = agg_payments([(SAT, None, "m"), (SAT + 8 * H, None, "m")], "D")
    assert out == [{"x": ms(SAT), "y": None, "est": False, "n": 0, "miss": 2}]


# ---------------------------------------------------------------- monthly_sums

def test_monthly_sums_exclude_missing_and_skip_months_with_only_missing():
    oct31 = int(dt.datetime(2026, 10, 31, 16, tzinfo=dt.timezone.utc).timestamp())
    nov1 = oct31 + 8 * H
    dec = int(dt.datetime(2026, 12, 5, tzinfo=dt.timezone.utc).timestamp())
    pays = [(oct31, 0.0001, "c"), (oct31 - 8 * H, 0.0002, "e"), (nov1, None, "m"), (nov1 + 8 * H, -0.0003, "c"), (dec, None, "m")]
    assert monthly_sums(pays) == {"2026-10": pytest.approx(0.0003), "2026-11": pytest.approx(-0.0003)}
    assert monthly_sums([]) == {}
