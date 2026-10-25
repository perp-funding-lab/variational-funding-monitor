"""collect.run_once: 偽の fetch で 1 回分の収集を通し、書かれるファイルと実行ログを確かめる。"""
from __future__ import annotations

import copy
import datetime as dt

import pytest

from tests.conftest import RUN1, RUN2, RUN3, T0, advance, body, get_listing, load_doc, parse, set_interval, set_rate
from vfm.collect import LATEST_CACHE, _dec2, _int_str, _sig, load_prev_changes, run_once
from vfm.fetch import FetchError, FetchResult
from vfm.store import OBS_HEADER, RUN_HEADER, DataPaths, append_csv, load_json, read_csv, save_json
from vfm.timeutil import parse_iso

SETTINGS = {"funding_mode": "estimated", "anomaly_warn_per_day": 20}


def fake_fetch(b: bytes):
    return lambda: FetchResult(200, b, 1, 0.0)


def failing_fetch(exc: Exception):
    def f():
        raise exc
    return f


def test_three_runs_write_obs_snapshots_meta_swaps_cache_and_run_log(tmp_path):
    root = tmp_path
    data = root / "data"
    doc1 = load_doc()
    st = parse(doc1)
    n_perp, n_swap = len(st.perps), len(st.swaps)
    n_funded = sum(p.has_funding for p in st.perps)
    t1, t2, t3 = parse_iso(RUN1), parse_iso(RUN2), parse_iso(RUN3)

    # ---- run 1
    row1 = run_once(data, SETTINGS, now=t1, scheduled=t1 - dt.timedelta(seconds=15), trigger="worker", fetch=fake_fetch(body(doc1)), project_root=root)
    assert row1["http_status"] == 200 and row1["delay_s"] == 15 and row1["trigger"] == "worker"
    assert row1["scheduled_ts"] == "2026-10-24T23:19:00Z" and row1["fetch_ts"] == RUN1
    assert (row1["n_listings"], row1["n_perp"], row1["n_swap"]) == (n_perp + n_swap, n_perp, n_swap)
    assert (row1["n_obs_rows"], row1["n_funding_rows"], row1["n_anomalies"]) == (n_funded, 0, 0)
    assert row1["note"] == f"new {n_funded}"

    obs1 = read_csv(data / "obs" / "2026-10-24.csv")
    assert len(obs1) == n_funded and {r["kind"] for r in obs1} == {"F"} and {r["obs_ts"] for r in obs1} == {RUN1}
    assert not (data / "funding").glob("*.csv") or list((data / "funding").glob("*.csv")) == []
    snap1 = read_csv(data / "snapshots" / "2026-10-24.csv")
    assert len(snap1) == n_perp + n_swap
    btc_snap = next(r for r in snap1 if r["ticker"] == "BTC-PERP")
    assert btc_snap == {"snap_ts": RUN1, "ticker": "BTC-PERP", "contract_type": "perpetual", "mark_price": "80433.95",
                        "long_oi": "95908240", "short_oi": "89209321", "volume_24h": "368352929", "base_spread_bps": "1.15"}
    copp_snap = next(r for r in snap1 if r["ticker"] == "COPP-SWAP")
    assert copp_snap["contract_type"] == "swap" and copp_snap["base_spread_bps"] == "" and copp_snap["mark_price"] == "6.566"
    nvda_snap = next(r for r in snap1 if r["ticker"] == "NVDA-PERP")      # funding 無しでもスナップショットには入る
    assert nvda_snap["mark_price"] != ""

    meta1 = load_json(data / "meta.json", None)
    assert meta1["collection_start"] == RUN1 and meta1["last_success_ts"] == RUN1 and meta1["last_run_ts"] == RUN1
    assert meta1["funding_mode"] == "estimated"
    assert meta1["anomalies_by_day"] == {"2026-10-24": {"n": 0, "checked": 0}}

    swaps = load_json(data / "swaps_latest.json", None)
    assert swaps["snap_ts"] == RUN1 and [r["ticker"] for r in swaps["rows"]] == ["COPP-SWAP", "XAU-SWAP"]
    assert swaps["rows"][0]["last"]["interval_long_rate"] == "0.0005431" and swaps["rows"][0]["next"] is None
    assert swaps["rows"][0]["interval_seconds"] == 86400 and swaps["rows"][0]["name"] == "Swap on Copper Spot"

    cache = load_json(root / LATEST_CACHE, None)
    assert cache["fetch_ts"] == RUN1 and len(cache["perps"]) == n_perp
    btc_cache = next(p for p in cache["perps"] if p["ticker"] == "BTC-PERP")
    assert btc_cache["interval_rate"] == "0.0001" and btc_cache["interval_seconds"] == 28800 and btc_cache["next_timestamp"] == T0
    assert btc_cache["name"] == "Bitcoin" and isinstance(btc_cache["mark_price"], str)
    assert (root / ".cache" / "latest_stats.json").read_text(encoding="utf-8").count("\n") == 1   # compact

    names = load_json(data / "names.json", None)
    assert names["BTC-PERP"] == "Bitcoin" and names["COPP-SWAP"] == "Swap on Copper Spot"

    # ---- run 2: 同じ時(スナップショット無し)、1 銘柄だけ値が変わる
    doc2 = set_rate(copy.deepcopy(doc1), "ETH-PERP", "0.00007")
    names_mtime = (data / "names.json").stat().st_mtime_ns
    row2 = run_once(data, SETTINGS, now=t2, fetch=fake_fetch(body(doc2)), project_root=root)
    assert (row2["n_obs_rows"], row2["n_funding_rows"], row2["delay_s"], row2["note"]) == (1, 0, 0, "")
    assert len(read_csv(data / "snapshots" / "2026-10-24.csv")) == n_perp + n_swap
    obs2 = read_csv(data / "obs" / "2026-10-24.csv")
    assert len(obs2) == n_funded + 1 and obs2[-1]["kind"] == "C" and obs2[-1]["ticker"] == "ETH-PERP" and obs2[-1]["interval_rate"] == "0.00007"
    meta2 = load_json(data / "meta.json", None)
    assert meta2["last_success_ts"] == RUN2 and meta2["collection_start"] == RUN1
    assert (data / "names.json").stat().st_mtime_ns == names_mtime         # 名前が変わらなければ書き直さない

    # ---- run 3: 次の時・次の日、窓が進む
    doc3 = advance(copy.deepcopy(doc2), 1)
    row3 = run_once(data, SETTINGS, now=t3, fetch=fake_fetch(body(doc3)), project_root=root)
    assert row3["n_funding_rows"] == n_funded
    assert row3["n_obs_rows"] == 2 * n_funded - 1          # L 行(ETH 以外)+ F 行(全部)
    assert row3["n_anomalies"] == 0 and row3["note"] == ""
    day1 = read_csv(data / "obs" / "2026-10-24.csv")
    day2 = read_csv(data / "obs" / "2026-10-25.csv")
    assert len(day1) == n_funded + 1 + (n_funded - 1)
    l_rows = [r for r in day1 if r["kind"] == "L"]
    assert len(l_rows) == n_funded - 1 and {r["obs_ts"] for r in l_rows} == {RUN2}      # L 行は直前の取得時刻の日へ
    assert len(day2) == n_funded and {r["kind"] for r in day2} == {"F"} and {r["obs_ts"] for r in day2} == {RUN3}
    funding = read_csv(data / "funding" / "2026-10.csv")
    assert len(funding) == n_funded and {r["status"] for r in funding} == {"estimated"} and {r["pay_ts"] for r in funding} == {str(T0)}
    assert [r["ticker"] for r in funding] == sorted(r["ticker"] for r in funding)
    eth = next(r for r in funding if r["ticker"] == "ETH-PERP")
    assert eth["interval_rate"] == "0.00007" and eth["basis_obs_ts"] == RUN2
    btc = next(r for r in funding if r["ticker"] == "BTC-PERP")
    assert btc["interval_rate"] == "0.0001" and btc["basis_obs_ts"] == RUN2 and btc["annualized_rate"] == "0.1095"
    assert len(read_csv(data / "snapshots" / "2026-10-25.csv")) == n_perp + n_swap   # 新しい時の最初の成功
    meta3 = load_json(data / "meta.json", None)
    assert meta3["last_success_ts"] == RUN3 and meta3["collection_start"] == RUN1
    assert meta3["anomalies_by_day"] == {"2026-10-24": {"n": 0, "checked": 0}, "2026-10-25": {"n": 0, "checked": 0}}
    assert load_json(root / LATEST_CACHE, None)["fetch_ts"] == RUN3

    runs = read_csv(data / "runs" / "2026-10.csv")
    assert (data / "runs" / "2026-10.csv").read_text(encoding="utf-8").splitlines()[0] == ",".join(RUN_HEADER)
    assert [r["fetch_ts"] for r in runs] == [RUN1, RUN2, RUN3]
    assert [r["delay_s"] for r in runs] == ["15", "0", "0"]
    assert [r["n_obs_rows"] for r in runs] == [str(n_funded), "1", str(2 * n_funded - 1)]
    assert [r["n_funding_rows"] for r in runs] == ["0", "0", str(n_funded)]
    assert runs[0]["trigger"] == "worker" and runs[1]["trigger"] == "manual"


