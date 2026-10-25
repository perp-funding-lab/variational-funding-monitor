"""1 回の収集: 取得 → 観測ログ → 確定 FR → 毎時スナップショット → swap 最新値 → meta → 実行ログ。"""
from __future__ import annotations

import datetime as _dt
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import fetch as _fetch
from .funding import MODES, process_run
from .model import SchemaError, parse_stats
from .store import (FUNDING_HEADER, OBS_HEADER, RUN_HEADER, SNAP_HEADER, DataPaths, append_csv, default_meta,
                    iter_csv_files, load_json, load_last_obs, load_last_pay, prune_obs, read_csv, save_json)
from .timeutil import day_str, floor_hour, fmt_iso, parse_iso, utc_now

LATEST_CACHE = ".cache/latest_stats.json"   # build が使う最新値(コミットしない)


def _sig(v: str | None, digits: int = 7) -> str:
    """有効数字 digits 桁の文字列(スナップショットの価格用)。"""
    if v is None:
        return ""
    try:
        d = Decimal(v)
    except InvalidOperation:
        return ""
    if d == 0:
        return "0"
    s = f"{d:.{digits}g}"
    return s


def _int_str(v: str | None) -> str:
    if v is None:
        return ""
    try:
        return str(int(Decimal(v).to_integral_value()))
    except InvalidOperation:
        return ""


def _dec2(v: str | None) -> str:
    if v is None:
        return ""
    try:
        return f"{Decimal(v):.2f}"
    except InvalidOperation:
        return ""


def load_prev_changes(paths: DataPaths, prev_obs: dict[str, dict]) -> tuple[dict[str, int], dict[str, str]]:
    """銘柄ごとに、現在の窓(prev_obs の next_timestamp)で記録した C 行の数と、窓の最初の観測時刻(F 行)。直近 2 日分のログで足りる。"""
    counts: dict[str, int] = {}
    first: dict[str, str] = {}
    for p in iter_csv_files(paths.obs_dir, newest_first=True)[:2]:
        for r in read_csv(p):
            cur = prev_obs.get(r["ticker"])
            if not cur or r["next_timestamp"] != cur["next_timestamp"]:
                continue
            if r["kind"] == "C":
                counts[r["ticker"]] = counts.get(r["ticker"], 0) + 1
            elif r["kind"] == "F":
                first[r["ticker"]] = r["obs_ts"]
    return counts, first


