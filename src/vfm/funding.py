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
# next_timestamp − interval(窓の開始)が取得時刻よりこれ以上未来なら API の値が怪しいので記録しない
CLOCK_SKEW_SECONDS = 120


@dataclass
class RunResult:
    obs_rows: list[dict] = field(default_factory=list)
    funding_rows: list[dict] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)      # 診断用の一覧(実行ログの note に出す)
    n_rollover: int = 0
    n_new: int = 0
    n_checked: int = 0          # 矛盾判定の対象になった窓の数(見込み値型: 0 でも金利成分でもなく 25 分以上観測できた窓)
    n_contradictions: int = 0   # 判定(funding_mode)と矛盾した窓の数(anomalies のうち、この判定に関わるものだけ)
    absent: list[str] = field(default_factory=list)   # 観測ログに居るのに今回の取得に居なかった銘柄


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
    刻みは before_pay_ts から逆算する。after_pay_ts が None(初めて見る銘柄)なら作らない。"""
    if after_pay_ts is None or step <= 0:
        return []
    out = []
    p = before_pay_ts - step
    while p > after_pay_ts:
        out.append(_fund(p, ticker, step, None, None, ST_MISSING, None))
        p -= step
    out.reverse()
    return out


def gap_rows_confirmed(ticker: str, lp: int | None, pnt: int, piv: int, pay: int, iv: int) -> list[dict]:
    """確定値型: 直前の記録 lp(排他)と今回記録する pay(排他)の間の欠測。
    pnt(旧窓の支払時刻)までは旧間隔 piv、pnt より後は新間隔 iv で刻む(間隔が変わっても偽の欠測を作らない)。"""
    if lp is None:
        return []
    out = []
    p = pnt
    while p > lp:
        if p < pay:
            out.append(_fund(p, ticker, piv, None, None, ST_MISSING, None))
        p -= piv
    out.reverse()
    out.extend(missing_rows(ticker, max(pnt, lp), pay, iv))
    return out


def process_run(perps, fetch_ts: str, prev_obs: dict[str, dict], prev_changes: dict[str, int],
                prev_fetch_ts: str | None, last_pay: dict[str, int], mode: str,
                prev_first_ts: dict[str, str] | None = None, absent_prev=None) -> RunResult:
    """1 回の取得を処理する。
    perps: model.Perp の列(has_funding のものだけ使う)。fetch_ts: この取得の時刻(ISO)。
    prev_obs: 銘柄 → 直近の観測行(dict、store.load_last_obs の結果)。
    prev_changes: 銘柄 → 現在の窓での C 行の数。prev_fetch_ts: 直前に成功した取得の時刻(全銘柄共通)。
    last_pay: 銘柄 → 最後に記録した pay_ts。prev_first_ts: 銘柄 → 現在の窓の最初の観測時刻(F 行の obs_ts)。
    absent_prev: 直前の成功した取得に居なかった銘柄(meta.absent)。その銘柄には prev_fetch_ts を「最後に見た時刻」として使わない。"""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    prev_first_ts = prev_first_ts or {}
    absent_prev = set(absent_prev or ())
    r = RunResult()
    fetch_unix = to_unix(parse_iso(fetch_ts))
    present: set[str] = set()
    for p in perps:
        if not p.has_funding:
            continue
        t, iv, nt, rate, ann = p.ticker, int(p.interval_seconds), int(p.next_timestamp), p.interval_rate, p.annualized_rate
        if nt - iv > fetch_unix + CLOCK_SKEW_SECONDS:
            # 窓の開始が取得時刻より未来 = API の値が怪しい。この回は記録しない(次の正しい値で通常の窓切り替えになる)
            r.anomalies.append(f"{t}: next_timestamp {nt} ahead of clock (window start {nt - iv} > {fetch_unix})")
            continue
        present.add(t)
        prev = prev_obs.get(t)
        if prev is None:
            # 初めて見る銘柄(新規上場、または観測ログの保持期間より長く不在だった)
            r.n_new += 1
            r.obs_rows.append(_obs(fetch_ts, t, iv, nt, rate, ann, KIND_FIRST))
            if mode == MODE_CONFIRMED:
                pay = nt - iv
                lp = last_pay.get(t)
                r.funding_rows.extend(missing_rows(t, lp, pay, iv))
                if lp is None or pay > lp:
                    r.funding_rows.append(_fund(pay, t, iv, rate, ann, ST_CONFIRMED, fetch_ts))
            continue
        piv, pnt, prate, pann = int(prev["interval_seconds"]), int(prev["next_timestamp"]), prev["interval_rate"], prev.get("annualized_rate") or None
        seen_prev = t not in absent_prev   # 直前の取得でもこの銘柄を見ていた(なら prev_fetch_ts が最後に見た時刻)
        if nt == pnt:
            if iv != piv:
                r.obs_rows.append(_obs(fetch_ts, t, iv, nt, rate, ann, KIND_CHANGE))
                r.anomalies.append(f"{t}: interval {piv}->{iv} inside window {nt}")
            elif not rate_equal(rate, prate):
                r.obs_rows.append(_obs(fetch_ts, t, iv, nt, rate, ann, KIND_CHANGE))
                if mode == MODE_CONFIRMED:
                    r.anomalies.append(f"{t}: rate changed inside window {nt} ({prate}->{rate})")
                    r.n_contradictions += 1
            if mode == MODE_CONFIRMED and not is_trivial_rate(prate, piv):
                r.n_checked += 1
            continue
        if nt < pnt:
            # 時刻が戻った(API の一時的な不整合)。この回の値は記録せず、この銘柄は不在扱いにする
            # (rebuild_from_obs も巻き戻った行を無視するので、本番と同じ結果になる)
            r.anomalies.append(f"{t}: next_timestamp went backwards {pnt}->{nt}")
            present.discard(t)
            continue
        # 窓が進んだ(pnt → nt)
        r.n_rollover += 1
        # 旧窓の最後の観測: 直前の取得でも同じ値を見ていたなら、その時刻を L 行に残す
        last_seen = prev["obs_ts"]
        if seen_prev and prev_fetch_ts and prev_fetch_ts > prev["obs_ts"]:
            last_seen = prev_fetch_ts
            r.obs_rows.append(_obs(prev_fetch_ts, t, piv, pnt, prate, pann, KIND_LAST))
        r.obs_rows.append(_obs(fetch_ts, t, iv, nt, rate, ann, KIND_FIRST))
        lp = last_pay.get(t)
        if mode == MODE_ESTIMATED:
            # pay_ts = pnt の推定値 = 旧窓で最後に見た値。旧窓の中で一度も観測できていなければ欠測
            window_start = pnt - piv
            last_seen_unix = to_unix(parse_iso(last_seen))
            r.funding_rows.extend(missing_rows(t, lp, pnt, piv))
            if lp is None or pnt > lp:
                if last_seen_unix > window_start:
                    r.funding_rows.append(_fund(pnt, t, piv, prate, pann, ST_ESTIMATED, last_seen))
                else:
                    r.funding_rows.append(_fund(pnt, t, piv, None, None, ST_MISSING, None))
            # 矛盾判定: 見込み値型なら窓の中で値が動くはず。0 でも金利成分でもない銘柄を 25 分以上観測できた窓を「検査」とし、
            # その中で一度も動かなかった窓を矛盾として数える(確定値型のように振る舞った窓)。
            if not is_trivial_rate(prate, piv):
                first_ts = prev_first_ts.get(t) or (prev["obs_ts"] if prev.get("kind") == KIND_FIRST else None)
                if first_ts is not None and last_seen_unix - to_unix(parse_iso(first_ts)) >= STALL_SECONDS:
                    r.n_checked += 1
                    if prev_changes.get(t, 0) == 0 and prev.get("kind") == KIND_FIRST:
                        r.anomalies.append(f"{t}: rate never moved inside window {pnt} ({prate})")
                        r.n_contradictions += 1
            # pnt と nt − iv の間に観測の無い窓があれば欠測(既に記録した支払より前には作らない)
            r.funding_rows.extend(missing_rows(t, max(pnt, lp) if lp is not None else pnt, nt, iv))
        else:
            pay = nt - iv
            r.funding_rows.extend(gap_rows_confirmed(t, lp, pnt, piv, pay, iv))
            if lp is None or pay > lp:
                r.funding_rows.append(_fund(pay, t, iv, rate, ann, ST_CONFIRMED, fetch_ts))
    # 今回の取得に居なかった銘柄: 直前の取得では居たなら、その時刻を「窓の最後の観測」として L 行に残す
    for t, prev in prev_obs.items():
        if t in present:
            continue
        r.absent.append(t)
        if t not in absent_prev and prev_fetch_ts and prev_fetch_ts > prev["obs_ts"]:
            r.obs_rows.append(_obs(prev_fetch_ts, t, int(prev["interval_seconds"]), int(prev["next_timestamp"]),
                                   prev["interval_rate"], prev.get("annualized_rate") or None, KIND_LAST))
    r.absent.sort()
    return r


def rebuild_from_obs(obs_rows: list[dict], mode: str) -> list[dict]:
    """観測ログ(F/C/L 行)から確定 FR を作り直す。ログの保持期間(30 日)の範囲に限られる。
    銘柄ごとに窓(next_timestamp)でまとめ、confirmed は窓の最初の値、estimated は窓の最後の値を使う。
    next_timestamp が戻った行(巻き戻り)は本番と同じく FR に寄与させない。"""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    by_t: dict[str, list[dict]] = {}
    for r in obs_rows:
        by_t.setdefault(r["ticker"], []).append(r)
    out: list[dict] = []
    for t, rows in by_t.items():
        rows.sort(key=lambda r: (r["obs_ts"], 0 if r["kind"] == KIND_LAST else 1))
        windows: list[dict] = []   # 窓ごとに first/last
        max_nt = -1
        for r in rows:
            nt = int(r["next_timestamp"])
            if nt < max_nt:
                continue   # 巻き戻り: 本番では FR を記録しない
            max_nt = nt
            if windows and windows[-1]["nt"] == nt:
                w = windows[-1]
                if r["obs_ts"] >= w["last_ts"]:
                    w["last_ts"], w["last_rate"], w["last_ann"], w["iv"] = r["obs_ts"], r["interval_rate"], r.get("annualized_rate") or None, int(r["interval_seconds"])
            else:
                windows.append({"nt": nt, "iv": int(r["interval_seconds"]), "first_ts": r["obs_ts"], "first_rate": r["interval_rate"],
                                "first_ann": r.get("annualized_rate") or None, "last_ts": r["obs_ts"], "last_rate": r["interval_rate"],
                                "last_ann": r.get("annualized_rate") or None})
        last_pay: int | None = None
        for i, w in enumerate(windows):
            if mode == MODE_CONFIRMED:
                pay = w["nt"] - w["iv"]
                if i == 0:
                    out.extend(missing_rows(t, last_pay, pay, w["iv"]))
                else:
                    pw = windows[i - 1]
                    out.extend(gap_rows_confirmed(t, last_pay, pw["nt"], pw["iv"], pay, w["iv"]))
                if last_pay is None or pay > last_pay:
                    out.append(_fund(pay, t, w["iv"], w["first_rate"], w["first_ann"], ST_CONFIRMED, w["first_ts"]))
                    last_pay = pay
            else:
                if i == len(windows) - 1:
                    break   # 最後の窓はまだ閉じていない
                pay = w["nt"]
                out.extend(missing_rows(t, last_pay, pay, w["iv"]))
                if last_pay is None or pay > last_pay:
                    if to_unix(parse_iso(w["last_ts"])) > pay - w["iv"]:
                        out.append(_fund(pay, t, w["iv"], w["last_rate"], w["last_ann"], ST_ESTIMATED, w["last_ts"]))
                    else:
                        out.append(_fund(pay, t, w["iv"], None, None, ST_MISSING, None))
                    last_pay = pay
                nxt = windows[i + 1]
                out.extend(missing_rows(t, max(pay, last_pay) if last_pay is not None else pay, nxt["nt"], nxt["iv"]))
                if nxt["nt"] - nxt["iv"] > (last_pay if last_pay is not None else pay):
                    last_pay = nxt["nt"] - nxt["iv"]
    out.sort(key=lambda r: (r["pay_ts"], r["ticker"]))
    return out