def test_fetch_error_run_logs_note_and_leaves_obs_and_last_success_untouched(tmp_path):
    root, data = tmp_path, tmp_path / "data"
    doc = load_doc()
    run_once(data, SETTINGS, now=parse_iso(RUN1), fetch=fake_fetch(body(doc)), project_root=root)
    obs_before = (data / "obs" / "2026-10-24.csv").read_bytes()
    snap_before = (data / "snapshots" / "2026-10-24.csv").read_bytes()
    row = run_once(data, SETTINGS, now=parse_iso(RUN2), fetch=failing_fetch(FetchError("HTTP 500", 500, 1)), project_root=root)
    assert row["http_status"] == 500
    assert row["note"] == "fetch failed: HTTP 500 (attempts 1)"
    assert row["n_obs_rows"] == "" and row["n_funding_rows"] == ""
    parse_iso(row["fetch_ts"])                                         # 時刻として読める
    assert (data / "obs" / "2026-10-24.csv").read_bytes() == obs_before
    assert (data / "snapshots" / "2026-10-24.csv").read_bytes() == snap_before
    assert sorted(p.name for p in (data / "obs").iterdir()) == ["2026-10-24.csv"]
    meta = load_json(data / "meta.json", None)
    assert meta["last_success_ts"] == RUN1 and meta["last_run_ts"] == RUN2
    runs = read_csv(data / "runs" / "2026-10.csv")
    assert len(runs) == 2 and runs[1]["http_status"] == "500" and runs[1]["note"].startswith("fetch failed")
    # ネットワーク例外(status 無し)は http_status 空欄
    row2 = run_once(data, SETTINGS, now=parse_iso(RUN3), fetch=failing_fetch(FetchError("network error: timeout", None, 4)), project_root=root)
    assert row2["http_status"] == "" and row2["note"] == "fetch failed: network error: timeout (attempts 4)"
    assert load_json(data / "meta.json", None)["last_success_ts"] == RUN1


