#!/usr/bin/env python3
"""報告と README 用の集計: 種類別の件数、上書き CSV の銘柄、HL 対応の数、欠測・推定の数、収集の空白。
  PYTHONPATH=src python3 scripts/report_summary.py [--site site]
site/data/summary.json(python -m vfm build の出力)と data/ を読む。"""
from __future__ import annotations

import argparse
import collections
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LABEL = {"crypto": "暗号資産", "equity": "株式・ETF", "commodity": "コモディティ", "index": "指数", "preipo": "プレIPO", "other": "その他"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default=str(ROOT / "site"))
    a = ap.parse_args()
    s = json.loads((Path(a.site) / "data" / "summary.json").read_text())
    rows = s["rows"]
    listed = [r for r in rows if r.get("listed", True)]
    cats = collections.Counter(r["category"] for r in listed)
    print("## 種類別の件数(掲載中の perp)")
    for k in LABEL:
        print(f"- {LABEL[k]}: {cats.get(k, 0)}")
    print(f"- 合計: {len(listed)}(掲載終了・一時的に無い銘柄 {len(rows) - len(listed)})")
    print("\n## 上書き CSV(config/category_overrides.csv)")
    with open(ROOT / "config" / "category_overrides.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            print(f"- {r['ticker']} → {LABEL.get(r['category'], r['category'])}: {r['reason']}")
    mapped = [r for r in listed if r.get("hl_coin")]
    with_val = [r for r in mapped if r.get("hl_apr7") is not None]
    print(f"\n## HL 比較: 対応あり {len(mapped)} / {len(listed)}、HL 7 日年率あり {len(with_val)}(HL 取得 {s.get('hl_fetched')})")
    st = collections.Counter()
    for p in sorted((ROOT / "data" / "funding").glob("*.csv")):
        with open(p, encoding="utf-8") as f:
            st.update(r["status"] for r in csv.DictReader(f))
    print(f"\n## 確定 FR の行: {dict(st)}、ページの欠測合計 {s.get('missing_total')}")
    runs = []
    for p in sorted((ROOT / "data" / "runs").glob("*.csv")):
        with open(p, encoding="utf-8") as f:
            runs += [r for r in csv.DictReader(f) if r.get("http_status") == "200"]
    print(f"\n## 取得の成功: {len(runs)} 回、最初 {runs[0]['fetch_ts'] if runs else '-'}、最後 {runs[-1]['fetch_ts'] if runs else '-'}")
    gaps = []
    import datetime as dt
    ts = [dt.datetime.fromisoformat(r["fetch_ts"].replace("Z", "+00:00")) for r in runs]
    for x, y in zip(ts, ts[1:]):
        if (y - x).total_seconds() > 30 * 60:
            gaps.append((x, y))
    for x, y in gaps:
        print(f"- 収集の空白: {x:%Y-%m-%d %H:%M:%S} UTC 〜 {y:%Y-%m-%d %H:%M:%S} UTC({(y - x).total_seconds() / 3600:.1f} 時間)")
    delays = sorted(int(r["delay_s"]) for r in runs if r.get("delay_s", "").lstrip("-").isdigit() and r.get("trigger") in ("workflow_dispatch", "schedule"))
    if delays:
        print(f"\n## 起動の遅れ(クラウド、{len(delays)} 回): 中央値 {delays[len(delays) // 2]} 秒、最大 {delays[-1]} 秒")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
