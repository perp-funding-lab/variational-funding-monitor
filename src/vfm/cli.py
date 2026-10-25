"""コマンド: python -m vfm <collect|build|hl-update|rebuild-funding|archive|probe-mode>"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import sys
from pathlib import Path

from .timeutil import fmt_both, fmt_iso, parse_iso, utc_now


def _root(args) -> Path:
    return Path(args.root).resolve()


def _settings(root: Path) -> dict:
    p = root / "config" / "settings.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _slot_time(now: _dt.datetime, slots: str) -> _dt.datetime:
    """'8,18,28,38,48,58' のような分の一覧から、now 以前で最も近い予定時刻を返す。"""
    mins = sorted(int(x) for x in slots.split(",") if x.strip() != "")
    base = now.replace(second=0, microsecond=0)
    for h_off in (0, 1):
        hour = base - _dt.timedelta(hours=h_off)
        for m in reversed(mins):
            cand = hour.replace(minute=m)
            if cand <= now:
                return cand
    return base


def cmd_collect(args) -> int:
    import os
    from .collect import run_once
    root = _root(args)
    now = utc_now()
    scheduled = parse_iso(args.scheduled) if args.scheduled else (_slot_time(now, args.slots) if args.slots else None)
    row = run_once(root / "data", _settings(root), now=now, scheduled=scheduled, trigger=args.trigger, project_root=root,
                   skip_if_fresh_seconds=args.skip_if_fresh or None)
    print(json.dumps(row, ensure_ascii=False))
    skipped = bool(row.get("skipped"))
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"skipped={'true' if skipped else 'false'}\n")
    if skipped:
        return 0
    ok = str(row.get("http_status")) == "200" and not str(row.get("note", "")).startswith(("fetch failed", "schema error"))
    return 0 if ok else 1


def cmd_build(args) -> int:
    from .build_site import build
    root = _root(args)
    out = Path(args.out) if args.out else root / "site"
    res = build(root, out)
    print(json.dumps(res, ensure_ascii=False))
    return 0


def cmd_hl_update(args) -> int:
    from .hl import fetch_7d, fetch_universe, update_mapping
    from .store import DataPaths, load_json, save_json
    root = _root(args)
    paths = DataPaths(root / "data")
    paths.ensure()
    universe = fetch_universe()
    save_json(paths.hl_dir / "hl_universe.json", universe, compact=True)
    latest = load_json(root / ".cache/latest_stats.json", None)
    tickers = [p["ticker"] for p in latest["perps"]] if latest else []
    if not tickers:
        from .store import load_last_obs
        tickers = list(load_last_obs(paths).keys())
    mapping, added = update_mapping(root / "config" / "hl_mapping.csv", tickers, universe)
    coins = sorted({m["hl_coin"] for m in mapping.values() if m["hl_coin"]})
    if args.limit:
        coins = coins[: args.limit]
    now = utc_now()
    res = fetch_7d(coins, int(now.timestamp() * 1000))
    trunc = res.pop("_truncated", None)
    save_json(paths.hl_path, {"fetched": fmt_iso(now), "n_coins": len(res), "truncated": trunc, "coins": res}, compact=True)
    print(json.dumps({"mapped": len(coins), "fetched": len(res), "added_mapping_rows": len(added), "truncated": trunc}, ensure_ascii=False))
    return 0


def cmd_rebuild(args) -> int:
    from .funding import rebuild_from_obs
    from .store import FUNDING_HEADER, DataPaths, iter_csv_files, read_csv
    root = _root(args)
    paths = DataPaths(root / "data")
    rows = []
    for p in iter_csv_files(paths.obs_dir):
        rows.extend(read_csv(p))
    out_rows = rebuild_from_obs(rows, args.mode)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FUNDING_HEADER, lineterminator="\n")
        w.writeheader()
        for r in out_rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in FUNDING_HEADER})
    print(json.dumps({"mode": args.mode, "obs_rows": len(rows), "funding_rows": len(out_rows), "out": str(out)}, ensure_ascii=False))
    return 0


def cmd_archive(args) -> int:
    from .store import DataPaths, archive_snapshots, prune_obs
    root = _root(args)
    paths = DataPaths(root / "data")
    now = utc_now()
    a = archive_snapshots(paths, now)
    b = prune_obs(paths, now)
    print(json.dumps({"archived": [str(p) for p in a], "pruned": [str(p) for p in b]}, ensure_ascii=False))
    return 0


def cmd_status(args) -> int:
    from .store import DataPaths, load_json
    root = _root(args)
    paths = DataPaths(root / "data")
    meta = load_json(paths.meta_path, {})
    for k in ("collection_start", "last_success_ts", "last_run_ts"):
        v = meta.get(k)
        print(f"{k}: {fmt_both(parse_iso(v)) if v else '-'}")
    print("funding_mode:", meta.get("funding_mode"), " anomalies_by_day:", meta.get("anomalies_by_day"))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="vfm")
    ap.add_argument("--root", default=".", help="リポジトリのルート(既定: カレント)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect", help="stats を 1 回取得して記録する")
    c.add_argument("--trigger", default="manual")
    c.add_argument("--scheduled", help="予定時刻(ISO、Worker からの dispatch に付ける)")
    c.add_argument("--slots", help="予定時刻を分の一覧から求める(例 '8,18,28,38,48,58')")
    c.add_argument("--skip-if-fresh", type=int, default=0, metavar="SECONDS",
                   help="直前の成功がこの秒数以内なら何もしない(保険の schedule 用)")
    c.set_defaults(fn=cmd_collect)
    b = sub.add_parser("build", help="site/data/*.json を作る")
    b.add_argument("--out")
    b.set_defaults(fn=cmd_build)
    h = sub.add_parser("hl-update", help="HL の対応表と 7 日年率を更新する")
    h.add_argument("--limit", type=int, default=0)
    h.set_defaults(fn=cmd_hl_update)
    r = sub.add_parser("rebuild-funding", help="観測ログから確定 FR を作り直す")
    r.add_argument("--mode", required=True, choices=["confirmed", "estimated"])
    r.add_argument("--out", required=True)
    r.set_defaults(fn=cmd_rebuild)
    a = sub.add_parser("archive", help="月締めと観測ログの保持期間の処理だけ行う")
    a.set_defaults(fn=cmd_archive)
    s = sub.add_parser("status")
    s.set_defaults(fn=cmd_status)
    args = ap.parse_args(argv)
    return args.fn(args)
