"""timeutil: 年率換算、ISO/JST の変換、時刻の丸め。"""
from __future__ import annotations

import datetime as dt

import pytest

from tests.conftest import funded_listings, load_doc
from vfm import timeutil as tu


def test_periods_per_year_for_1h_4h_8h():
    assert tu.periods_per_year(3600) == 8760
    assert tu.periods_per_year(14400) == 2190
    assert tu.periods_per_year(28800) == 1095


def test_annualize_multiplies_by_periods_per_year():
    assert tu.annualize(0.0000125, 3600) == pytest.approx(0.1095)
    assert tu.annualize(0.00005, 14400) == pytest.approx(0.1095)
    assert tu.annualize(0.0001, 28800) == pytest.approx(0.1095)
    assert tu.annualize(0.0002, 28800) == pytest.approx(0.219)
    assert tu.annualize(-0.002854, 3600) == pytest.approx(-25.00104)
    assert tu.annualize(0.0, 14400) == 0.0


def test_periods_per_year_rejects_non_positive_interval():
    with pytest.raises(ValueError):
        tu.periods_per_year(0)
    with pytest.raises(ValueError):
        tu.annualize(0.0001, -1)


def test_annualize_matches_api_annualized_rate_for_every_fixture_perp():
    checked = 0
    for x in funded_listings(load_doc()):
        f = x["funding"]
        rate, api = float(f["last"]["interval_rate"]), float(f["last"]["annualized_rate"])
        calc = tu.annualize(rate, f["interval_seconds"])
        if rate == 0:
            assert api == 0 and calc == 0, x["ticker"]
        else:
            assert calc == pytest.approx(api, rel=1e-3), x["ticker"]
        checked += 1
    assert checked >= 10


def test_interest_component_and_apr():
    assert tu.INTEREST_PER_HOUR == 0.0000125
    assert tu.interest_component(28800) == pytest.approx(0.0001)
    assert tu.interest_component(14400) == pytest.approx(0.00005)
    assert tu.interest_component(3600) == pytest.approx(0.0000125)
    assert tu.INTEREST_APR == pytest.approx(0.1095)
    # どの間隔でも金利成分の年率は同じ
    for iv in (3600, 14400, 28800):
        assert tu.annualize(tu.interest_component(iv), iv) == pytest.approx(0.1095)


def test_parse_iso_and_fmt_iso_round_trip():
    s = "2026-10-24T23:19:15Z"
    d = tu.parse_iso(s)
    assert d == dt.datetime(2026, 10, 24, 23, 19, 15, tzinfo=tu.UTC)
    assert d.tzinfo is not None
    assert tu.fmt_iso(d) == s
    assert tu.fmt_iso(tu.parse_iso(" 2026-10-24T23:19:15Z\n")) == s


def test_parse_iso_accepts_offsets_and_naive_as_utc():
    d = tu.parse_iso("2026-10-24T23:19:15Z")
    assert tu.parse_iso("2026-10-24T23:19:15+00:00") == d
    assert tu.parse_iso("2026-10-25T08:19:15+09:00") == d      # JST 表記は UTC に直す
    assert tu.parse_iso("2026-10-24T23:19:15") == d            # 無印は UTC とみなす
    assert tu.parse_iso("2026-10-25T08:19:15+09:00").tzinfo == tu.UTC


def test_fmt_iso_converts_other_zones_to_utc():
    jst = dt.datetime(2026, 10, 25, 8, 19, 15, tzinfo=tu.JST)
    assert tu.fmt_iso(jst) == "2026-10-24T23:19:15Z"


def test_jst_conversions():
    d = tu.parse_iso("2026-10-24T23:19:15Z")
    j = tu.to_jst(d)
    assert (j.year, j.month, j.day, j.hour, j.minute, j.second) == (2026, 10, 25, 8, 19, 15)
    assert j.utcoffset() == dt.timedelta(hours=9)
    assert j == d   # 同じ瞬間
    assert tu.fmt_jst(d) == "2026-10-25 08:19 JST"
    assert tu.fmt_both(d) == "2026-10-24 23:19 UTC (08:19 JST)"


def test_fmt_both_accepts_jst_input():
    j = dt.datetime(2026, 10, 25, 8, 19, 15, tzinfo=tu.JST)
    assert tu.fmt_both(j) == "2026-10-24 23:19 UTC (08:19 JST)"


def test_floor_hour_day_str_month_str_use_utc():
    d = tu.parse_iso("2026-10-24T23:19:15Z")
    assert tu.floor_hour(d) == tu.parse_iso("2026-10-24T23:00:00Z")
    assert tu.day_str(d) == "2026-10-24"
    assert tu.month_str(d) == "2026-10"
    # JST の datetime を渡しても UTC の日付・時で丸める
    j = tu.to_jst(d)
    assert tu.day_str(j) == "2026-10-24"
    assert tu.month_str(j) == "2026-10"
    assert tu.floor_hour(j) == tu.parse_iso("2026-10-24T23:00:00Z")
    assert tu.floor_hour(j).tzinfo == tu.UTC


def test_floor_hour_keeps_whole_hours():
    d = tu.parse_iso("2026-10-25T00:00:00Z")
    assert tu.floor_hour(d) == d


def test_unix_round_trip():
    d = tu.from_unix(1792886400)
    assert tu.fmt_iso(d) == "2026-10-25T00:00:00Z"
    assert tu.to_unix(d) == 1792886400
    assert tu.to_unix(tu.from_unix(1792886400.9)) == 1792886400
    assert tu.to_unix(tu.parse_iso("2026-10-25T00:00:00Z")) == 1792886400


def test_utc_now_is_aware_utc_without_microseconds():
    n = tu.utc_now()
    assert n.tzinfo == tu.UTC
    assert n.microsecond == 0
