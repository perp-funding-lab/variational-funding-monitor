"""store: CSV/JSON の読み書き、直近の観測・支払の復元、保持期間、月次アーカイブ。"""
from __future__ import annotations

import datetime as dt
import gzip
import json

import pytest

from vfm import store
from vfm.store import (FUNDING_HEADER, OBS_HEADER, SNAP_HEADER, DataPaths, append_csv, archive_snapshots, default_meta,
                       iter_csv_files, load_all_funding, load_json, load_last_obs, load_last_pay, prune_obs, read_csv, save_json)
from vfm.timeutil import parse_iso

T0 = 1792886400


def obs(ts, ticker, nt, kind, rate="0.0001", iv=28800):
    return {"obs_ts": ts, "ticker": ticker, "interval_seconds": iv, "next_timestamp": nt, "interval_rate": rate, "annualized_rate": "", "kind": kind}


def fund(pay_ts, ticker, status="estimated", rate="0.0001"):
    return {"pay_ts": pay_ts, "ticker": ticker, "interval_seconds": 28800, "interval_rate": rate if status != "missing" else None,
            "annualized_rate": None, "status": status, "basis_obs_ts": None}


def snap(ts, ticker):
    return {"snap_ts": ts, "ticker": ticker, "contract_type": "perpetual", "mark_price": "1", "long_oi": "2", "short_oi": "3", "volume_24h": "4", "base_spread_bps": "5"}


def test_append_csv_writes_header_once_and_appends(tmp_path):
    p = tmp_path / "a.csv"
    assert append_csv(p, ["a", "b"], []) == 0
    assert not p.exists()                                   # 空なら何も作らない
    assert append_csv(p, ["a", "b"], [{"a": 1, "b": None}]) == 1
    assert append_csv(p, ["a", "b"], [{"a": 2, "b": "x", "extra": 9}]) == 1   # 余分な列は捨てる
    assert p.read_text(encoding="utf-8") == "a,b\n1,\n2,x\n"
    assert read_csv(p) == [{"a": "1", "b": ""}, {"a": "2", "b": "x"}]


def test_append_csv_adds_header_to_empty_existing_file_and_creates_dirs(tmp_path):
    p = tmp_path / "deep" / "dir" / "a.csv"
    p.parent.mkdir(parents=True)
    p.write_text("")
    append_csv(p, ["a"], [{"a": 1}])
    assert p.read_text() == "a\n1\n"
    q = tmp_path / "new" / "sub" / "b.csv"
    append_csv(q, ["a"], [{"a": 1}])
    assert q.read_text() == "a\n1\n"


def test_append_csv_quotes_commas_in_values(tmp_path):
    p = tmp_path / "a.csv"
    append_csv(p, ["a", "b"], [{"a": "x,y", "b": "日本語"}])
    assert read_csv(p) == [{"a": "x,y", "b": "日本語"}]


def test_read_csv_reads_plain_and_gzip_and_missing(tmp_path):
    plain = tmp_path / "x.csv"
    plain.write_text("a,b\n1,2\n", encoding="utf-8")
    gz = tmp_path / "y.csv.gz"
    with gzip.open(gz, "wt", encoding="utf-8") as f:
        f.write("a,b\n3,4\n5,6\n")
    assert read_csv(plain) == [{"a": "1", "b": "2"}]
    assert read_csv(gz) == [{"a": "3", "b": "4"}, {"a": "5", "b": "6"}]
    assert read_csv(tmp_path / "none.csv") == []


