"""data/ からサイト用 JSON(site/data/)を作る。毎回全部作り直す(コミットしない)。"""
from __future__ import annotations

import datetime as _dt
import json
import shutil
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .aggregate import cum_stat, monthly_sums
from .categories import classify, load_overrides
from .collect import LATEST_CACHE
from .funding import ST_MISSING
from .hl import load_mapping
from .store import DataPaths, iter_csv_files, load_all_funding, load_json, load_last_obs, read_csv, save_json
from .timeutil import INTEREST_APR, annualize, day_str, fmt_iso, from_unix, parse_iso, utc_now

PRESETS = {"7": 7, "30": 30, "90": 90, "180": 180, "365": 365, "all": None}
STATUS_CODE = {"confirmed": "c", "estimated": "e", "missing": "m"}


def _f(v) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(Decimal(str(v)))
    except (InvalidOperation, ValueError):
        return None


def _month_key(ts: int) -> str:
    return from_unix(ts).strftime("%Y-%m")


def _r(v, sig: int = 6):
    """summary.json を小さくするため、float を有効数字 sig 桁に丸める(dict / list は再帰)。"""
    if isinstance(v, float):
        if v == 0 or v != v:
            return v
        return float(f"{v:.{sig}g}")
    if isinstance(v, dict):
        return {k: _r(x, sig) for k, x in v.items()}
    if isinstance(v, list):
        return [_r(x, sig) for x in v]
    return v


def _latest_from_fallback(paths: DataPaths, obs: dict[str, dict]) -> dict:
    """.cache が無いとき(収集に失敗した回)、最新のスナップショットと観測ログから最新値を組み立てる。"""
    perps: dict[str, dict] = {}
    files = iter_csv_files(paths.snap_dir, newest_first=True)
    snap_ts = None
    if files:
        rows = read_csv(files[0])
        last_ts = max((r["snap_ts"] for r in rows), default=None)
        snap_ts = last_ts
        for r in rows:
            if r["snap_ts"] == last_ts and r["contract_type"] == "perpetual":
                perps[r["ticker"]] = {"ticker": r["ticker"], "name": r["ticker"], "mark_price": r["mark_price"], "long_oi": r["long_oi"],
                                      "short_oi": r["short_oi"], "volume_24h": r["volume_24h"], "spread_bps": r["base_spread_bps"]}
    for t, o in obs.items():
        p = perps.setdefault(t, {"ticker": t, "name": t})
        p.update(interval_seconds=int(o["interval_seconds"]), next_timestamp=int(o["next_timestamp"]), interval_rate=o["interval_rate"],
                 annualized_rate=o.get("annualized_rate"))
    return {"fetch_ts": snap_ts, "perps": list(perps.values())}