def test_fetch_error_on_first_run_creates_meta_without_success(tmp_path):
    data = tmp_path / "data"
    row = run_once(data, SETTINGS, now=parse_iso(RUN1), fetch=failing_fetch(FetchError("HTTP 429", 429, 4)), project_root=tmp_path)
    assert row["http_status"] == 429
    meta = load_json(data / "meta.json", None)
    assert meta["last_success_ts"] is None and meta["collection_start"] == RUN1 and meta["funding_mode"] == "estimated"
    assert not (data / "swaps_latest.json").exists() and not (tmp_path / LATEST_CACHE).exists()


def test_schema_error_run_logs_schema_error(tmp_path):
    data = tmp_path / "data"
    row = run_once(data, SETTINGS, now=parse_iso(RUN1), fetch=fake_fetch(b"<html>maintenance</html>"), project_root=tmp_path)
    assert row["http_status"] == 200 and row["note"].startswith("schema error: not json")
    assert row["fetch_ts"] == RUN1 and row["n_listings"] == ""
    assert list((data / "obs").iterdir()) == [] and list((data / "snapshots").iterdir()) == []
    assert load_json(data / "meta.json", None)["last_success_ts"] is None
    assert len(read_csv(data / "runs" / "2026-10.csv")) == 1
    # 件数が少なすぎる応答も schema error
    doc = load_doc()
    row2 = run_once(data, SETTINGS, now=parse_iso(RUN2), fetch=fake_fetch(body(doc, pad=False)), project_root=tmp_path)
    assert row2["note"].startswith("schema error: too few listings")


