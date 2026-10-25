"""build_site: 累積統計と、tmp のプロジェクトからのサイト用 JSON 生成。"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from tests.conftest import RUN3, T0
from vfm.build_site import _f, _r, build, cum_stat
from vfm.store import FUNDING_HEADER, OBS_HEADER, SNAP_HEADER, append_csv, save_json
from vfm.timeutil import INTEREST_APR, annualize, day_str, fmt_iso, from_unix, parse_iso, to_unix

IV = 28800
NOW = parse_iso(RUN3)                 # 2026-10-25T00:09:00Z
NOW_TS = int(NOW.timestamp())


def payments_8h(n: int, last: int = T0, rate: float = 0.0001, status: str = "estimated"):
    """last で終わる 8 時間刻みの n 回分(昇順)。"""
    return [(last - k * IV, rate, status) for k in reversed(range(n))]


# ---------------------------------------------------------------- cum_stat

def test_cum_stat_is_partial_when_data_is_shorter_than_window():
    pays = payments_8h(9)                    # 9 回 = ちょうど 3 日分の支払
    s = cum_stat(pays, NOW_TS, 7, IV)
    first = T0 - 8 * IV
    assert s["partial"] is True
    assert s["n"] == 9
    assert s["sum"] == pytest.approx(9 * 0.0001)
    # 窓を最初の支払に切り詰めたときは「支払が覆う期間」(最初の支払の窓の開始 〜 最後の支払)= 9 × 8h = 3 日で年率換算
    assert s["days"] == 3.0
    assert s["apr"] == pytest.approx(9 * 0.0001 * 365 / 3) == pytest.approx(annualize(0.0001, IV))
    assert s["start"] == day_str(from_unix(first)) == "2026-10-22"


def test_cum_stat_full_window_with_10_days_of_data():
    pays = payments_8h(30)                   # 9.67 日分
    s = cum_stat(pays, NOW_TS, 7, IV)
    assert s["partial"] is False
    frm = NOW_TS - 7 * 86400
    expected = [r for t, r, _ in pays if frm <= t <= NOW_TS]
    assert len(expected) < 30 and s["n"] == len(expected) == 21
    assert s["sum"] == pytest.approx(sum(expected))
    assert s["apr"] == pytest.approx(sum(expected) * 365 / 7)
    assert s["days"] == 7.0
    assert s["start"] == day_str(from_unix(pays[0][0]))             # start は最初の支払日(窓の始まりではない)


def test_cum_stat_partial_boundary_is_one_interval_of_slack():
    # 最初の支払が「7 日前 + 1 間隔」以内なら揃っているとみなす
    first_ok = NOW_TS - 7 * 86400 + IV
    assert cum_stat([(first_ok, 0.0001, "estimated"), (NOW_TS, 0.0001, "estimated")], NOW_TS, 7, IV)["partial"] is False
    assert cum_stat([(first_ok + 1, 0.0001, "estimated"), (NOW_TS, 0.0001, "estimated")], NOW_TS, 7, IV)["partial"] is True


def test_cum_stat_excludes_missing_and_never_counts_it_as_zero():
    base = payments_8h(9)
    with_missing = sorted(base + [(T0 - 9 * IV, None, "missing"), (T0 - 10 * IV, 0.0, "missing")])
    assert cum_stat(with_missing, NOW_TS, 7, IV) == cum_stat(base, NOW_TS, 7, IV)   # 先頭の missing は開始日も変えない
    # 途中の支払を missing に置き換えると、その分だけ減る
    mid = list(base)
    mid[4] = (mid[4][0], None, "missing")
    s_mid, s_base = cum_stat(mid, NOW_TS, 7, IV), cum_stat(base, NOW_TS, 7, IV)
    assert s_mid["n"] == s_base["n"] - 1
    assert s_mid["sum"] == pytest.approx(s_base["sum"] - 0.0001)
    assert s_mid["days"] == s_base["days"]
    # rate が無い行は status が何でも足さない
    assert cum_stat(base + [(T0 + IV, None, "estimated")], NOW_TS + IV, 7, IV)["n"] == 9


def test_cum_stat_days_none_covers_everything():
    pays = payments_8h(30)
    s = cum_stat(pays, NOW_TS, None, IV)
    assert s["partial"] is False and s["n"] == 30
    assert s["sum"] == pytest.approx(30 * 0.0001)
    assert s["days"] == 10.0                                          # 30 × 8h の支払が覆う期間
    assert s["apr"] == pytest.approx(30 * 0.0001 * 365 / 10) == pytest.approx(annualize(0.0001, IV))
    assert s["start"] == day_str(from_unix(pays[0][0]))


def test_cum_stat_returns_none_without_valid_payments():
    assert cum_stat([], NOW_TS, 7, IV) is None
    assert cum_stat([(T0, None, "missing")], NOW_TS, 7, IV) is None
    assert cum_stat([(NOW_TS + 1, 0.0001, "estimated")], NOW_TS, 7, IV) is None      # 未来の支払だけ
    assert cum_stat([(NOW_TS - 10 * 86400, 0.0001, "estimated")], NOW_TS, 7, IV) is None   # 窓の外だけ


def test_cum_stat_actual_days_has_one_interval_floor():
    s = cum_stat([(NOW_TS, 0.0002, "confirmed")], NOW_TS, 7, IV)
    assert s["days"] == round(IV / 86400, 2) and s["apr"] == pytest.approx(0.0002 * 1095)
    assert s["partial"] is True and s["n"] == 1


def test_cum_stat_negative_rates_sum():
    pays = [(T0 - IV, -0.001, "estimated"), (T0, 0.0004, "confirmed")]
    s = cum_stat(pays, NOW_TS, None, IV)
    assert s["sum"] == pytest.approx(-0.0006) and s["apr"] < 0


@pytest.mark.parametrize("v,expected", [(None, None), ("", None), ("0.0001", 0.0001), (0.5, 0.5), ("-0.002854", -0.002854), ("abc", None), ("1e-5", 1e-5)])
def test_f(v, expected):
    assert _f(v) == expected


# ---------------------------------------------------------------- build()

def _cache_perp(ticker, name, long_oi, short_oi, rate="0.0001", iv=IV, mark="100", vol="1000", spread="1.5"):
    return {"ticker": ticker, "name": name, "mark_price": mark, "long_oi": long_oi, "short_oi": short_oi, "volume_24h": vol,
            "spread_bps": spread, "interval_seconds": iv, "next_timestamp": T0 + iv, "interval_rate": rate,
            "annualized_rate": None}


def _fund_row(pay_ts, ticker, rate, status="estimated", iv=IV, basis="2026-10-25T00:01:00Z"):
    return {"pay_ts": pay_ts, "ticker": ticker, "interval_seconds": iv, "interval_rate": rate if status != "missing" else None,
            "annualized_rate": None, "status": status, "basis_obs_ts": basis if status != "missing" else None}


def _project(tmp_path, *, funding_mode="estimated", meta_extra=None, hl=False, settings_extra=None, cache=True):
    root = tmp_path / "proj"
    (root / "config").mkdir(parents=True)
    settings = {"funding_mode": funding_mode, "anomaly_warn_per_day": 20, "anomaly_warn_ratio": 0.5, "anomaly_warn_min_checked": 20, "stale_warn_hours": 2}
    settings.update(settings_extra or {})
    save_json(root / "config" / "settings.json", settings)
    (root / "config" / "category_overrides.csv").write_text("ticker,category,reason\nAGLD-PERP,crypto,game token\n", encoding="utf-8")
    (root / "config" / "hl_mapping.csv").write_text("ticker,hl_coin,note\nBTC-PERP,BTC,本体\n" if hl else "", encoding="utf-8")
    data = root / "data"
    meta = {"collection_start": "2026-10-15T00:00:00Z", "last_success_ts": fmt_iso(NOW - dt.timedelta(hours=3)),
            "last_run_ts": fmt_iso(NOW - dt.timedelta(hours=3)), "funding_mode": funding_mode, "anomalies_by_day": {}}
    meta.update(meta_extra or {})
    save_json(data / "meta.json", meta)
    rows = [_fund_row(T0 - k * IV, "BTC-PERP", "0.0001") for k in reversed(range(30))]                       # 10 日分
    rows += [_fund_row(T0 - 3 * IV, "ETH-PERP", None, "missing")] + [_fund_row(T0 - k * IV, "ETH-PERP", "0.00006") for k in (2, 1, 0)]
    rows.sort(key=lambda r: (r["pay_ts"], r["ticker"]))
    append_csv(data / "funding" / "2026-10.csv", FUNDING_HEADER, rows)
    append_csv(data / "obs" / "2026-10-25.csv", OBS_HEADER, [
        {"obs_ts": RUN3, "ticker": t, "interval_seconds": IV, "next_timestamp": T0 + IV, "interval_rate": "0.0001", "annualized_rate": "0.1095", "kind": "F"}
        for t in ("BTC-PERP", "ETH-PERP", "COIN-PERP", "AGLD-PERP")])
    if cache:
        save_json(root / ".cache" / "latest_stats.json", {"fetch_ts": RUN3, "perps": [
            _cache_perp("BTC-PERP", "Bitcoin", "100", "50", mark="80433.95"),
            _cache_perp("ETH-PERP", "Ethereum", "300", "10", rate="0.00006"),
            _cache_perp("COIN-PERP", "Coinbase Global, Inc.", "10", "10", rate="0"),
            _cache_perp("AGLD-PERP", "Adventure Gold", "5", "5"),
            {"ticker": "NVDA-PERP", "name": "NVIDIA Corporation", "mark_price": "212", "long_oi": "1", "short_oi": "1", "volume_24h": "1",
             "spread_bps": "1", "interval_seconds": None, "next_timestamp": None, "interval_rate": None, "annualized_rate": None},
        ]}, compact=True)
    save_json(data / "swaps_latest.json", {"snap_ts": RUN3, "rows": [
        {"ticker": "COPP-SWAP", "name": "Swap on Copper Spot", "mark_price": "6.566", "long_oi": "922408.55", "short_oi": "1157564.13",
         "volume_24h": "0", "interval_seconds": 86400, "next_timestamp": None,
         "last": {"annualized_long_rate": "0.1955", "annualized_short_rate": "0.1955", "interval_long_rate": "0.0005431", "interval_short_rate": "0.0005431"},
         "next": None}]})
    if hl:
        save_json(data / "hl" / "hl_7d.json", {"fetched": "2026-10-25T00:30:00Z", "coins": {"BTC": {"apr7d": 0.10, "n": 168}}}, compact=True)
    return root


def _read(out, name):
    return json.loads((out / "data" / name).read_text(encoding="utf-8"))


def test_build_summary_rows_and_ticker_files(tmp_path):
    root = _project(tmp_path)
    out = tmp_path / "site"
    res = build(root, out, now=NOW)
    summary = _read(out, "summary.json")
    assert res["n_rows"] == summary["n_tickers"] == 4 and res["n_swaps"] == 1 and res["missing_total"] == 1
    assert summary["stale_hours"] == 3.0 and summary["stale_hours"] > 2
    assert summary["generated"] == fmt_iso(NOW) and summary["last_success"] == fmt_iso(NOW - dt.timedelta(hours=3))
    assert summary["collection_start"] == "2026-10-15T00:00:00Z" and summary["funding_mode"] == "estimated"
    assert summary["interest_apr"] == pytest.approx(INTEREST_APR) and summary["hl_fetched"] is None
    assert summary["anomalies_today"] == 0 and summary["anomalies_checked_today"] == 0 and summary["anomaly_warn"] is False
    assert summary["anomaly_warn_per_day"] == 20 and summary["anomaly_warn_ratio"] == 0.5
    assert summary["missing_total"] == 1

    rows = summary["rows"]
    assert [r["ticker"] for r in rows] == ["ETH-PERP", "BTC-PERP", "COIN-PERP", "AGLD-PERP"]      # OI の大きい順
    assert [r["oi"] for r in rows] == [310.0, 150.0, 20.0, 10.0]
    by = {r["ticker"]: r for r in rows}

    eth = by["ETH-PERP"]
    assert eth["apr7"] is None and eth["apr30"] is None                       # 7 日分揃っていない
    assert eth["cum"]["7"][2] == 1 and eth["cum"]["7"][0] == pytest.approx(3 * 0.00006)   # [sum, apr, partial, start, days]
    assert eth["cum"]["7"][3] == day_str(from_unix(T0 - 2 * IV)) and eth["cum"]["7"][4] == 1.0
    assert eth["cum"]["7"][1] == pytest.approx(annualize(0.00006, IV), rel=1e-5)
    assert eth["n_pay"] == 3 and eth["n_missing"] == 1
    assert eth["last_status"] == "estimated" and eth["last_pay_ts"] == T0 and eth["last_rate"] == 0.00006
    assert eth["last_apr"] == pytest.approx(0.00006 * 1095)
    assert eth["cur_rate"] == 0.00006 and eth["cur_apr"] == pytest.approx(0.00006 * 1095)
    assert eth["long_share"] == pytest.approx(300 / 310) and eth["long_oi"] == 300.0 and eth["short_oi"] == 10.0
    assert eth["data_start"] == day_str(from_unix(T0 - 3 * IV))              # missing も開始日に数える
    assert eth["category"] == "crypto" and eth["name"] == "Ethereum" and eth["interval"] == IV and eth["next_ts"] == T0 + IV
    assert eth["last_obs"] == RUN3 and eth["hl_coin"] is None and eth["hl_apr7"] is None and eth["diff_pp"] is None

    btc = by["BTC-PERP"]
    frm = NOW_TS - 7 * 86400
    n7 = sum(1 for k in range(30) if frm <= T0 - k * IV <= NOW_TS)
    assert btc["apr7"] == pytest.approx(n7 * 0.0001 * 365 / 7)
    assert btc["apr30"] is None and btc["cum"]["30"][2] == 1
    assert btc["cum"]["all"][2] == 0 and btc["cum"]["all"][0] == pytest.approx(30 * 0.0001) and btc["cum"]["all"][4] == 10.0
    assert btc["cum"]["7"][2] == 0 and btc["cum"]["7"][4] == 7.0 and len(btc["cum"]["7"]) == 5
    assert btc["n_pay"] == 30 and btc["n_missing"] == 0
    assert btc["mark"] == pytest.approx(80433.95, rel=1e-5) and btc["mark"] == 80433.9     # 行の float は有効数字 6 桁
    assert btc["data_start"] == "2026-10-15" and btc["category"] == "crypto"
    assert set(btc["cum"]) == {"7", "30", "90", "180", "365", "all"}

    coin = by["COIN-PERP"]
    assert coin["category"] == "equity"                                       # 名前の Inc. で株式
    assert coin["n_pay"] == 0 and coin["cum"] == {k: None for k in ("7", "30", "90", "180", "365", "all")}
    assert coin["last_rate"] is None and coin["last_status"] is None and coin["apr7"] is None
    assert coin["cur_rate"] == 0.0 and coin["cur_apr"] == 0.0
    assert coin["data_start"] == "2026-10-25"                                 # 支払が無ければ観測ログの日付
    assert by["AGLD-PERP"]["category"] == "crypto"                            # 上書き(名前の Gold はコモディティではない)
    assert "NVDA-PERP" not in by                                              # funding の無い銘柄は出さない

    tick = out / "data" / "tickers"
    assert sorted(p.name for p in tick.iterdir()) == ["AGLD-PERP.json", "BTC-PERP.json", "COIN-PERP.json", "ETH-PERP.json"]
    eth_j = json.loads((tick / "ETH-PERP.json").read_text())
    assert eth_j == {"ticker": "ETH-PERP", "interval": IV, "payments": [
        [T0 - 3 * IV, None, "m"], [T0 - 2 * IV, 0.00006, "e"], [T0 - IV, 0.00006, "e"], [T0, 0.00006, "e"]]}
    btc_j = json.loads((tick / "BTC-PERP.json").read_text())
    assert len(btc_j["payments"]) == 30 and [p[0] for p in btc_j["payments"]] == sorted(p[0] for p in btc_j["payments"])
    assert {p[2] for p in btc_j["payments"]} == {"e"}
    assert json.loads((tick / "COIN-PERP.json").read_text())["payments"] == []

    swaps = _read(out, "swaps.json")
    assert swaps["generated"] == fmt_iso(NOW) and swaps["snap_ts"] == RUN3 and len(swaps["rows"]) == 1
    assert all(r["listed"] is True for r in rows)
    sw = swaps["rows"][0]
    assert sw["ticker"] == "COPP-SWAP" and sw["name"] == "Swap on Copper Spot" and sw["mark"] == 6.566
    assert sw["long_oi"] == 922408.55 and sw["short_oi"] == 1157564.13 and sw["vol24"] == 0.0
    assert sw["interval"] == 86400 and sw["next_ts"] is None and sw["snap_ts"] == RUN3
    assert sw["last"] == {"ann_long": 0.1955, "ann_short": 0.1955, "int_long": 0.0005431, "int_short": 0.0005431}
    assert sw["next"] is None
    # compact JSON
    assert (out / "data" / "summary.json").read_text(encoding="utf-8").count("\n") == 1


def test_build_hl_comparison_computes_diff_in_percentage_points(tmp_path):
    root = _project(tmp_path, hl=True)
    out = tmp_path / "site"
    build(root, out, now=NOW)
    summary = _read(out, "summary.json")
    assert summary["hl_fetched"] == "2026-10-25T00:30:00Z"
    by = {r["ticker"]: r for r in summary["rows"]}
    btc = by["BTC-PERP"]
    assert btc["hl_coin"] == "BTC" and btc["hl_apr7"] == 0.10
    assert btc["diff_pp"] == pytest.approx((btc["apr7"] - 0.10) * 100, rel=1e-5)
    assert btc["diff_pp"] == pytest.approx(0.95, rel=1e-5)                    # 21 回 × 0.0001 × 365/7 = 10.95% − 10% = 0.95 pt
    assert by["ETH-PERP"]["hl_coin"] is None and by["ETH-PERP"]["diff_pp"] is None


def test_build_hl_mapping_without_apr7_gives_no_diff(tmp_path):
    root = _project(tmp_path, hl=True)
    (root / "config" / "hl_mapping.csv").write_text("ticker,hl_coin,note\nETH-PERP,ETH,本体\nBTC-PERP,BTC,本体\n", encoding="utf-8")
    build(root, tmp_path / "site", now=NOW)
    by = {r["ticker"]: r for r in _read(tmp_path / "site", "summary.json")["rows"]}
    assert by["ETH-PERP"]["hl_coin"] == "ETH" and by["ETH-PERP"]["hl_apr7"] is None and by["ETH-PERP"]["diff_pp"] is None   # HL 側に無い
    assert by["BTC-PERP"]["diff_pp"] is not None


@pytest.mark.parametrize("today,expected_warn,expected_n,expected_checked", [
    ({"n": 15, "checked": 20}, True, 15, 20),
    ({"n": 5, "checked": 20}, False, 5, 20),
    ({"n": 10, "checked": 20}, False, 10, 20),          # ちょうど半分は超えていない
    ({"n": 15, "checked": 19}, False, 15, 19),          # 検査数が足りない
    ({"n": 0, "checked": 0}, False, 0, 0),
    (25, False, 25, 0),                                 # 旧形式(整数)も読める
])
def test_build_anomaly_warn_in_estimated_mode_uses_ratio(tmp_path, today, expected_warn, expected_n, expected_checked):
    root = _project(tmp_path, meta_extra={"anomalies_by_day": {"2026-10-25": today, "2026-10-24": {"n": 99, "checked": 99}}})
    build(root, tmp_path / "site", now=NOW)
    s = _read(tmp_path / "site", "summary.json")
    assert s["anomaly_warn"] is expected_warn
    assert s["anomalies_today"] == expected_n and s["anomalies_checked_today"] == expected_checked


@pytest.mark.parametrize("today,expected_warn", [({"n": 21, "checked": 0}, True), ({"n": 20, "checked": 0}, False), (21, True), ({"n": 21, "checked": 100}, True)])
def test_build_anomaly_warn_in_confirmed_mode_uses_count(tmp_path, today, expected_warn):
    root = _project(tmp_path, funding_mode="confirmed", meta_extra={"anomalies_by_day": {"2026-10-25": today}})
    build(root, tmp_path / "site", now=NOW)
    s = _read(tmp_path / "site", "summary.json")
    assert s["anomaly_warn"] is expected_warn and s["funding_mode"] == "confirmed"


def test_build_anomaly_thresholds_come_from_settings(tmp_path):
    root = _project(tmp_path, meta_extra={"anomalies_by_day": {"2026-10-25": {"n": 3, "checked": 5}}},
                    settings_extra={"anomaly_warn_ratio": 0.2, "anomaly_warn_min_checked": 5})
    build(root, tmp_path / "site", now=NOW)
    s = _read(tmp_path / "site", "summary.json")
    assert s["anomaly_warn"] is True and s["anomaly_warn_ratio"] == 0.2


def test_build_without_meta_or_settings_still_works(tmp_path):
    root = _project(tmp_path)
    (root / "data" / "meta.json").unlink()
    (root / "config" / "settings.json").unlink()
    res = build(root, tmp_path / "site", now=NOW)
    s = _read(tmp_path / "site", "summary.json")
    assert s["stale_hours"] is None and res["stale_hours"] is None and s["last_success"] is None
    assert s["funding_mode"] is None and s["anomaly_warn"] is False and s["n_tickers"] == 4


def test_build_falls_back_to_snapshot_and_obs_without_cache(tmp_path):
    root = _project(tmp_path, cache=False)
    append_csv(root / "data" / "snapshots" / "2026-10-25.csv", SNAP_HEADER, [
        {"snap_ts": "2026-10-25T00:01:00Z", "ticker": "BTC-PERP", "contract_type": "perpetual", "mark_price": "80000", "long_oi": "100", "short_oi": "50", "volume_24h": "7", "base_spread_bps": "1.10"},
        {"snap_ts": "2026-10-24T23:01:00Z", "ticker": "BTC-PERP", "contract_type": "perpetual", "mark_price": "1", "long_oi": "1", "short_oi": "1", "volume_24h": "1", "base_spread_bps": "1"},
        {"snap_ts": "2026-10-25T00:01:00Z", "ticker": "COPP-SWAP", "contract_type": "swap", "mark_price": "6.5", "long_oi": "1", "short_oi": "1", "volume_24h": "0", "base_spread_bps": ""},
    ])
    save_json(root / "data" / "names.json", {"BTC-PERP": "Bitcoin", "ETH-PERP": "Ethereum"}, compact=True)
    build(root, tmp_path / "site", now=NOW)
    s = _read(tmp_path / "site", "summary.json")
    by = {r["ticker"]: r for r in s["rows"]}
    assert set(by) == {"BTC-PERP", "ETH-PERP", "COIN-PERP", "AGLD-PERP"}      # 観測ログにある銘柄
    assert by["BTC-PERP"]["mark"] == 80000.0 and by["BTC-PERP"]["oi"] == 150.0 and by["BTC-PERP"]["name"] == "Bitcoin"   # 最新のスナップショットのみ
    assert by["ETH-PERP"]["name"] == "Ethereum" and by["ETH-PERP"]["oi"] == 0.0 and by["ETH-PERP"]["long_share"] is None
    assert by["COIN-PERP"]["name"] == "COIN-PERP"                             # 名前が分からなければティッカー
    assert by["BTC-PERP"]["cur_rate"] == 0.0001 and by["BTC-PERP"]["interval"] == IV
    assert s["rows"][0]["ticker"] == "BTC-PERP"


def test_build_removes_stale_ticker_files_and_is_repeatable(tmp_path):
    root = _project(tmp_path)
    out = tmp_path / "site"
    (out / "data" / "tickers").mkdir(parents=True)
    (out / "data" / "tickers" / "OLD-PERP.json").write_text("{}")
    build(root, out, now=NOW)
    assert not (out / "data" / "tickers" / "OLD-PERP.json").exists()
    first = (out / "data" / "summary.json").read_text()
    build(root, out, now=NOW)
    assert (out / "data" / "summary.json").read_text() == first


def test_build_with_interval_only_from_funding_rows(tmp_path):
    # .cache の銘柄に interval_seconds が無くても、確定 FR の間隔から補う
    root = _project(tmp_path)
    cache = json.loads((root / ".cache" / "latest_stats.json").read_text())
    for p in cache["perps"]:
        if p["ticker"] == "BTC-PERP":
            p["interval_seconds"] = None
    save_json(root / ".cache" / "latest_stats.json", cache, compact=True)
    build(root, tmp_path / "site", now=NOW)
    by = {r["ticker"]: r for r in _read(tmp_path / "site", "summary.json")["rows"]}
    assert by["BTC-PERP"]["interval"] == IV and by["BTC-PERP"]["n_pay"] == 30


@pytest.mark.parametrize("v,expected", [
    (80433.9531236734, 80434.0), (80433.95, 80433.9), (0.0000622612345, 0.0000622612), (0.1095, 0.1095), (0.0, 0.0), (-25.001234567, -25.0012),
    (123456789.0, 123457000.0), (5, 5), ("x", "x"), (None, None),
])
def test_r_rounds_floats_to_6_significant_digits(v, expected):
    assert _r(v) == expected


def test_r_recurses_into_dicts_and_lists_and_keeps_nan():
    out = _r({"a": [1.23456789, {"b": 9.87654321}], "c": "s", "d": 7})
    assert out == {"a": [1.23457, {"b": 9.87654}], "c": "s", "d": 7}
    assert _r(1.23456789, sig=3) == 1.23
    nan = _r(float("nan"))
    assert nan != nan


def test_build_skips_tickers_unsafe_for_file_names(tmp_path):
    root = _project(tmp_path)
    cache = json.loads((root / ".cache" / "latest_stats.json").read_text())
    cache["perps"].append(_cache_perp("A/B-PERP", "Slash", "1", "1"))
    save_json(root / ".cache" / "latest_stats.json", cache, compact=True)
    build(root, tmp_path / "site", now=NOW)
    s = _read(tmp_path / "site", "summary.json")
    assert "A/B-PERP" not in {r["ticker"] for r in s["rows"]} and s["n_tickers"] == 4


# ---------------------------------------------------------------- cum_stat: 切り詰めた窓は「支払が覆う期間」で年率換算

@pytest.mark.parametrize("n", [1, 2, 3, 9, 30])
def test_cum_stat_partial_apr_equals_annualized_constant_rate(n):
    pays = payments_8h(n, rate=0.0002)
    for days in (7, 30, None):
        s = cum_stat(pays, NOW_TS, days, IV)
        if days is not None and n * IV >= days * 86400 + IV:
            continue   # 窓が揃っている場合は別のテスト
        assert s["n"] == n and s["days"] == round(n * IV / 86400, 2)
        assert s["apr"] == pytest.approx(annualize(0.0002, IV)), (n, days)    # 支払 2 回で 2 倍に跳ねない
        assert s["partial"] is (days is not None)


def test_cum_stat_partial_gap_lowers_apr_over_the_covered_span():
    pays = [(T0 - 2 * IV, 0.0003, "estimated"), (T0 - IV, None, "missing"), (T0, 0.0003, "estimated")]
    s = cum_stat(pays, NOW_TS, 7, IV)
    assert s["n"] == 2 and s["days"] == 1.0 and s["sum"] == pytest.approx(0.0006)
    assert s["apr"] == pytest.approx(annualize(0.0003, IV) * 2 / 3)


def test_cum_stat_non_clamped_window_still_uses_elapsed_time():
    s = cum_stat(payments_8h(30), NOW_TS, 7, IV)
    assert s["partial"] is False and s["days"] == 7.0
    assert s["apr"] == pytest.approx(s["sum"] * 365 / 7)


def test_cum_stat_clamped_span_ends_at_last_payment_before_now():
    pays = payments_8h(3) + [(T0 + IV, 0.0001, "estimated")]          # 最後は未来の支払
    s = cum_stat(pays, T0 + 100, 7, IV)
    assert s["n"] == 3 and s["days"] == 1.0 and s["apr"] == pytest.approx(annualize(0.0001, IV))


# ---------------------------------------------------------------- build(): 最新の取得に無い銘柄、monthly.json

def test_build_keeps_unlisted_tickers_with_history_and_marks_listed_flag(tmp_path):
    root = _project(tmp_path)
    old_nt = T0 - 17 * IV
    append_csv(root / "data" / "funding" / "2026-10.csv", FUNDING_HEADER,
               [_fund_row(old_nt - 3 * IV + k * IV, "OLD-PERP", "0.0002") for k in range(3)]
               + [_fund_row(T0 - 30 * IV, "GONE-PERP", "0.0003", iv=14400)])
    append_csv(root / "data" / "obs" / "2026-10-19.csv", OBS_HEADER, [
        {"obs_ts": "2026-10-19T08:08:00Z", "ticker": "OLD-PERP", "interval_seconds": IV, "next_timestamp": old_nt, "interval_rate": "0.0002", "annualized_rate": "0.219", "kind": "F"}])
    out = tmp_path / "site"
    res = build(root, out, now=NOW)
    s = _read(out, "summary.json")
    assert res["n_rows"] == s["n_tickers"] == 6
    by = {r["ticker"]: r for r in s["rows"]}
    assert {t for t, r in by.items() if r["listed"]} == {"BTC-PERP", "ETH-PERP", "COIN-PERP", "AGLD-PERP"}
    old = by["OLD-PERP"]
    assert old["listed"] is False and old["last_obs"] == "2026-10-19T08:08:00Z"
    assert old["interval"] == IV and int(old["next_ts"]) == old_nt and old["n_pay"] == 3 and old["oi"] == 0.0
    assert old["cur_rate"] == 0.0002 and old["name"] == "OLD-PERP" and old["category"] == "crypto"
    assert old["last_pay_ts"] == old_nt - IV and old["mark"] is None
    gone = by["GONE-PERP"]
    assert gone["listed"] is False and gone["interval"] == 14400 and gone["next_ts"] is None and gone["cur_rate"] is None
    assert gone["last_obs"] == day_str(from_unix(T0 - 30 * IV)) + "T00:00:00Z"
    assert gone["n_pay"] == 1 and gone["last_rate"] == 0.0003
    assert [r["ticker"] for r in s["rows"]][:4] == ["ETH-PERP", "BTC-PERP", "COIN-PERP", "AGLD-PERP"]    # OI 0 の銘柄は末尾
    assert sorted(p.name for p in (out / "data" / "tickers").iterdir()) == sorted(f"{t}.json" for t in by)
    assert json.loads((out / "data" / "tickers" / "GONE-PERP.json").read_text())["payments"] == [[T0 - 30 * IV, 0.0003, "e"]]


def test_build_writes_monthly_sums_excluding_missing(tmp_path):
    root = _project(tmp_path)
    append_csv(root / "data" / "funding" / "2026-09.csv", FUNDING_HEADER, [
        _fund_row(T0 - 91 * IV, "BTC-PERP", None, "missing"), _fund_row(T0 - 90 * IV, "BTC-PERP", "0.0005")])   # 2026-09-25
    out = tmp_path / "site"
    build(root, out, now=NOW)
    m = _read(out, "monthly.json")
    assert m["generated"] == fmt_iso(NOW)
    assert m["months"] == ["2026-09", "2026-10"]
    assert m["rows"]["BTC-PERP"] == {"2026-09": 0.0005, "2026-10": pytest.approx(30 * 0.0001)}
    assert m["rows"]["ETH-PERP"] == {"2026-10": pytest.approx(3 * 0.00006)}                        # missing は入れない
    assert "COIN-PERP" not in m["rows"] and "AGLD-PERP" not in m["rows"]
    assert (out / "data" / "monthly.json").read_text(encoding="utf-8").count("\n") == 1


def test_build_monthly_keeps_only_last_18_months(tmp_path):
    root = _project(tmp_path)
    rows = []
    months = []
    for i in range(24):
        y, mth = divmod(2024 * 12 + 10 + i, 12)          # 2024-11 .. 2026-10
        d = dt.datetime(y, mth + 1, 15, tzinfo=dt.timezone.utc)
        months.append(d.strftime("%Y-%m"))
        rows.append(_fund_row(int(d.timestamp()), "LONG-PERP", "0.001"))
    append_csv(root / "data" / "funding" / "2026-10.csv", FUNDING_HEADER, rows)
    build(root, tmp_path / "site", now=NOW)
    m = _read(tmp_path / "site", "monthly.json")
    assert m["months"] == months[-18:] and len(m["months"]) == 18 and m["months"][0] == "2025-05"
    assert sorted(m["rows"]["LONG-PERP"]) == months[-18:]
    assert all(v == 0.001 for v in m["rows"]["LONG-PERP"].values())
    assert "2024-11" not in m["rows"]["LONG-PERP"]
    by = {r["ticker"]: r for r in _read(tmp_path / "site", "summary.json")["rows"]}
    assert by["LONG-PERP"]["listed"] is False and by["LONG-PERP"]["interval"] == IV


def test_build_monthly_empty_without_funding(tmp_path):
    root = _project(tmp_path)
    (root / "data" / "funding" / "2026-10.csv").unlink()
    build(root, tmp_path / "site", now=NOW)
    m = _read(tmp_path / "site", "monthly.json")
    assert m["months"] == [] and m["rows"] == {}
