"""支払いの列の集計(期間の累積・年率・※、日/週/月のまとめ、月ごとの累積)。

ページの JS(site/lib.js の cumStat / aggPayments / monthlyCum)と同じ定義。両方が同じ入力で同じ結果になることを
tests/fixtures/cross_lang.json(tests/fixtures/make_cross_lang.py が作る)で確かめている。

支払いは (pay_ts 秒, rate または None, status, interval_seconds または None) の列。status は confirmed / estimated / missing
(ページ用の 1 文字 c / e / m も受け付ける)。rate は小数(0.0001 = 0.01%)。
"""
from __future__ import annotations

import datetime as _dt

DAY = 86400
DAY_MS = DAY * 1000
_MISSING = {"missing", "m"}
_ESTIMATED = {"estimated", "e"}


def _norm(p) -> tuple[int, float | None, str, int | None]:
    ts, rate, status = int(p[0]), p[1], p[2]
    iv = p[3] if len(p) > 3 and p[3] else None
    if status in _MISSING or rate is None:
        return ts, None, "m", iv
    return ts, float(rate), ("e" if status in _ESTIMATED else "c"), iv


def cum_stat(payments, now_ts: int, days: int | None, interval: int) -> dict | None:
    """期間 (now − days 日, now](days が None なら全期間)の累積 FR と年率換算。
    - 累積 = 期間内の支払いの合計。欠測は加えない(0 にしない)。
    - 年率 = 累積 ÷ 観測できた日数 × 365。観測できた日数 = 期間内の(欠測でない)支払いの間隔の合計
      (欠測の窓を分母に入れて 0 のように薄めない。欠測が無ければ期間の長さと同じ)。
    - partial(ページの ※)= データが期間分ない(最初の支払いが期間の開始 + 1 間隔より後)。
    - n_missing = 期間内の欠測の数(ページの ※ の説明に出す)。"""
    rows = [_norm(p) for p in payments]
    valid = [(t, r, iv or interval) for t, r, _, iv in rows if r is not None]
    if not valid:
        return None
    first = valid[0][0]
    start = None if days is None else now_ts - days * DAY
    partial = False if days is None else first > start + interval

    def inwin(t: int) -> bool:
        return (start is None or t > start) and t <= now_ts

    sel = [(t, r, iv) for t, r, iv in valid if inwin(t)]
    if not sel:
        return None
    covered = sum(iv for _, _, iv in sel) / DAY
    total = 0.0
    for _, r, _ in sel:
        total += r
    return {"sum": total, "apr": total * 365 / covered, "partial": partial,
            "start": _dt.datetime.fromtimestamp(first, _dt.timezone.utc).strftime("%Y-%m-%d"),
            "days": covered, "n": len(sel), "n_missing": sum(1 for t, r, _, _ in rows if r is None and inwin(t))}


def day_start(ms: int) -> int:
    return ms // DAY_MS * DAY_MS


def week_start(ms: int) -> int:
    """月曜はじまり(JS の (getUTCDay() + 6) % 7 と同じ)。"""
    d = day_start(ms)
    return d - _dt.datetime.fromtimestamp(d / 1000, _dt.timezone.utc).weekday() * DAY_MS


def month_start(ms: int) -> int:
    d = _dt.datetime.fromtimestamp(ms / 1000, _dt.timezone.utc)
    return int(_dt.datetime(d.year, d.month, 1, tzinfo=_dt.timezone.utc).timestamp() * 1000)


def next_bucket(ms: int, f: str) -> int:
    if f == "D":
        return ms + DAY_MS
    if f == "W":
        return ms + 7 * DAY_MS
    d = _dt.datetime.fromtimestamp(ms / 1000, _dt.timezone.utc)
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return int(_dt.datetime(y, m, 1, tzinfo=_dt.timezone.utc).timestamp() * 1000)


BUCKET = {"D": day_start, "W": week_start, "M": month_start}


def agg_payments(payments, f: str) -> list[dict]:
    """FR(%)を f(raw / D / W / M)でまとめた [{x: ms, y, est, n, miss}](JS の aggPayments と同じ)。
    raw は 1 回の支払いが 1 本。欠測は y = None(0 にしない)。区間に推定が 1 つでもあれば est。
    D/W/M は最初と最後の区間の間を埋め、支払いのない区間は y = None。"""
    rows = sorted((_norm(p) for p in payments), key=lambda p: p[0])
    if f != "raw" and f not in BUCKET:
        f = "D"   # 知らない指定は日ごと(区切りと進め方を揃える。JS の aggPayments と同じ)
    if f == "raw":
        return [{"x": t * 1000, "y": None if r is None else r * 100, "est": s == "e" and r is not None,
                 "n": 0 if r is None else 1, "miss": 1 if r is None else 0} for t, r, s, _ in rows]
    bucket = BUCKET[f]
    m: dict[int, dict] = {}
    for t, r, s, _ in rows:
        k = bucket(t * 1000)
        a = m.setdefault(k, {"x": k, "y": None, "est": False, "n": 0, "miss": 0})
        if r is None:
            a["miss"] += 1
        else:
            a["y"] = (a["y"] or 0) + r * 100
            a["n"] += 1
            if s == "e":
                a["est"] = True
    if not m:
        return []
    keys = sorted(m)
    out, k = [], keys[0]
    while k <= keys[-1]:
        out.append(m.get(k) or {"x": k, "y": None, "est": False, "n": 0, "miss": 0})
        k = next_bucket(k, f)
    return out


def monthly_sums(payments) -> dict[str, float]:
    """月ごとの累積(小数)。欠測は加えない。ページの monthlyCum(%)と同じ区切り。"""
    out: dict[str, float] = {}
    for t, r, _, _ in (_norm(p) for p in payments):
        if r is None:
            continue
        k = _dt.datetime.fromtimestamp(t, _dt.timezone.utc).strftime("%Y-%m")
        out[k] = out.get(k, 0.0) + r
    return out