@pytest.mark.parametrize("settings", [{}, {"funding_mode": None}, {"funding_mode": "weird"}])
def test_missing_funding_mode_raises(tmp_path, settings):
    with pytest.raises(ValueError):
        run_once(tmp_path / "data", settings, now=parse_iso(RUN1), fetch=fake_fetch(body(load_doc())), project_root=tmp_path)


def test_confirmed_mode_writes_funding_on_first_run_and_counts_anomalies(tmp_path):
    data = tmp_path / "data"
    doc = load_doc()
    settings = {"funding_mode": "confirmed"}
    row1 = run_once(data, settings, now=parse_iso(RUN1), fetch=fake_fetch(body(doc)), project_root=tmp_path)
    n_funded = row1["n_obs_rows"]
    assert row1["n_funding_rows"] == n_funded
    funding = read_csv(data / "funding" / "2026-10.csv")
    assert {r["status"] for r in funding} == {"confirmed"}
    btc = next(r for r in funding if r["ticker"] == "BTC-PERP")
    assert btc["pay_ts"] == str(T0 - 28800) and btc["basis_obs_ts"] == RUN1
    doc2 = set_rate(copy.deepcopy(doc), "ETH-PERP", "0.00007")
    row2 = run_once(data, settings, now=parse_iso(RUN2), fetch=fake_fetch(body(doc2)), project_root=tmp_path)
    assert row2["n_anomalies"] == 1 and row2["note"].startswith("anomalies: ETH-PERP: rate changed inside window")
    meta = load_json(data / "meta.json", None)
    assert meta["anomalies_by_day"]["2026-10-24"]["n"] == 1
    assert meta["anomalies_by_day"]["2026-10-24"]["checked"] >= 1
    assert meta["funding_mode"] == "confirmed"


def test_estimated_anomaly_counts_reach_meta(tmp_path):
    data = tmp_path / "data"
    doc = load_doc()
    t0 = parse_iso("2026-10-24T23:00:00Z")
    run_once(data, SETTINGS, now=t0, fetch=fake_fetch(body(doc)), project_root=tmp_path)
    run_once(data, SETTINGS, now=parse_iso(RUN2), fetch=fake_fetch(body(doc)), project_root=tmp_path)      # 31.5 分、値は動かない
    row3 = run_once(data, SETTINGS, now=parse_iso(RUN3), fetch=fake_fetch(body(advance(copy.deepcopy(doc), 1))), project_root=tmp_path)
    st = parse(doc)
    from vfm.funding import is_trivial_rate
    non_trivial = sum(1 for p in st.perps if p.has_funding and not is_trivial_rate(p.interval_rate, p.interval_seconds))
    assert row3["n_anomalies"] == non_trivial == 5
    meta = load_json(data / "meta.json", None)
    assert meta["anomalies_by_day"]["2026-10-25"] == {"n": non_trivial, "checked": non_trivial}
    assert row3["note"].startswith("anomalies: ") and row3["note"].count("rate never moved") == 5


