"""ページの JS(site/lib.js)と Python(src/vfm/aggregate.py)の計算が一致することを確かめるための共通の入力と Python の答え。

  python3 tests/fixtures/make_cross_lang.py      # tests/fixtures/cross_lang.json を作り直す

- 入力の支払いは data/tickers/<T>.json と同じ形([pay_ts 秒, rate|null, "c"|"e"|"m", 間隔(今と違うときだけ)])。
- tests/js/cross_lang.test.js が同じ入力を JS で計算して比べる。tests/test_cross_lang.py は JSON が最新か確かめる。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from vfm.aggregate import agg_payments, cum_stat, monthly_sums  # noqa: E402

OUT = Path(__file__).resolve().parent / "cross_lang.json"
PRESETS = {"7": 7, "30": 30, "90": 90, "180": 180, "365": 365, "all": None}
MIDNIGHT = 1793404800          # 2026-10-31T00:00:00Z
NOW = MIDNIGHT + 8 * 3600 + 8 * 60   # 2026-10-31T08:08:00Z(支払時刻とずらす)
H = 3600


def rates(seed: int):
    x = seed
    while True:
        x = (1103515245 * x + 12345) % (1 << 31)
        yield float(f"{(x / (1 << 31) - 0.4) * 0.0004:.7g}")


def series(start: int, end: int, iv: int, seed: int, miss_every: int = 0, est_from: int | None = None,
           file_iv: int | None = None) -> list:
    g, out, t, i = rates(seed), [], start, 0
    while t <= end:
        r = next(g)
        if miss_every and i % miss_every == miss_every - 1:
            p = [t, None, "m"]
        else:
            p = [t, r, "e" if est_from is not None and t >= est_from else "c"]
        if file_iv and iv != file_iv:
            p.append(iv)
        out.append(p)
        t, i = t + iv, i + 1
    return out


def cases() -> list[dict]:
    c = []
    c.append({"name": "8h・40 日・欠測と推定が混ざる", "interval": 8 * H,
              "payments": series(MIDNIGHT - 40 * 86400, MIDNIGHT + 8 * H, 8 * H, 1, miss_every=7, est_from=NOW - 3 * 86400)})
    c.append({"name": "4h・2 日だけ(※)・途中に欠測", "interval": 4 * H,
              "payments": series(MIDNIGHT - 2 * 86400, MIDNIGHT + 8 * H, 4 * H, 2, miss_every=3)})
    c.append({"name": "1h・5 日・すべて推定", "interval": H,
              "payments": series(MIDNIGHT - 5 * 86400, MIDNIGHT + 8 * H, H, 3, est_from=0)})
    old = series(MIDNIGHT - 60 * 86400, MIDNIGHT - 10 * 86400, 8 * H, 4, file_iv=4 * H)
    new = series(MIDNIGHT - 10 * 86400 + 4 * H, MIDNIGHT + 8 * H, 4 * H, 5, miss_every=11, file_iv=4 * H)
    c.append({"name": "支払間隔の変更 8h→4h(古い支払いに間隔が付く)", "interval": 4 * H, "payments": old + new})
    c.append({"name": "支払い 1 回だけ", "interval": 8 * H, "payments": [[MIDNIGHT + 8 * H, 0.0001, "e"]]})
    c.append({"name": "すべて欠測", "interval": 8 * H, "payments": [[MIDNIGHT - k * 8 * H, None, "m"] for k in range(5, 0, -1)]})
    lead = [[MIDNIGHT - 3 * 86400 + k * 8 * H, None, "m"] for k in range(3)]
    c.append({"name": "先頭が欠測、その後に支払い", "interval": 8 * H,
              "payments": lead + series(MIDNIGHT - 2 * 86400, MIDNIGHT + 8 * H, 8 * H, 6)})
    c.append({"name": "月と週の境目をまたぐ(直近 7 日には無い)", "interval": 8 * H,
              "payments": series(1790380800, 1791158400, 8 * H, 7, miss_every=5)})   # 2026-09-26 〜 2026-10-05
    c.append({"name": "8h・400 日(1 年の列が埋まる)", "interval": 8 * H,
              "payments": series(MIDNIGHT - 400 * 86400, MIDNIGHT + 8 * H, 8 * H, 8, miss_every=50, est_from=MIDNIGHT - 20 * 86400)})
    # 期間の境目: now がちょうど支払時刻で、now − 7 日・now − 30 日にも支払いがある((now − N 日, now] なので前者は入らず後者は入る)
    at = MIDNIGHT + 8 * H
    c.append({"name": "now が支払時刻ちょうど(境目の支払いの扱い)", "interval": 8 * H, "now": at,
              "payments": series(at - 40 * 86400, at, 8 * H, 9, miss_every=13)})
    for x in c:
        now = x.pop("now", NOW)
        x["now_ms"] = now * 1000
        pays, iv = x["payments"], x["interval"]
        x["expected"] = {
            "cum": {k: cum_stat(pays, now, d, iv) for k, d in PRESETS.items()},
            "agg": {f: agg_payments(pays, f) for f in ("raw", "D", "W", "M")},
            "monthly": monthly_sums(pays),
        }
    return c


def build() -> dict:
    return {"note": "tests/fixtures/make_cross_lang.py が作る。手で直さない。rate は小数、cum の sum/apr も小数、agg の y は %。",
            "cases": cases()}


if __name__ == "__main__":
    OUT.write_text(json.dumps(build(), ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
