"""1 年後のリポサイズ(git 履歴込み)を、実データから外挿して見積もる。

やり方: 一時リポに「1 回の収集」相当の追記を N 回コミットし(観測ログ・確定 FR・毎時スナップショット・実行ログ)、
git gc --aggressive 後の pack サイズから 1 回あたりの費用を測り、1 年(144 回/日 × 365 日)に外挿する。
月締めの .csv.gz は別に見積もる(gz は差分圧縮が効かないので 1 月分の実サイズ × 12)。
  使い方: python scripts/estimate_repo_size.py [--runs 288] [--root .]
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import random
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from vfm.store import FUNDING_HEADER, OBS_HEADER, RUN_HEADER, SNAP_HEADER, read_csv  # noqa: E402

# Worker 経由 6 回/時 = 144 回/日。保険の schedule(2 回/時)は直前 12 分以内に成功していれば何も書かないので、
# Worker が止まっている時間帯だけ増える。余裕を見て 150 回/日とする
RUNS_PER_DAY = 150
DAYS = 365


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--runs", type=int, default=288, help="模擬する収集回数(288 = 2 日分)")
    args = ap.parse_args()
    root = Path(args.root)
    obs_files = sorted((root / "data/obs").glob("*.csv"))
    snap_files = sorted((root / "data/snapshots").glob("*.csv"))
    if not obs_files or not snap_files:
        print("data/obs と data/snapshots に実データが必要です"); return 1
    obs = [r for f in obs_files for r in read_csv(f)]
    snaps = [r for f in snap_files for r in read_csv(f)]
    runs = [r for f in sorted((root / "data/runs").glob("*.csv")) for r in read_csv(f)]
    tickers = sorted({r["ticker"] for r in obs})
    intervals = {r["ticker"]: int(r["interval_seconds"]) for r in obs}
    # 1 日の窓の数(= 支払回数)と、1 回あたりの観測行(変化点)
    windows_per_day = sum(86400 // iv for iv in intervals.values())
    changes = [int(r["n_obs_rows"]) for r in runs[1:] if r.get("n_obs_rows") and int(r["n_obs_rows"]) < len(tickers)]
    c_rows_per_run = (sum(changes) / len(changes)) if changes else 90
    # 窓ごとに F と L の 2 行 + 変化点
    obs_rows_per_run = c_rows_per_run + 2 * windows_per_day / RUNS_PER_DAY
    fund_rows_per_run = windows_per_day / RUNS_PER_DAY
    snap_rows_per_hour = len({r["ticker"] for r in snaps})
    print(f"銘柄 {len(tickers)}、1 日の窓 {windows_per_day}、観測行/回 {obs_rows_per_run:.1f}(変化点 {c_rows_per_run:.1f})、確定 FR 行/回 {fund_rows_per_run:.1f}、スナップショット行/時 {snap_rows_per_hour}")

    rnd = random.Random(1)
    c_rows = [r for r in obs if r["kind"] == "C"] or obs
    sw_path = root / "data/swaps_latest.json"
    swaps_latest = json.loads(sw_path.read_text()) if sw_path.exists() else None
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        git(d, "init", "-q", "-b", "main"); git(d, "config", "user.email", "a@b"); git(d, "config", "user.name", "a")
        (d / "data/obs").mkdir(parents=True); (d / "data/funding").mkdir(); (d / "data/snapshots").mkdir(); (d / "data/runs").mkdir()
        base = 1792886400
        for i in range(args.runs):
            ts = base + i * 600
            day = i // RUNS_PER_DAY
            iso = f"2026-11-{1 + day:02d}T{(i % RUNS_PER_DAY) // 6:02d}:{(i % 6) * 10:02d}:05Z"
            def app(path, header, rows):
                new = not path.exists()
                with path.open("a", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=header, lineterminator="\n")
                    if new: w.writeheader()
                    w.writerows(rows)
            # 観測: 実データの変化点行をランダムに選び、時刻・値を少し変える
            rows = []
            for _ in range(int(round(obs_rows_per_run))):
                r = dict(rnd.choice(c_rows)); r["obs_ts"] = iso
                r["interval_rate"] = f"{float(r['interval_rate']) * (1 + rnd.uniform(-0.3, 0.3)):.7g}"
                r["annualized_rate"] = f"{float(r['interval_rate']) * 365 * 86400 / int(r['interval_seconds']):.4g}"
                r["next_timestamp"] = str(ts // 14400 * 14400 + 14400); rows.append(r)
            app(d / f"data/obs/2026-11-{1 + day:02d}.csv", OBS_HEADER, rows)
            frows = []
            for _ in range(int(round(fund_rows_per_run))):
                r = rnd.choice(c_rows)
                frows.append({"pay_ts": ts // 3600 * 3600, "ticker": r["ticker"], "interval_seconds": r["interval_seconds"],
                              "interval_rate": f"{float(r['interval_rate']) * (1 + rnd.uniform(-0.3, 0.3)):.7g}",
                              "annualized_rate": r["annualized_rate"], "status": "estimated", "basis_obs_ts": iso})
            app(d / "data/funding/2026-11.csv", FUNDING_HEADER, frows)
            if i % 6 == 0:
                srows = []
                for r in snaps[:snap_rows_per_hour]:
                    r = dict(r); r["snap_ts"] = iso
                    for k in ("long_oi", "short_oi", "volume_24h"):
                        if r[k]: r[k] = str(int(int(r[k]) * rnd.uniform(0.97, 1.03)))
                    if r["mark_price"]: r["mark_price"] = f"{float(r['mark_price']) * rnd.uniform(0.995, 1.005):.7g}"
                    srows.append(r)
                app(d / f"data/snapshots/2026-11-{1 + day:02d}.csv", SNAP_HEADER, srows)
            app(d / "data/runs/2026-11.csv", RUN_HEADER, [{"scheduled_ts": iso, "fetch_ts": iso, "delay_s": 5, "http_status": 200, "n_listings": 582,
                                                          "n_perp": 564, "n_swap": 18, "n_obs_rows": len(rows), "n_funding_rows": len(frows), "n_anomalies": 0,
                                                          "trigger": "workflow_dispatch", "note": ""}])
            (d / "data/meta.json").write_text('{"last_success_ts": "%s", "last_run_ts": "%s"}\n' % (iso, iso))
            # swap の最新値(全件)は毎回書き換わる(価格・OI・出来高・レートが動く)
            if swaps_latest:
                sw = json.loads(json.dumps(swaps_latest)); sw["snap_ts"] = iso
                for row in sw.get("rows", []):
                    for k in ("mark_price", "long_oi", "short_oi", "volume_24h"):
                        if row.get(k): row[k] = f"{float(row[k]) * rnd.uniform(0.97, 1.03):.10g}"
                (d / "data/swaps_latest.json").write_text(json.dumps(sw, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
            git(d, "add", "-A"); git(d, "commit", "-q", "-m", f"data: {iso}")
        git(d, "gc", "-q", "--aggressive", "--prune=now")
        pack = sum(p.stat().st_size for p in (d / ".git/objects/pack").glob("*"))
        work = sum(p.stat().st_size for p in (d / "data").rglob("*") if p.is_file())
    per_run = pack / args.runs
    year_runs = RUNS_PER_DAY * DAYS
    hist = per_run * year_runs
    # 月締め gz: 1 月分のスナップショット行を gzip した実サイズ
    buf = io.StringIO(); w = csv.DictWriter(buf, fieldnames=SNAP_HEADER, lineterminator="\n"); w.writeheader()
    for h in range(24 * 30):
        for r in snaps[:snap_rows_per_hour]:
            r = dict(r); r["snap_ts"] = f"2026-11-{1 + h // 24:02d}T{h % 24:02d}:08:05Z"; w.writerow(r)
    gz = len(gzip.compress(buf.getvalue().encode(), 9))
    hl = 12 * 30 * 40_000  # hl_7d.json(約 40 KB)を毎日 1 blob
    total = hist + gz * 12 + hl
    print(f"模擬 {args.runs} 回: pack {pack/1e6:.2f} MB(作業ツリー {work/1e6:.2f} MB)→ 1 回あたり {per_run/1e3:.1f} KB")
    print(f"1 年 = {year_runs} 回 → 履歴 {hist/1e6:.0f} MB + スナップショット月締め gz {gz/1e6:.1f} MB × 12 = {gz*12/1e6:.0f} MB + HL {hl/1e6:.0f} MB")
    print(f"見積もり合計(1 年、git 履歴込み): {total/1e6:.0f} MB → {'1 GB 未満 OK' if total < 1e9 else '1 GB 超え NG'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