def build(project_root: Path, out_dir: Path, *, now: _dt.datetime | None = None) -> dict:
    project_root, out_dir = Path(project_root), Path(out_dir)
    now = now or utc_now()
    now_ts = int(now.timestamp())
    paths = DataPaths(project_root / "data")
    settings = load_json(project_root / "config" / "settings.json", {})
    meta = load_json(paths.meta_path, {})
    overrides = load_overrides(project_root / "config" / "category_overrides.csv")
    mapping = load_mapping(project_root / "config" / "hl_mapping.csv")
    hl = load_json(paths.hl_path, {})
    hl_universe = load_json(paths.hl_dir / "hl_universe.json", {})
    hl_main = set(hl_universe.get("main", []))
    hip3_equity = {c for coins in hl_universe.get("hip3", {}).values() for c in coins}
    obs = load_last_obs(paths)
    latest = load_json(project_root / LATEST_CACHE, None) or _latest_from_fallback(paths, obs)
    names = dict(load_json(paths.root / "names.json", {}))
    names.update({p["ticker"]: p["name"] for p in latest.get("perps", []) if p.get("name") and p.get("name") != p["ticker"]})

    # 確定 FR を銘柄ごとに
    pays: dict[str, list[tuple[int, float | None, str]]] = {}
    intervals: dict[str, int] = {}
    for r in load_all_funding(paths):
        t = r["ticker"]
        iv_row = int(r["interval_seconds"]) if r.get("interval_seconds") else None
        pays.setdefault(t, []).append((int(r["pay_ts"]), _f(r.get("interval_rate")), r["status"], iv_row))
        if iv_row:
            intervals[t] = iv_row
    for v in pays.values():
        v.sort()

    tick_dir = out_dir / "data" / "tickers"
    if tick_dir.exists():
        shutil.rmtree(tick_dir)
    tick_dir.mkdir(parents=True, exist_ok=True)

    # 最新の取得に無い銘柄(上場廃止・一時的な欠落)も履歴があれば残す(ページで「停止?」)
    listed = {p["ticker"] for p in latest.get("perps", [])}
    perps = list(latest.get("perps", []))
    for t in sorted((set(pays) | set(obs)) - listed):
        o = obs.get(t) or {}
        perps.append({"ticker": t, "interval_seconds": int(o.get("interval_seconds") or intervals.get(t) or 0) or None,
                      "next_timestamp": int(o["next_timestamp"]) if o.get("next_timestamp") else None,
                      "interval_rate": o.get("interval_rate"), "annualized_rate": o.get("annualized_rate")})
    month_keys = sorted({_month_key(p[0]) for pl in pays.values() for p in pl})[-18:]
    monthly: dict[str, dict] = {}
    rows = []
    missing_total = 0
    for p in perps:
        t = p["ticker"]
        iv = int(p.get("interval_seconds") or intervals.get(t) or 0)
        if not iv:
            continue
        pl = pays.get(t, [])
        n_missing = sum(1 for p in pl if p[2] == ST_MISSING)
        missing_total += n_missing
        valid = [p for p in pl if p[2] != ST_MISSING and p[1] is not None]
        # 直近の支払い = いちばん新しい行。それが欠測なら rate は None(古い支払いを「直近」と見せない)
        latest_row = pl[-1] if pl else None
        last_pay = latest_row if (latest_row and latest_row[2] != ST_MISSING and latest_row[1] is not None) else None
        cur_rate = _f(p.get("interval_rate"))
        cum = {k: cum_stat(pl, now_ts, d, iv) for k, d in PRESETS.items()}
        apr7 = cum["7"]["apr"] if cum["7"] and not cum["7"]["partial"] else None
        apr30 = cum["30"]["apr"] if cum["30"] and not cum["30"]["partial"] else None
        # summary.json を小さくするため配列で持つ(ページ側で {sum, apr, partial, start, days} に展開する)
        cum_compact = {k: ([c["sum"], c["apr"], 1 if c["partial"] else 0, c["start"], round(c["days"], 2), c["n_missing"]] if c else None)
                       for k, c in cum.items()}
        hl_coin = (mapping.get(t) or {}).get("hl_coin") or None
        hl_apr7 = (hl.get("coins", {}).get(hl_coin) or {}).get("apr7d") if hl_coin else None
        long_oi, short_oi = _f(p.get("long_oi")) or 0.0, _f(p.get("short_oi")) or 0.0
        oi = long_oi + short_oi
        o = obs.get(t)
        data_start = day_str(from_unix(pl[0][0])) if pl else (o["obs_ts"][:10] if o else None)
        if "/" in t or "\\" in t:
            continue   # ファイル名に使えないティッカー(想定外)は飛ばす
        rows.append(_r({
            "ticker": t, "name": names.get(t, t), "category": classify(t, names.get(t, ""), overrides, hl_main, hip3_equity),
            "interval": iv, "next_ts": p.get("next_timestamp"), "data_start": data_start,
            "cur_rate": cur_rate, "cur_apr": annualize(cur_rate, iv) if cur_rate is not None else None,
            "last_rate": last_pay[1] if last_pay else None,
            "last_apr": annualize(last_pay[1], last_pay[3] or iv) if last_pay else None,
            "last_status": latest_row[2] if latest_row else None, "last_pay_ts": latest_row[0] if latest_row else None,
            "apr7": apr7, "apr30": apr30, "cum": cum_compact,
            "hl_coin": hl_coin, "hl_apr7": hl_apr7,
            "diff_pp": (apr7 - hl_apr7) * 100 if (apr7 is not None and hl_apr7 is not None) else None,
            "oi": oi, "long_oi": long_oi, "short_oi": short_oi, "long_share": (long_oi / oi) if oi > 0 else None,
            "vol24": _f(p.get("volume_24h")), "spread_bps": _f(p.get("spread_bps")), "mark": _f(p.get("mark_price")),
            "n_pay": len(valid), "n_missing": n_missing,
            "last_obs": o["obs_ts"] if o else (latest.get("fetch_ts") if t in listed else (day_str(from_unix(pl[-1][0])) + "T00:00:00Z" if pl else None)),
            "listed": t in listed,
        }))
        # 月ごとの累積(ヒートマップ用。銘柄別ファイルを 60 個読まずに済むように)
        msum = {k: v for k, v in monthly_sums(pl).items() if k in month_keys}
        if msum:
            monthly[t] = _r(msum)
        # 1 支払い = [pay_ts, rate|null, c/e/m]。支払間隔が今と違う支払い(間隔の変更前)だけ 4 つ目に間隔を付ける
        save_json(tick_dir / f"{t}.json", {"ticker": t, "interval": iv, "payments": [
            [p[0], p[1], STATUS_CODE.get(p[2], p[2])] + ([p[3]] if p[3] and p[3] != iv else []) for p in pl]}, compact=True)
    rows.sort(key=lambda r: -(r["oi"] or 0))
    last_ok = meta.get("last_success_ts")
    stale_h = (now - parse_iso(last_ok)).total_seconds() / 3600 if last_ok else None
    today = day_str(now)
    a_today = (meta.get("anomalies_by_day") or {}).get(today, 0)
    if not isinstance(a_today, dict):
        a_today = {"n": int(a_today or 0), "checked": 0}
    anomalies_today, checked_today = int(a_today.get("n", 0)), int(a_today.get("checked", 0))
    warn_per_day = int(settings.get("anomaly_warn_per_day", 20))
    warn_ratio = float(settings.get("anomaly_warn_ratio", 0.5))
    warn_min_checked = int(settings.get("anomaly_warn_min_checked", 20))
    if settings.get("funding_mode") == "estimated":
        # 見込み値型: 検査した窓のうち「動かなかった」窓が半分を超えたら判定が怪しい
        anomaly_warn = checked_today >= warn_min_checked and anomalies_today / checked_today > warn_ratio
    else:
        anomaly_warn = anomalies_today > warn_per_day
    summary = {
        "generated": fmt_iso(now), "last_success": last_ok, "collection_start": meta.get("collection_start"),
        "funding_mode": settings.get("funding_mode"), "anomalies_today": anomalies_today, "anomalies_checked_today": checked_today,
        "anomaly_warn": anomaly_warn, "anomaly_warn_per_day": warn_per_day, "anomaly_warn_ratio": warn_ratio,
        "missing_total": missing_total, "stale_hours": round(stale_h, 2) if stale_h is not None else None,
        "hl_fetched": hl.get("fetched"), "interest_apr": INTEREST_APR, "n_tickers": len(rows), "rows": rows,
    }
    (out_dir / "data").mkdir(parents=True, exist_ok=True)
    save_json(out_dir / "data" / "summary.json", summary, compact=True)
    save_json(out_dir / "data" / "monthly.json", {"generated": fmt_iso(now), "months": month_keys, "rows": monthly}, compact=True)

    sw = load_json(paths.swaps_path, {"rows": []})
    swap_rows = []
    for s in sw.get("rows", []):
        lo, sh = _f(s.get("long_oi")) or 0.0, _f(s.get("short_oi")) or 0.0
        def _conv(d):
            if not d:
                return None
            return {"ann_long": _f(d.get("annualized_long_rate")), "ann_short": _f(d.get("annualized_short_rate")),
                    "int_long": _f(d.get("interval_long_rate")), "int_short": _f(d.get("interval_short_rate"))}
        swap_rows.append({"ticker": s["ticker"], "name": s.get("name") or s["ticker"], "mark": _f(s.get("mark_price")),
                          "long_oi": lo, "short_oi": sh, "vol24": _f(s.get("volume_24h")), "interval": s.get("interval_seconds"),
                          "next_ts": s.get("next_timestamp"), "last": _conv(s.get("last")), "next": _conv(s.get("next")), "snap_ts": sw.get("snap_ts")})
    save_json(out_dir / "data" / "swaps.json", {"generated": fmt_iso(now), "snap_ts": sw.get("snap_ts"), "rows": swap_rows}, compact=True)
    return {"n_rows": len(rows), "n_swaps": len(swap_rows), "missing_total": missing_total, "stale_hours": stale_h}
