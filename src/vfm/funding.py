"""観測 → 観測ログの行と確定 FR の行を作る純粋ロジック。

funding_mode:
  confirmed(確定値型): next_timestamp = T の窓で最初に観測した値を pay_ts = T − interval の確定 FR とする。
  estimated(見込み値型): 窓 T が閉じた(next_timestamp が進んだ)とき、T の直前の最後の観測値を pay_ts = T の推定 FR とする。
欠測は missing(rate 空欄)で埋め、0 にはしない。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from .timeutil import INTEREST_PER_HOUR, parse_iso, to_unix

MODE_CONFIRMED = "confirmed"
MODE_ESTIMATED = "estimated"
MODES = (MODE_CONFIRMED, MODE_ESTIMATED)

KIND_FIRST, KIND_CHANGE, KIND_LAST = "F", "C", "L"
ST_CONFIRMED, ST_ESTIMATED, ST_MISSING = "confirmed", "estimated", "missing"

# 見込み値型の矛盾判定: 窓の中で 25 分以上観測が続いたのに(10 分間隔で 3 回以上)値が一度も動かなかった
STALL_SECONDS = 25 * 60


@dataclass
class RunResult:
    obs_rows: list[dict] = field(default_factory=list)
    funding_rows: list[dict] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)
    n_rollover: int = 0
    n_new: int = 0


def rate_equal(a: str | None, b: str | None) -> bool:
    """文字列の数値を数として比べる('0.000050' と '0.00005000' は同じ)。"""
    if a is None or b is None:
        return a is b
    try:
        return Decimal(a) == Decimal(b)
    except InvalidOperation:
        return a == b


def is_trivial_rate(rate: str | None, interval_seconds: int) -> bool:
    """0 または金利成分そのもの(プレミアムが無い銘柄)。"""
    if rate is None:
        return True
    try:
        v = Decimal(rate)
    except InvalidOperation:
        return False
    if v == 0:
        return True
    comp = Decimal(str(INTEREST_PER_HOUR)) * Decimal(interval_seconds) / Decimal(3600)
    return abs(v - comp) <= Decimal("1e-12")


def _obs(obs_ts: str, ticker: str, iv: int, nt: int, rate: str, ann: str | None, kind: str) -> dict:
    return {"obs_ts": obs_ts, "ticker": ticker, "interval_seconds": iv, "next_timestamp": nt,
            "interval_rate": rate, "annualized_rate": ann or "", "kind": kind}


def _fund(pay_ts: int, ticker: str, iv: int, rate: str | None, ann: str | None, status: str, basis: str | None) -> dict:
    return {"pay_ts": pay_ts, "ticker": ticker, "interval_seconds": iv, "interval_rate": rate if status != ST_MISSING else None,
            "annualized_rate": ann if status != ST_MISSING else None, "status": status, "basis_obs_ts": basis}


def missing_rows(ticker: str, after_pay_ts: int | None, before_pay_ts: int, step: int) -> list[dict]:
    """after_pay_ts(排他)と before_pay_ts(排他)の間に step 刻みの支払時刻があれば missing 行を作る。
    刻みは before_pay_ts から逆算する(新しい方の間隔に揃える)。after_pay_ts が None(初めて見る銘柄)なら作らない。"""
    if after_pay_ts is None or step <= 0:
        return []
    out = []
    p = before_pay_ts - step
    while p > after_pay_ts:
        out.append(_fund(p, ticker, step, None, None, ST_MISSING, None))
        p -= step
    out.reverse()
    return out


def process_run(perps, fetch_ts: str, prev_obs: dict[str, dict], prev_changes: dict[str, int],
                prev_fetch_ts: str | None, last_pay: dict[str, int], mode: str) -> RunResult:
    """1 回の取得を処理する。
    perps: model.Perp の列(has_funding のものだけ使う)。fetch_ts: この取得の時刻(ISO)。
    prev_obs: 銘柄 → 直近の観測行(dict、store.load_last_obs の結果)。
    prev_changes: 銘柄 → 現在の窓での C 行の数。prev_fetch_ts: 直前に成功した取得の時刻(全銘柄共通)。
    last_pay: 銘柄 → 最後に記録した pay_ts。"""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    r = RunResult()
    fetch_unix = to_unix(parse_iso(fetch_ts))
    for p in perps:
        if not p.has_funding:
            continue
        t, iv, nt, rate, ann = p.ticker, int(p.interval_seconds), int(p.next_timestamp), p.interval_rate, p.annualized_rate
        prev = prev_obs.get(t)
        if prev is None:
            # 初めて見る銘柄(新規上場、または観測ログの保持期間より長く不在だった)
            r.n_new += 1
            r.obs_rows.append(_obs(fetch_ts, t, iv, nt, rate, ann, KIND_FIRST))
            if mode == MODE_CONFIRMED:
                pay = nt - iv
                r.funding_rows.extend(missing_rows(t, last_pay.get(t), pay, iv))
                if last_pay.get(t) is None or pay > last_pay[t]:
                    r.funding_rows.append(_fund(pay, t, iv, rate, ann, ST_CONFIRMED, fetch_ts))
            continue
        piv, pnt, prate, pann = int(prev["interval_seconds"]), int(prev["next_timestamp"]), prev["interval_rate"], prev.get("annualized_rate") or None
        if nt == pnt:
            if iv != piv:
                r.obs_rows.append(_obs(fetch_ts, t, iv, nt, rate, ann, KIND_CHANGE))
                r.anomalies.append(f"{t}: interval {piv}->{iv} inside window {nt}")
            elif not rate_equal(rate, prate):
                r.obs_rows.append(_obs(fetch_ts, t, iv, nt, rate, ann, KIND_CHANGE))
                if mode == MODE_CONFIRMED:
                    r.anomalies.append(f"{t}: rate changed inside window {nt} ({prate}->{rate})")
            continue
        if nt < pnt:
            # 時刻が戻った。窓を作り直すが FR は記録しない
            r.anomalies.append(f"{t}: next_timestamp went backwards {pnt}->{nt}")
            r.obs_rows.append(_obs(fetch_ts, t, iv, nt, rate, ann, KIND_FIRST))
            continue
        # 窓が進んだ(pnt → nt)
        r.n_rollover += 1
        # 旧窓の最後の観測: 直前の取得で同じ値を見ていたなら、その時刻を L 行に残す
        last_seen = prev["obs_ts"]
        if prev_fetch_ts and prev_fetch_ts > prev["obs_ts"]:
            last_seen = prev_fetch_ts
            r.obs_rows.append(_obs(prev_fetch_ts, t, piv, pnt, prate, pann, KIND_LAST))
        r.obs_rows.append(_obs(fetch_ts, t, iv, nt, rate, ann, KIND_FIRST))
        if mode == MODE_ESTIMATED:
            # pay_ts = pnt の推定値 = 旧窓で最後に見た値。旧窓の中で一度も観測できていなければ欠測
            window_start = pnt - piv
            last_seen_unix = to_unix(parse_iso(last_seen))
            lp = last_pay.get(t)
            r.funding_rows.extend(missing_rows(t, lp, pnt, piv))
            if lp is None or pnt > lp:
                if last_seen_unix > window_start:
                    r.funding_rows.append(_fund(pnt, t, piv, prate, pann, ST_ESTIMATED, last_seen))
                else:
                    r.funding_rows.append(_fund(pnt, t, piv, None, None, ST_MISSING, None))
            # 矛盾判定: 値が動くはずの銘柄が 25 分以上動かなかった
            first_unix = to_unix(parse_iso(prev["obs_ts"])) if prev.get("kind") == KIND_FIRST else None
            if (prev_changes.get(t, 0) == 0 and not is_trivial_rate(prate, piv) and first_unix is not None
                    and last_seen_unix - first_unix >= STALL_SECONDS):
                r.anomalies.append(f"{t}: rate never moved inside window {pnt} ({prate})")
            # pnt と nt − iv の間に観測の無い窓があれば欠測
            r.funding_rows.extend(missing_rows(t, pnt, nt, iv))
        else:
            pay = nt - iv
            lp = last_pay.get(t)
            r.funding_rows.extend(missing_rows(t, lp, pay, iv))
            if lp is None or pay > lp:
                r.funding_rows.append(_fund(pay, t, iv, rate, ann, ST_CONFIRMED, fetch_ts))
        _ = fetch_unix
    return r


def rebuild_from_obs(obs_rows: list[dict], mode: str) -> list[dict]:
    """観測ログ(F/C/L 行)から確定 FR を作り直す。ログの保持期間(30 日)の範囲に限られる。
    銘柄ごとに窓(next_timestamp)でまとめ、confirmed は窓の最初の値、estimated は窓の最後の値を使う。"""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    by_t: dict[str, list[dict]] = {}
    for r in obs_rows:
        by_t.setdefault(r["ticker"], []).append(r)
    out: list[dict] = []
    for t, rows in by_t.items():
        rows.sort(key=lambda r: (r["obs_ts"], 0 if r["kind"] == KIND_LAST else 1))
        windows: list[dict] = []   # 窓ごとに first/last
        for r in rows:
            nt = int(r["next_timestamp"])
            if windows and windows[-1]["nt"] == nt:
                w = windows[-1]
                if r["obs_ts"] >= w["last_ts"]:
                    w["last_ts"], w["last_rate"], w["last_ann"], w["iv"] = r["obs_ts"], r["interval_rate"], r.get("annualized_rate") or None, int(r["interval_seconds"])
            else:
                windows.append({"nt": nt, "iv": int(r["interval_seconds"]), "first_ts": r["obs_ts"], "first_rate": r["interval_rate"],
                                "first_ann": r.get("annualized_rate") or None, "last_ts": r["obs_ts"], "last_rate": r["interval_rate"],
                                "last_ann": r.get("annualized_rate") or None})
        windows.sort(key=lambda w: w["nt"])
        last_pay: int | None = None
        for i, w in enumerate(windows):
            if mode == MODE_CONFIRMED:
                pay = w["nt"] - w["iv"]
                out.extend(missing_rows(t, last_pay, pay, w["iv"]))
                out.append(_fund(pay, t, w["iv"], w["first_rate"], w["first_ann"], ST_CONFIRMED, w["first_ts"]))
                last_pay = pay
            else:
                if i == len(windows) - 1:
                    break   # 最後の窓はまだ閉じていない
                pay = w["nt"]
                out.extend(missing_rows(t, last_pay, pay, w["iv"]))
                if to_unix(parse_iso(w["last_ts"])) > pay - w["iv"]:
                    out.append(_fund(pay, t, w["iv"], w["last_rate"], w["last_ann"], ST_ESTIMATED, w["last_ts"]))
                else:
                    out.append(_fund(pay, t, w["iv"], None, None, ST_MISSING, None))
                last_pay = pay
                nxt = windows[i + 1]
                out.extend(missing_rows(t, pay, nxt["nt"], nxt["iv"]))
                if nxt["nt"] - nxt["iv"] > pay:
                    last_pay = nxt["nt"] - nxt["iv"]
    out.sort(key=lambda r: (r["pay_ts"], r["ticker"]))
    return out
