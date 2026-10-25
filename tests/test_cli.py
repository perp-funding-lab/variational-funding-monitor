"""cli: 予定時刻の算出とサブコマンドの配線(ネットワークは使わない)。"""
from __future__ import annotations

import csv
import json

import pytest

from vfm import cli
from vfm.cli import _settings, _slot_time
from vfm.store import DataPaths, append_csv, save_json, OBS_HEADER
from vfm.timeutil import fmt_iso, parse_iso, utc_now

SLOTS = "8,18,28,38,48,58"


@pytest.mark.parametrize("now,slots,expected", [
    ("2026-10-25T00:05:00Z", SLOTS, "2026-10-24T23:58:00Z"),   # 前の時の最後のスロット
    ("2026-10-25T00:20:00Z", SLOTS, "2026-10-25T00:18:00Z"),
    ("2026-10-25T13:00:00Z", "13,43", "2026-10-25T12:43:00Z"),
    ("2026-10-25T00:18:00Z", SLOTS, "2026-10-25T00:18:00Z"),   # ちょうど
    ("2026-10-25T00:18:30Z", SLOTS, "2026-10-25T00:18:00Z"),
    ("2026-10-25T00:07:59Z", SLOTS, "2026-10-24T23:58:00Z"),
    ("2026-10-25T00:58:00Z", SLOTS, "2026-10-25T00:58:00Z"),
    ("2026-10-25T13:43:05Z", "43,13", "2026-10-25T13:43:00Z"),  # 並び順は問わない
])
def test_slot_time(now, slots, expected):
    assert fmt_iso(_slot_time(parse_iso(now), slots)) == expected


def test_slot_time_ignores_blank_entries():
    assert fmt_iso(_slot_time(parse_iso("2026-10-25T00:20:00Z"), "8,,18, ")) == "2026-10-25T00:18:00Z"


def test_slot_time_result_is_utc_without_seconds():
    d = _slot_time(parse_iso("2026-10-25T00:20:45Z"), SLOTS)
    assert d.second == 0 and d.microsecond == 0 and d.utcoffset().total_seconds() == 0


def test_settings_missing_file_is_empty(tmp_path):
    assert _settings(tmp_path) == {}
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.json").write_text('{"funding_mode": "estimated"}')
    assert _settings(tmp_path) == {"funding_mode": "estimated"}


def test_main_requires_a_command():
    with pytest.raises(SystemExit):
        cli.main([])


def test_collect_command_passes_slot_scheduled_time_and_trigger(monkeypatch, tmp_path, capsys):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.json").write_text('{"funding_mode": "estimated"}')
    seen = {}

    def fake_run_once(data_root, settings, **kw):
        seen.update(data_root=data_root, settings=settings, **kw)
        return {"http_status": 200, "note": ""}

    import vfm.collect
    monkeypatch.setattr(vfm.collect, "run_once", fake_run_once)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    rc = cli.main(["--root", str(tmp_path), "collect", "--slots", "8,18,28,38,48,58", "--trigger", "worker"])
    assert rc == 0
    assert seen["skip_if_fresh_seconds"] is None                         # 指定しなければ無効
    assert seen["data_root"] == tmp_path.resolve() / "data" and seen["project_root"] == tmp_path.resolve()
    assert seen["settings"] == {"funding_mode": "estimated"} and seen["trigger"] == "worker"
    assert seen["scheduled"] <= seen["now"] and seen["scheduled"].minute in (8, 18, 28, 38, 48, 58)
    assert json.loads(capsys.readouterr().out)["http_status"] == 200


def test_collect_command_explicit_scheduled_and_failure_exit_code(monkeypatch, tmp_path):
    seen = {}

    def fake_run_once(data_root, settings, **kw):
        seen.update(kw)
        return {"http_status": "", "note": "fetch failed: HTTP 500 (attempts 1)"}

    import vfm.collect
    monkeypatch.setattr(vfm.collect, "run_once", fake_run_once)
    rc = cli.main(["--root", str(tmp_path), "collect", "--scheduled", "2026-10-25T00:08:00Z"])
    assert rc == 1
    assert fmt_iso(seen["scheduled"]) == "2026-10-25T00:08:00Z" and seen["trigger"] == "manual"