def test_iter_csv_files_filters_names_and_orders(tmp_path):
    for n in ("2026-10-24.csv", "2026-10.csv", "2026-09.csv.gz", "2026-09-30.csv", "junk.csv", "notes.txt", "2026-1.csv"):
        (tmp_path / n).write_text("")
    (tmp_path / "2026-08-01.csv").mkdir()      # ディレクトリも名前が合えば拾う(実運用では無い)
    names = [p.name for p in iter_csv_files(tmp_path)]
    assert names == ["2026-08-01.csv", "2026-09-30.csv", "2026-09.csv.gz", "2026-10-24.csv", "2026-10.csv"]
    assert [p.name for p in iter_csv_files(tmp_path, newest_first=True)] == names[::-1]
    assert [p.name for p in iter_csv_files(tmp_path, pattern=r"^\d{4}-\d{2}-\d{2}\.csv$")] == ["2026-08-01.csv", "2026-09-30.csv", "2026-10-24.csv"]
    assert iter_csv_files(tmp_path / "missing") == []


def test_load_last_obs_prefers_greatest_obs_ts_even_when_L_row_is_appended_later(tmp_path):
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    # 新窓の F 行(05:00)が先に書かれ、その後に旧窓の L 行(04:50)が追記される
    append_csv(paths.obs_dir / "2026-10-24.csv", OBS_HEADER, [
        obs("2026-10-24T04:00:00Z", "X-PERP", 1, "F"),
        obs("2026-10-24T05:00:00Z", "X-PERP", 2, "F", rate="0.0002"),
        obs("2026-10-24T04:50:00Z", "X-PERP", 1, "L"),
    ])
    last = load_last_obs(paths)
    assert last["X-PERP"]["obs_ts"] == "2026-10-24T05:00:00Z"
    assert last["X-PERP"]["kind"] == "F" and last["X-PERP"]["next_timestamp"] == "2" and last["X-PERP"]["interval_rate"] == "0.0002"


def test_load_last_obs_across_day_files(tmp_path):
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    append_csv(paths.obs_dir / "2026-10-24.csv", OBS_HEADER, [obs("2026-10-24T23:19:15Z", "X-PERP", T0, "F"), obs("2026-10-24T23:19:15Z", "Y-PERP", T0, "F")])
    # 00:09 の run: L 行は前日のファイル、F 行は当日のファイルへ
    append_csv(paths.obs_dir / "2026-10-25.csv", OBS_HEADER, [obs("2026-10-25T00:09:00Z", "X-PERP", T0 + 28800, "F")])
    append_csv(paths.obs_dir / "2026-10-24.csv", OBS_HEADER, [obs("2026-10-24T23:31:30Z", "X-PERP", T0, "L")])
    last = load_last_obs(paths)
    assert last["X-PERP"]["obs_ts"] == "2026-10-25T00:09:00Z" and last["X-PERP"]["kind"] == "F"
    assert last["Y-PERP"]["obs_ts"] == "2026-10-24T23:19:15Z"
    # max_files で読むファイルを絞れる(新しい方から)
    assert set(load_last_obs(paths, max_files=1)) == {"X-PERP"}
    assert load_last_obs(DataPaths(tmp_path / "empty")) == {}


def test_load_last_pay_picks_max_pay_ts_within_recent_files(tmp_path):
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    append_csv(paths.funding_dir / "2026-08.csv", FUNDING_HEADER, [fund(50, "Z-PERP")])
    append_csv(paths.funding_dir / "2026-09.csv", FUNDING_HEADER, [fund(100, "X-PERP"), fund(90, "Y-PERP", "missing")])
    append_csv(paths.funding_dir / "2026-10.csv", FUNDING_HEADER, [fund(200, "X-PERP"), fund(150, "X-PERP", "missing")])
    lp = load_last_pay(paths)
    assert lp == {"X-PERP": 200, "Y-PERP": 90}         # 直近 2 か月のみ(Z は 3 か月前)。missing も数える
    assert load_last_pay(paths, max_files=1) == {"X-PERP": 200}
    assert load_last_pay(paths, max_files=5) == {"X-PERP": 200, "Y-PERP": 90, "Z-PERP": 50}
    assert load_last_pay(DataPaths(tmp_path / "empty")) == {}