def test_legacy_int_anomalies_by_day_is_upgraded_and_only_7_days_kept(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    old_days = {f"2026-10-{d:02d}": {"n": 0, "checked": 0} for d in range(10, 17)}
    old_days["2026-10-17"] = 3            # 旧形式(整数)
    old_days["2026-10-24"] = 2
    save_json(data / "meta.json", {"collection_start": "2026-10-01T00:00:00Z", "last_run_ts": None, "last_success_ts": None,
                                   "funding_mode": "estimated", "anomalies_by_day": old_days})
    run_once(data, SETTINGS, now=parse_iso(RUN1), fetch=fake_fetch(body(load_doc())), project_root=tmp_path)
    meta = load_json(data / "meta.json", None)
    ab = meta["anomalies_by_day"]
    assert len(ab) == 7 and list(ab) == sorted(ab)
    assert "2026-10-10" not in ab and "2026-10-11" not in ab
    assert ab["2026-10-17"] == 3                                   # 触らない日はそのまま
    assert ab["2026-10-24"] == {"n": 2, "checked": 0}              # 今日の分は旧形式から変換して足す
    assert meta["collection_start"] == "2026-10-01T00:00:00Z"      # 既存の開始時刻を保つ


def test_run_once_in_next_month_splits_run_log_and_prunes_old_obs(tmp_path):
    data = tmp_path / "data"
    doc = load_doc()
    run_once(data, SETTINGS, now=parse_iso("2026-10-31T23:50:00Z"), fetch=fake_fetch(body(doc)), project_root=tmp_path)
    (data / "obs" / "2026-09-01.csv").write_text("x")           # 30 日より古い観測ログ
    run_once(data, SETTINGS, now=parse_iso("2026-11-01T00:05:00Z"), fetch=fake_fetch(body(doc)), project_root=tmp_path)
    assert (data / "runs" / "2026-10.csv").exists() and (data / "runs" / "2026-11.csv").exists()
    assert not (data / "obs" / "2026-09-01.csv").exists()
    assert (data / "snapshots" / "2026-11-01.csv").exists()


def test_funding_rows_go_to_month_of_pay_ts(tmp_path):
    data = tmp_path / "data"
    doc = load_doc()
    # 窓を 10 月末に置き、11 月に窓が進む → pay_ts は 10 月のファイル
    for x in doc["listings"]:
        if isinstance(x.get("funding"), dict):
            x["funding"]["next_timestamp"] = 1793491200          # 2026-11-01T00:00:00Z
    run_once(data, SETTINGS, now=parse_iso("2026-10-31T23:40:00Z"), fetch=fake_fetch(body(doc)), project_root=tmp_path)
    run_once(data, SETTINGS, now=parse_iso("2026-11-01T00:08:00Z"), fetch=fake_fetch(body(advance(copy.deepcopy(doc), 1))), project_root=tmp_path)
    assert (data / "funding" / "2026-11.csv").exists() and not (data / "funding" / "2026-10.csv").exists()
    assert {r["pay_ts"] for r in read_csv(data / "funding" / "2026-11.csv")} == {"1793491200"}


def test_load_prev_changes_counts_c_rows_and_first_ts_of_current_window(tmp_path):
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    rows = [
        {"obs_ts": "2026-10-24T10:00:00Z", "ticker": "X", "interval_seconds": 28800, "next_timestamp": 1, "interval_rate": "1", "annualized_rate": "", "kind": "F"},
        {"obs_ts": "2026-10-24T10:10:00Z", "ticker": "X", "interval_seconds": 28800, "next_timestamp": 1, "interval_rate": "2", "annualized_rate": "", "kind": "C"},
        {"obs_ts": "2026-10-24T10:20:00Z", "ticker": "X", "interval_seconds": 28800, "next_timestamp": 1, "interval_rate": "3", "annualized_rate": "", "kind": "C"},
        {"obs_ts": "2026-10-24T10:30:00Z", "ticker": "X", "interval_seconds": 28800, "next_timestamp": 1, "interval_rate": "3", "annualized_rate": "", "kind": "L"},
        {"obs_ts": "2026-10-24T18:00:00Z", "ticker": "X", "interval_seconds": 28800, "next_timestamp": 2, "interval_rate": "4", "annualized_rate": "", "kind": "F"},
        {"obs_ts": "2026-10-24T18:00:00Z", "ticker": "Y", "interval_seconds": 28800, "next_timestamp": 2, "interval_rate": "4", "annualized_rate": "", "kind": "C"},
    ]
    append_csv(paths.obs_dir / "2026-10-24.csv", OBS_HEADER, rows)
    prev = {"X": {"next_timestamp": "1"}}
    changes, first = load_prev_changes(paths, prev)
    assert changes == {"X": 2} and first == {"X": "2026-10-24T10:00:00Z"}
    changes, first = load_prev_changes(paths, {"X": {"next_timestamp": "2"}})
    assert changes == {} and first == {"X": "2026-10-24T18:00:00Z"}
    assert load_prev_changes(paths, {}) == ({}, {})


@pytest.mark.parametrize("v,expected", [
    ("80433.9531236734", "80433.95"), ("0.0044773734", "0.004477373"), ("5250294.0805954544882565", "5250294"),
    ("53.4937496566432", "53.49375"), ("0", "0"), ("0.000", "0"), ("1", "1"), ("123456789", "1.234568e+8"),
    (None, ""), ("abc", ""), ("", ""),
])
def test_sig_7_significant_digits(v, expected):
    assert _sig(v) == expected


def test_sig_custom_digits():
    assert _sig("80433.9531236734", 3) == "8.04e+4"
    assert _sig("1.23456", 3) == "1.23"


@pytest.mark.parametrize("v,expected", [
    ("2049421.7460786031842799", "2049422"), ("5250294.0805954544882565", "5250294"), ("0", "0"), ("2.5", "2"), ("3.5", "4"),
    ("-1.2", "-1"), ("7", "7"), (None, ""), ("abc", ""),
])
def test_int_str_rounds_to_integer(v, expected):
    assert _int_str(v) == expected


@pytest.mark.parametrize("v,expected", [
    ("5.6268255998506149", "5.63"), ("1.1450388352589006", "1.15"), ("0", "0.00"), ("12", "12.00"),
    ("0.125", "0.12"), ("0.135", "0.14"), ("0.005", "0.00"),       # Decimal の既定(偶数丸め)
    (None, ""), ("abc", ""),
])
def test_dec2(v, expected):
    assert _dec2(v) == expected


# ---------------------------------------------------------------- 429 の待ち時間、absent、skip_if_fresh、矛盾の計数

def _tree(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_fetch_ts_is_shifted_by_rounded_wait_seconds(tmp_path):
    data = tmp_path / "data"
    t1 = parse_iso(RUN1)
    row = run_once(data, SETTINGS, now=t1, scheduled=t1 - dt.timedelta(seconds=10), fetch=lambda: FetchResult(200, body(load_doc()), 3, 6.4), project_root=tmp_path)
    assert row["fetch_ts"] == "2026-10-24T23:19:21Z" and row["delay_s"] == 16
    assert {r["obs_ts"] for r in read_csv(data / "obs" / "2026-10-24.csv")} == {"2026-10-24T23:19:21Z"}
    meta = load_json(data / "meta.json", None)
    assert meta["last_success_ts"] == "2026-10-24T23:19:21Z" and meta["last_run_ts"] == RUN1
    assert meta["collection_start"] == RUN1                      # 開始時刻は初回の now(待ち時間は足さない)
    assert read_csv(data / "snapshots" / "2026-10-24.csv")[0]["snap_ts"] == "2026-10-24T23:19:21Z"
    # 0.4 秒は丸めて 0
    row2 = run_once(data, SETTINGS, now=parse_iso(RUN2), fetch=lambda: FetchResult(200, body(load_doc()), 1, 0.4), project_root=tmp_path)
    assert row2["fetch_ts"] == RUN2


def test_meta_absent_tracks_missing_tickers_and_basis_uses_real_last_observation(tmp_path):
    data = tmp_path / "data"
    doc = load_doc()
    run_once(data, SETTINGS, now=parse_iso(RUN1), fetch=fake_fetch(body(doc)), project_root=tmp_path)
    assert load_json(data / "meta.json", None)["absent"] == []
    run_once(data, SETTINGS, now=parse_iso(RUN2), fetch=fake_fetch(body(doc)), project_root=tmp_path)          # 全員居る、変化なし
    gone = copy.deepcopy(doc)
    get_listing(gone, "ETH-PERP")["funding"] = None                                                             # ETH の funding が消えた
    t3 = "2026-10-24T23:41:30Z"
    row3 = run_once(data, SETTINGS, now=parse_iso(t3), fetch=fake_fetch(body(gone)), project_root=tmp_path)
    assert load_json(data / "meta.json", None)["absent"] == ["ETH-PERP"]
    assert row3["n_obs_rows"] == 1
    obs = read_csv(data / "obs" / "2026-10-24.csv")
    l_rows = [r for r in obs if r["kind"] == "L"]
    assert [(r["ticker"], r["obs_ts"], r["interval_rate"]) for r in l_rows] == [("ETH-PERP", RUN2, "0.00006226")]   # 最後に見た取得の時刻
    t4 = "2026-10-24T23:51:30Z"
    row4 = run_once(data, SETTINGS, now=parse_iso(t4), fetch=fake_fetch(body(gone)), project_root=tmp_path)       # まだ居ない
    assert row4["n_obs_rows"] == 0 and load_json(data / "meta.json", None)["absent"] == ["ETH-PERP"]
    back = advance(copy.deepcopy(doc), 1)                                                                       # 戻ってきて窓も進んだ
    row5 = run_once(data, SETTINGS, now=parse_iso(RUN3), fetch=fake_fetch(body(back)), project_root=tmp_path)
    assert load_json(data / "meta.json", None)["absent"] == []
    funding = {r["ticker"]: r for r in read_csv(data / "funding" / "2026-10.csv")}
    assert funding["ETH-PERP"]["basis_obs_ts"] == RUN2 and funding["ETH-PERP"]["status"] == "estimated"      # 直前の取得 (23:51:30) ではない
    assert funding["BTC-PERP"]["basis_obs_ts"] == t4
    # 32 分動かなかった 0 でも金利成分でもない銘柄(ORDER/PUFFER/1000PEPE/1000FLOKI)は矛盾。戻ってきた ETH は 10 分しか見ていないので含めない
    assert row5["n_anomalies"] == 4 and row5["note"].count("rate never moved") == 4 and "ETH-PERP" not in row5["note"]
    assert load_json(data / "meta.json", None)["anomalies_by_day"]["2026-10-25"] == {"n": 4, "checked": 4}
    eth_rows = [r for r in read_csv(data / "obs" / "2026-10-24.csv") if r["ticker"] == "ETH-PERP"]
    assert [r["kind"] for r in eth_rows] == ["F", "L"]                                                         # 23:51:30 の偽 L 行は無い


def test_skip_if_fresh_returns_without_touching_files(tmp_path):
    data = tmp_path / "data"
    t1 = parse_iso(RUN1)
    run_once(data, SETTINGS, now=t1, fetch=fake_fetch(body(load_doc())), project_root=tmp_path)
    before = _tree(tmp_path)
    calls = []

    def counting_fetch():
        calls.append(1)
        return FetchResult(200, body(load_doc()), 1, 0.0)

    res = run_once(data, SETTINGS, now=t1 + dt.timedelta(minutes=5), trigger="schedule", fetch=counting_fetch, project_root=tmp_path, skip_if_fresh_seconds=720)
    assert res == {"skipped": True, "last_success_ts": RUN1, "age_s": 300, "trigger": "schedule"}
    assert calls == [] and _tree(tmp_path) == before
    # 20 分前なら進む
    res2 = run_once(data, SETTINGS, now=t1 + dt.timedelta(minutes=20), fetch=counting_fetch, project_root=tmp_path, skip_if_fresh_seconds=720)
    assert "skipped" not in res2 and res2["http_status"] == 200 and calls == [1]
    assert len(read_csv(data / "runs" / "2026-10.csv")) == 2
    # 指定しなければ直後でも進む
    res3 = run_once(data, SETTINGS, now=t1 + dt.timedelta(minutes=20, seconds=5), fetch=counting_fetch, project_root=tmp_path)
    assert res3["http_status"] == 200 and calls == [1, 1]


def test_skip_if_fresh_proceeds_without_meta_or_with_future_last_success(tmp_path):
    calls = []

    def counting_fetch():
        calls.append(1)
        return FetchResult(200, body(load_doc()), 1, 0.0)

    res = run_once(tmp_path / "data", SETTINGS, now=parse_iso(RUN1), fetch=counting_fetch, project_root=tmp_path, skip_if_fresh_seconds=720)
    assert res["http_status"] == 200 and calls == [1]
    # last_success_ts が未来(時計ずれ)なら飛ばさない
    res2 = run_once(tmp_path / "data", SETTINGS, now=parse_iso(RUN1) - dt.timedelta(minutes=1), fetch=counting_fetch, project_root=tmp_path, skip_if_fresh_seconds=720)
    assert res2["http_status"] == 200 and calls == [1, 1]
    # ちょうど閾値なら飛ばさない
    res3 = run_once(tmp_path / "data", SETTINGS, now=parse_iso(RUN1) + dt.timedelta(seconds=720), fetch=counting_fetch, project_root=tmp_path, skip_if_fresh_seconds=720)
    assert res3["http_status"] == 200


def test_interval_change_inside_window_is_logged_but_not_a_contradiction(tmp_path):
    data = tmp_path / "data"
    doc = load_doc()
    run_once(data, SETTINGS, now=parse_iso(RUN1), fetch=fake_fetch(body(doc)), project_root=tmp_path)
    changed = set_interval(copy.deepcopy(doc), "XAG-PERP", 28800)
    row = run_once(data, SETTINGS, now=parse_iso(RUN2), fetch=fake_fetch(body(changed)), project_root=tmp_path)
    assert row["n_anomalies"] == 1 and row["note"] == f"anomalies: XAG-PERP: interval 14400->28800 inside window {T0}"
    meta = load_json(data / "meta.json", None)
    assert meta["anomalies_by_day"]["2026-10-24"] == {"n": 0, "checked": 0}
    assert read_csv(data / "runs" / "2026-10.csv")[-1]["n_anomalies"] == "1"


def test_ahead_of_clock_sample_is_logged_but_not_recorded(tmp_path):
    data = tmp_path / "data"
    doc = load_doc()
    run_once(data, SETTINGS, now=parse_iso(RUN1), fetch=fake_fetch(body(doc)), project_root=tmp_path)
    glitch = copy.deepcopy(doc)
    get_listing(glitch, "SOL-PERP")["funding"]["next_timestamp"] = T0 + 2 * 86400     # (PAD は BTC の写しなので BTC は使わない)
    row = run_once(data, SETTINGS, now=parse_iso(RUN2), fetch=fake_fetch(body(glitch)), project_root=tmp_path)
    assert row["n_anomalies"] == 1 and "ahead of clock" in row["note"] and row["n_obs_rows"] == 0 and row["n_funding_rows"] == 0
    meta = load_json(data / "meta.json", None)
    assert meta["absent"] == ["SOL-PERP"] and meta["anomalies_by_day"]["2026-10-24"] == {"n": 0, "checked": 0}
    assert not (data / "funding").exists() or list((data / "funding").iterdir()) == []