def test_collect_command_schema_error_exit_code(monkeypatch, tmp_path):
    import vfm.collect
    monkeypatch.setattr(vfm.collect, "run_once", lambda *a, **k: {"http_status": 200, "note": "schema error: not json"})
    assert cli.main(["--root", str(tmp_path), "collect"]) == 1


def test_rebuild_funding_command_writes_csv(tmp_path, capsys):
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    T = 1792886400
    append_csv(paths.obs_dir / "2026-10-24.csv", OBS_HEADER, [
        {"obs_ts": "2026-10-24T23:19:15Z", "ticker": "BTC-PERP", "interval_seconds": 28800, "next_timestamp": T, "interval_rate": "0.0001", "annualized_rate": "0.1095", "kind": "F"},
        {"obs_ts": "2026-10-24T23:31:30Z", "ticker": "BTC-PERP", "interval_seconds": 28800, "next_timestamp": T, "interval_rate": "0.0001", "annualized_rate": "0.1095", "kind": "L"},
    ])
    append_csv(paths.obs_dir / "2026-10-25.csv", OBS_HEADER, [
        {"obs_ts": "2026-10-25T00:09:00Z", "ticker": "BTC-PERP", "interval_seconds": 28800, "next_timestamp": T + 28800, "interval_rate": "0.00012", "annualized_rate": "0.1314", "kind": "F"},
    ])
    out = tmp_path / "out" / "rebuilt.csv"
    assert cli.main(["--root", str(tmp_path), "rebuild-funding", "--mode", "estimated", "--out", str(out)]) == 0
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert [(r["pay_ts"], r["ticker"], r["status"], r["interval_rate"], r["basis_obs_ts"]) for r in rows] == \
        [(str(T), "BTC-PERP", "estimated", "0.0001", "2026-10-24T23:31:30Z")]
    info = json.loads(capsys.readouterr().out)
    assert info["obs_rows"] == 3 and info["funding_rows"] == 1 and info["mode"] == "estimated"

    out2 = tmp_path / "out" / "confirmed.csv"
    assert cli.main(["--root", str(tmp_path), "rebuild-funding", "--mode", "confirmed", "--out", str(out2)]) == 0
    rows2 = list(csv.DictReader(out2.open(encoding="utf-8")))
    assert [(r["pay_ts"], r["status"], r["interval_rate"]) for r in rows2] == [(str(T - 28800), "confirmed", "0.0001"), (str(T), "confirmed", "0.00012")]
    # missing は rate 空欄で書く
    assert all(r["interval_rate"] != "None" for r in rows2)


def test_rebuild_funding_rejects_unknown_mode(tmp_path):
    with pytest.raises(SystemExit):
        cli.main(["--root", str(tmp_path), "rebuild-funding", "--mode", "guess", "--out", str(tmp_path / "x.csv")])


def test_status_command_prints_meta(tmp_path, capsys):
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    save_json(paths.meta_path, {"collection_start": "2026-10-24T23:19:15Z", "last_success_ts": "2026-10-25T00:09:00Z",
                                "last_run_ts": None, "funding_mode": "estimated", "anomalies_by_day": {"2026-10-25": {"n": 0, "checked": 0}}})
    assert cli.main(["--root", str(tmp_path), "status"]) == 0
    out = capsys.readouterr().out
    assert "collection_start: 2026-10-24 23:19 UTC (08:19 JST)" in out
    assert "last_success_ts: 2026-10-25 00:09 UTC (09:09 JST)" in out
    assert "last_run_ts: -" in out
    assert "funding_mode: estimated" in out