def test_load_all_funding_concatenates_in_file_order(tmp_path):
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    append_csv(paths.funding_dir / "2026-10.csv", FUNDING_HEADER, [fund(200, "X-PERP")])
    append_csv(paths.funding_dir / "2026-09.csv", FUNDING_HEADER, [fund(100, "X-PERP")])
    assert [r["pay_ts"] for r in load_all_funding(paths)] == ["100", "200"]


def test_prune_obs_deletes_files_older_than_30_days_and_keeps_the_rest(tmp_path):
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    for d in ("2026-09-24", "2026-09-25", "2026-10-24", "2026-10-25"):
        (paths.obs_dir / f"{d}.csv").write_text("x")
    (paths.obs_dir / "README.txt").write_text("keep")
    removed = prune_obs(paths, parse_iso("2026-10-25T00:09:00Z"))
    assert [p.name for p in removed] == ["2026-09-24.csv"]
    assert sorted(p.name for p in paths.obs_dir.iterdir()) == ["2026-09-25.csv", "2026-10-24.csv", "2026-10-25.csv", "README.txt"]
    assert store.OBS_KEEP_DAYS == 30
    assert [p.name for p in prune_obs(paths, parse_iso("2026-10-25T00:09:00Z"), keep_days=1)] == ["2026-09-25.csv"]
    assert prune_obs(DataPaths(tmp_path / "empty"), parse_iso("2026-10-25T00:09:00Z")) == []


def test_archive_snapshots_merges_last_month_into_one_gzip_with_single_header(tmp_path):
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    append_csv(paths.snap_dir / "2026-09-29.csv", SNAP_HEADER, [snap("2026-09-29T00:00:00Z", "A"), snap("2026-09-29T01:00:00Z", "B")])
    append_csv(paths.snap_dir / "2026-09-30.csv", SNAP_HEADER, [snap("2026-09-30T00:00:00Z", "A")])
    append_csv(paths.snap_dir / "2026-10-01.csv", SNAP_HEADER, [snap("2026-10-01T00:00:00Z", "A")])
    append_csv(paths.snap_dir / "2026-10-24.csv", SNAP_HEADER, [snap("2026-10-24T00:00:00Z", "A")])
    out = archive_snapshots(paths, parse_iso("2026-10-25T00:09:00Z"))
    dest = paths.archive_dir / "snapshots" / "2026-09.csv.gz"
    assert out == [dest]
    with gzip.open(dest, "rt", encoding="utf-8") as f:
        text = f.read()
    lines = text.splitlines()
    assert lines[0] == ",".join(SNAP_HEADER)
    assert lines.count(",".join(SNAP_HEADER)) == 1
    assert len(lines) == 4
    rows = read_csv(dest)
    assert [(r["snap_ts"], r["ticker"]) for r in rows] == [("2026-09-29T00:00:00Z", "A"), ("2026-09-29T01:00:00Z", "B"), ("2026-09-30T00:00:00Z", "A")]
    assert sorted(p.name for p in paths.snap_dir.iterdir()) == ["2026-10-01.csv", "2026-10-24.csv"]   # 今月は触らない


def test_archive_snapshots_handles_several_months_and_existing_archive(tmp_path):
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    append_csv(paths.snap_dir / "2026-08-31.csv", SNAP_HEADER, [snap("2026-08-31T00:00:00Z", "A")])
    append_csv(paths.snap_dir / "2026-09-30.csv", SNAP_HEADER, [snap("2026-09-30T00:00:00Z", "B")])
    dest9 = paths.archive_dir / "snapshots" / "2026-09.csv.gz"
    with gzip.open(dest9, "wt", encoding="utf-8") as f:
        f.write(",".join(SNAP_HEADER) + "\n" + "2026-09-01T00:00:00Z,A,perpetual,1,2,3,4,5\n")
    out = archive_snapshots(paths, parse_iso("2026-10-25T00:09:00Z"))
    assert out == [paths.archive_dir / "snapshots" / "2026-08.csv.gz", dest9]
    rows9 = read_csv(dest9)
    assert [(r["snap_ts"], r["ticker"]) for r in rows9] == [("2026-09-01T00:00:00Z", "A"), ("2026-09-30T00:00:00Z", "B")]
    with gzip.open(dest9, "rt", encoding="utf-8") as f:
        assert f.read().count("snap_ts,") == 1
    assert list(paths.snap_dir.iterdir()) == []