def run_once(data_root: Path, settings: dict, *, now: _dt.datetime | None = None, scheduled: _dt.datetime | None = None,
             trigger: str = "manual", fetch=_fetch.fetch_stats, project_root: Path | None = None) -> dict:
    """戻り値: 実行ログの行(dict)。失敗しても例外は投げず、行の http_status / note で分かる。"""
    paths = DataPaths(data_root)
    paths.ensure()
    mode = settings.get("funding_mode")
    if mode not in MODES:
        raise ValueError("config/settings.json の funding_mode が未設定(confirmed か estimated)")
    now = now or utc_now()
    scheduled = scheduled or now
    meta = load_json(paths.meta_path, None) or default_meta(now)
    meta["funding_mode"] = mode
    row = {k: "" for k in RUN_HEADER}
    row.update(scheduled_ts=fmt_iso(scheduled), trigger=trigger)
    try:
        res = fetch()
    except _fetch.FetchError as e:
        row.update(fetch_ts=fmt_iso(now), http_status=e.status or "", note=f"fetch failed: {e} (attempts {e.attempts})")
        meta["last_run_ts"] = fmt_iso(now)
        save_json(paths.meta_path, meta)
        append_csv(paths.run_file(now), RUN_HEADER, [row])
        return row
    fetch_ts_dt = now
    fetch_ts = fmt_iso(fetch_ts_dt)
    row.update(fetch_ts=fetch_ts, delay_s=int((fetch_ts_dt - scheduled).total_seconds()), http_status=res.status)
    try:
        stats = parse_stats(res.body)
    except SchemaError as e:
        row.update(note=f"schema error: {e}")
        meta["last_run_ts"] = fmt_iso(now)
        save_json(paths.meta_path, meta)
        append_csv(paths.run_file(now), RUN_HEADER, [row])
        return row
    row.update(n_listings=len(stats.perps) + len(stats.swaps), n_perp=len(stats.perps), n_swap=len(stats.swaps))

    prev_obs = load_last_obs(paths)
    prev_changes, prev_first = load_prev_changes(paths, prev_obs)
    last_pay = load_last_pay(paths)
    result = process_run(stats.perps, fetch_ts, prev_obs, prev_changes, meta.get("last_success_ts"), last_pay, mode, prev_first)

    # 観測ログ: 行ごとの obs_ts の日付のファイルへ(L 行は前回の取得時刻なので前日のことがある)
    by_day: dict[str, list[dict]] = {}
    for r in result.obs_rows:
        by_day.setdefault(r["obs_ts"][:10], []).append(r)
    n_obs = 0
    for day, rows in sorted(by_day.items()):
        n_obs += append_csv(paths.obs_dir / f"{day}.csv", OBS_HEADER, rows)
    # 確定 FR: pay_ts の月のファイルへ
    by_month: dict[str, list[dict]] = {}
    for r in result.funding_rows:
        d = _dt.datetime.fromtimestamp(int(r["pay_ts"]), _dt.timezone.utc)
        by_month.setdefault(d.strftime("%Y-%m"), []).append(r)
    n_fund = 0
    for m, rows in sorted(by_month.items()):
        rows.sort(key=lambda r: (r["pay_ts"], r["ticker"]))
        n_fund += append_csv(paths.funding_dir / f"{m}.csv", FUNDING_HEADER, rows)

    # 毎時スナップショット: この UTC 時で最初の成功なら書く
    last_ok = meta.get("last_success_ts")
    first_in_hour = last_ok is None or floor_hour(parse_iso(last_ok)) < floor_hour(fetch_ts_dt)
    if first_in_hour:
        snap = []
        for p in stats.perps:
            snap.append({"snap_ts": fetch_ts, "ticker": p.ticker, "contract_type": "perpetual", "mark_price": _sig(p.mark_price),
                         "long_oi": _int_str(p.long_oi), "short_oi": _int_str(p.short_oi), "volume_24h": _int_str(p.volume_24h),
                         "base_spread_bps": _dec2(p.spread_bps)})
        for s in stats.swaps:
            snap.append({"snap_ts": fetch_ts, "ticker": s.ticker, "contract_type": "swap", "mark_price": _sig(s.mark_price),
                         "long_oi": _int_str(s.long_oi), "short_oi": _int_str(s.short_oi), "volume_24h": _int_str(s.volume_24h),
                         "base_spread_bps": ""})
        append_csv(paths.snap_file(fetch_ts_dt), SNAP_HEADER, snap)

    # swap の最新値(全件)
    save_json(paths.swaps_path, {"snap_ts": fetch_ts, "rows": [
        {"ticker": s.ticker, "name": s.name, "mark_price": s.mark_price, "long_oi": s.long_oi, "short_oi": s.short_oi,
         "volume_24h": s.volume_24h, "interval_seconds": s.interval_seconds, "next_timestamp": s.next_timestamp,
         "last": s.last, "next": s.next} for s in stats.swaps]})

    # 銘柄名(新規上場のときだけ変わる。取得に失敗した回の build でも名前を出せるように持つ)
    names = {p.ticker: p.name for p in stats.perps}
    names.update({s.ticker: s.name for s in stats.swaps})
    names_path = Path(data_root) / "names.json"
    if load_json(names_path, None) != names:
        save_json(names_path, names, compact=True)

    # build 用の最新値(gitignore 対象)
    root = project_root or Path(data_root).parent
    cache = root / LATEST_CACHE
    cache.parent.mkdir(parents=True, exist_ok=True)
    save_json(cache, {"fetch_ts": fetch_ts, "perps": [
        {"ticker": p.ticker, "name": p.name, "mark_price": p.mark_price, "long_oi": p.long_oi, "short_oi": p.short_oi,
         "volume_24h": p.volume_24h, "spread_bps": p.spread_bps, "interval_seconds": p.interval_seconds,
         "next_timestamp": p.next_timestamp, "interval_rate": p.interval_rate, "annualized_rate": p.annualized_rate} for p in stats.perps]},
              compact=True)

    # meta と矛盾の計数(直近 7 日分だけ持つ)
    day = day_str(fetch_ts_dt)
    ab = meta.get("anomalies_by_day") or {}
    cur = ab.get(day) or {}
    if not isinstance(cur, dict):
        cur = {"n": int(cur), "checked": 0}
    ab[day] = {"n": int(cur.get("n", 0)) + len(result.anomalies), "checked": int(cur.get("checked", 0)) + result.n_checked}
    meta["anomalies_by_day"] = {k: v for k, v in sorted(ab.items())[-7:]}
    meta["last_run_ts"] = fmt_iso(now)
    meta["last_success_ts"] = fetch_ts
    meta.setdefault("collection_start", fetch_ts)
    save_json(paths.meta_path, meta)

    note = []
    if stats.skipped:
        note.append(f"skipped {len(stats.skipped)}")
    if result.n_new:
        note.append(f"new {result.n_new}")
    if result.anomalies:
        note.append("anomalies: " + "; ".join(result.anomalies[:5]))
    row.update(n_obs_rows=n_obs, n_funding_rows=n_fund, n_anomalies=len(result.anomalies), note=" | ".join(note))
    append_csv(paths.run_file(now), RUN_HEADER, [row])

    # 観測ログの保持期間(月締めのアーカイブは archive.yml が別に行う: 収集ジョブは sparse checkout で data/archive を持たない)
    prune_obs(paths, now)
    return row