def test_archive_command_archives_last_month_and_prunes(tmp_path, capsys):
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    now = utc_now()
    last_month = (now.replace(day=1) - __import__("datetime").timedelta(days=1)).strftime("%Y-%m")
    old_day = (now - __import__("datetime").timedelta(days=40)).strftime("%Y-%m-%d")
    (paths.snap_dir / f"{last_month}-01.csv").write_text("snap_ts,ticker,contract_type,mark_price,long_oi,short_oi,volume_24h,base_spread_bps\nx,BTC-PERP,perpetual,1,2,3,4,5\n")
    (paths.obs_dir / f"{old_day}.csv").write_text("obs_ts,ticker,interval_seconds,next_timestamp,interval_rate,annualized_rate,kind\n")
    assert cli.main(["--root", str(tmp_path), "archive"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["archived"] == [str(paths.archive_dir / "snapshots" / f"{last_month}.csv.gz")]
    assert info["pruned"] == [str(paths.obs_dir / f"{old_day}.csv")]
    assert not (paths.snap_dir / f"{last_month}-01.csv").exists()


def test_build_command_uses_root_site_by_default(monkeypatch, tmp_path, capsys):
    seen = {}
    import vfm.build_site
    monkeypatch.setattr(vfm.build_site, "build", lambda root, out: seen.update(root=root, out=out) or {"n_rows": 0})
    assert cli.main(["--root", str(tmp_path), "build"]) == 0
    assert seen["out"] == tmp_path.resolve() / "site" and seen["root"] == tmp_path.resolve()
    assert cli.main(["--root", str(tmp_path), "build", "--out", str(tmp_path / "x")]) == 0
    assert seen["out"] == tmp_path / "x"


def test_collect_command_skip_if_fresh_flag_and_github_output(monkeypatch, tmp_path, capsys):
    seen = {}
    returns = {"value": {"http_status": 200, "note": ""}}

    def fake_run_once(data_root, settings, **kw):
        seen.update(kw)
        return returns["value"]

    import vfm.collect
    monkeypatch.setattr(vfm.collect, "run_once", fake_run_once)
    gh = tmp_path / "gh_output.txt"
    gh.write_text("existing=1\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_OUTPUT", str(gh))
    assert cli.main(["--root", str(tmp_path), "collect", "--skip-if-fresh", "720"]) == 0
    assert seen["skip_if_fresh_seconds"] == 720
    assert gh.read_text(encoding="utf-8") == "existing=1\nskipped=false\n"
    # 飛ばした回: 終了コード 0、skipped=true
    returns["value"] = {"skipped": True, "last_success_ts": "2026-10-25T00:01:30Z", "age_s": 300, "trigger": "schedule"}
    assert cli.main(["--root", str(tmp_path), "collect", "--skip-if-fresh", "720", "--trigger", "schedule"]) == 0
    assert gh.read_text(encoding="utf-8") == "existing=1\nskipped=false\nskipped=true\n"
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == returns["value"]
    # 0 は無効(None)
    returns["value"] = {"http_status": 200, "note": ""}
    assert cli.main(["--root", str(tmp_path), "collect", "--skip-if-fresh", "0"]) == 0
    assert seen["skip_if_fresh_seconds"] is None
    # 失敗した回も skipped=false を書いて 1 を返す
    returns["value"] = {"http_status": 500, "note": "fetch failed: HTTP 500 (attempts 1)"}
    assert cli.main(["--root", str(tmp_path), "collect"]) == 1
    assert gh.read_text(encoding="utf-8").endswith("skipped=true\nskipped=false\nskipped=false\n")


def test_collect_command_without_github_output_writes_no_file(monkeypatch, tmp_path):
    import vfm.collect
    monkeypatch.setattr(vfm.collect, "run_once", lambda *a, **k: {"http_status": 200, "note": ""})
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    before = sorted(p.name for p in tmp_path.iterdir())
    assert cli.main(["--root", str(tmp_path), "collect"]) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_collect_command_rejects_non_integer_skip_if_fresh(tmp_path):
    with pytest.raises(SystemExit):
        cli.main(["--root", str(tmp_path), "collect", "--skip-if-fresh", "soon"])