def test_archive_snapshots_nothing_to_do(tmp_path):
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    append_csv(paths.snap_dir / "2026-10-24.csv", SNAP_HEADER, [snap("2026-10-24T00:00:00Z", "A")])
    assert archive_snapshots(paths, parse_iso("2026-10-25T00:09:00Z")) == []
    assert (paths.snap_dir / "2026-10-24.csv").exists()


def test_save_json_compact_is_sorted_single_line_utf8(tmp_path):
    p = tmp_path / "x.json"
    save_json(p, {"b": 1, "a": [1, 2], "c": "日本", "d": None, "e": 1.5}, compact=True)
    assert p.read_text(encoding="utf-8") == '{"a":[1,2],"b":1,"c":"日本","d":null,"e":1.5}\n'
    assert not p.with_suffix(".json.tmp").exists()
    assert load_json(p, None) == {"a": [1, 2], "b": 1, "c": "日本", "d": None, "e": 1.5}


def test_save_json_pretty_is_indented_and_sorted(tmp_path):
    p = tmp_path / "sub" / "x.json"
    save_json(p, {"b": 1, "a": 2})
    assert p.read_text(encoding="utf-8") == '{\n "a": 2,\n "b": 1\n}\n'
    save_json(p, {"z": 0})          # 上書き
    assert load_json(p, None) == {"z": 0}


def test_load_json_default_for_missing(tmp_path):
    assert load_json(tmp_path / "none.json", {"d": 1}) == {"d": 1}
    assert load_json(tmp_path / "none.json", None) is None


def test_default_meta_shape():
    m = default_meta(parse_iso("2026-10-24T23:19:15Z"))
    assert m == {"collection_start": "2026-10-24T23:19:15Z", "last_run_ts": None, "last_success_ts": None, "funding_mode": None, "anomalies_by_day": {}}


def test_data_paths_layout(tmp_path):
    paths = DataPaths(str(tmp_path / "data"))
    d = parse_iso("2026-10-24T23:19:15Z")
    assert paths.run_file(d) == tmp_path / "data" / "runs" / "2026-10.csv"
    assert paths.obs_file(d) == tmp_path / "data" / "obs" / "2026-10-24.csv"
    assert paths.funding_file(d) == tmp_path / "data" / "funding" / "2026-10.csv"
    assert paths.snap_file(d) == tmp_path / "data" / "snapshots" / "2026-10-24.csv"
    assert paths.meta_path == tmp_path / "data" / "meta.json"
    assert paths.swaps_path == tmp_path / "data" / "swaps_latest.json"
    assert paths.hl_path == tmp_path / "data" / "hl" / "hl_7d.json"
    paths.ensure()
    for sub in ("runs", "obs", "funding", "snapshots", "archive/snapshots", "hl"):
        assert (tmp_path / "data" / sub).is_dir()
    paths.ensure()      # 2 回目も問題ない


def test_headers_match_design():
    assert store.RUN_HEADER == ["scheduled_ts", "fetch_ts", "delay_s", "http_status", "n_listings", "n_perp", "n_swap",
                                "n_obs_rows", "n_funding_rows", "n_anomalies", "trigger", "note"]
    assert OBS_HEADER == ["obs_ts", "ticker", "interval_seconds", "next_timestamp", "interval_rate", "annualized_rate", "kind"]
    assert FUNDING_HEADER == ["pay_ts", "ticker", "interval_seconds", "interval_rate", "annualized_rate", "status", "basis_obs_ts"]
    assert SNAP_HEADER == ["snap_ts", "ticker", "contract_type", "mark_price", "long_oi", "short_oi", "volume_24h", "base_spread_bps"]
