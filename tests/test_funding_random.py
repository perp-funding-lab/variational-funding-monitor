"""無作為な取得の列(種を固定)で、本番の process_run と観測ログからの作り直し(rebuild_from_obs)が行単位で一致すること。
あわせて、偽の API が実際に返した値と照らして、推定・確定の根拠が正しいこと(最後/最初の観測であること)を確かめる。

各シナリオ: 1〜3 銘柄、約 3 日。取得は 10 分 ± 2.5 分ごと、ときどき数時間止まる。支払間隔は 1h/4h/8h(UTC の区切り)で、
窓の途中や境目で変わることがある。API の時計のずれ(-100〜+200 秒)、値の変化、居ない回(funding.last が null / 一覧に無い)、
API の不整合(next_timestamp の巻き戻り、時計より先の窓、数時間前の値)を混ぜる。"""
from __future__ import annotations

import bisect
import functools
import random
from collections import Counter
from decimal import Decimal

import pytest

from tests.conftest import Simulator, full_row
from vfm.funding import ST_CONFIRMED, ST_ESTIMATED, ST_MISSING, is_fresh, rebuild_from_obs
from vfm.model import Perp
from vfm.timeutil import fmt_iso, from_unix, parse_iso, to_unix

H = 3600
IVS = (H, 4 * H, 8 * H)
BASE = 1792540800                    # 2026-10-21T00:00:00Z
SEED = 20261101                      # 固定(変えると別のシナリオの集合になる)
N_SCENARIOS = 300
CHUNKS = 3
KINDS = ("nofund", "gone", "back", "ahead", "stale")
ACCEPTABLE_GLITCHES = {"back", "stale"}   # 本番が「窓の途中の間隔変更」として受け付けうる不整合


def _iv_at(sched, te):
    iv = sched[0][1]
    for tc, new in sched[1:]:
        if te >= tc:
            iv = new
    return iv


@functools.lru_cache(maxsize=None)
def scenario(i: int) -> dict:
    rng = random.Random(SEED * 1000 + i)
    t = BASE + rng.randrange(86400)
    end = t + rng.randrange(60 * H, 84 * H)
    times = []
    while t < end:
        times.append(t)
        t += rng.randrange(2 * H, 14 * H) if rng.random() < 0.006 else 600 + rng.randrange(-150, 151)
    n = len(times)
    tickers = []
    for k in range(rng.randint(1, 3)):
        iv = rng.choice(IVS)
        sched, changes = [(None, iv)], []
        for _ in range(rng.choice((0, 0, 1, 1, 2))):
            new = rng.choice([x for x in IVS if x != iv])
            tc = rng.randrange(times[0], times[-1])
            if rng.random() < 0.35:
                tc = tc // max(iv, new) * max(iv, new)            # 両方の区切りにちょうど重なる変更
            changes.append((tc, new))
            iv = new
        sched += sorted(changes)
        episodes = []
        for _ in range(rng.choice((0, 1, 2, 3, 4))):
            i0 = rng.randrange(n)
            episodes.append((i0, i0 + rng.choice((1, 1, 2, 3, 6, 12, 30, 60)), rng.choice(KINDS)))
        start = 0 if rng.random() < 0.7 else rng.randrange(n)      # 途中から上場
        stop = n if rng.random() < 0.8 else rng.randrange(start, n + 1)   # 途中で上場廃止
        tickers.append({"name": f"T{k}-PERP", "sched": sched, "lag": rng.randrange(-100, 201),
                        "p_rate": rng.choice((0.0, 0.1, 0.5, 1.0)), "trivial": rng.random() < 0.15,
                        "episodes": episodes, "start": start, "stop": stop})
    samples, served, clean, kinds_used, rates = [], {}, {}, set(), {}
    for idx, ft in enumerate(times):
        ts = fmt_iso(from_unix(ft))
        perps = []
        for tk in tickers:
            name = tk["name"]
            te = ft - tk["lag"]
            iv = _iv_at(tk["sched"], te)
            nt = (te // iv + 1) * iv
            if tk["trivial"]:
                rate = "0" if name.startswith("T0") else format(Decimal("0.0000125") * iv / 3600, "f")
            else:
                if name not in rates or rng.random() < tk["p_rate"]:
                    rates[name] = f"{rng.uniform(-0.0004, 0.0006):.7f}"
                rate = rates[name]
            kind = None if tk["start"] <= idx < tk["stop"] else "gone"
            if kind is None:
                kind = next((kd for a, b, kd in tk["episodes"] if a <= idx < b), None)
            if kind == "gone":
                continue
            if kind:
                kinds_used.add(kind)
            if kind == "back":
                nt -= iv * rng.choice((1, 2))
            elif kind == "ahead":
                nt += 86400 * rng.choice((1, 2, 3))
            elif kind == "stale":
                te2 = te - rng.choice((H, 4 * H, 8 * H))
                iv = _iv_at(tk["sched"], te2)
                nt = (te2 // iv + 1) * iv
            ann = format((Decimal(rate) * 365 * 86400 / iv).normalize(), "f")
            if kind == "nofund":
                perps.append(Perp(name, name, "1", "1", "1", "1", "1", iv, nt, None, None))
                continue
            perps.append(Perp(name, name, "1", "1", "1", "1", "1", iv, nt, rate, ann))
            served[(ts, name)] = (nt, iv, rate, ft)
            if kind is None:
                clean.setdefault(name, []).append((ft, nt, iv))
        samples.append((ts, perps))
    changing = frozenset(tk["name"] for tk in tickers if len({iv for _, iv in tk["sched"]}) > 1)
    # 根拠の照合用: 銘柄ごと、窓(next_timestamp)ごと / 確定値型の支払時刻(nt − iv)ごとの、乱れの無い観測の時刻(昇順)
    by_nt: dict[tuple[str, int], list[int]] = {}
    by_pay: dict[tuple[str, int], list[int]] = {}
    for name, lst in clean.items():
        for ft, nt, iv in lst:
            by_nt.setdefault((name, nt), []).append(ft)
            by_pay.setdefault((name, nt - iv), []).append(ft)
    seen_nt_iv: dict[tuple[str, int], set[int]] = {}
    for (ts, name), (nt, iv, _, _) in served.items():
        seen_nt_iv.setdefault((name, nt), set()).add(iv)
    return {"times": times, "samples": samples, "served": served, "by_nt": by_nt, "by_pay": by_pay,
            "seen_nt_iv": seen_nt_iv, "kinds": kinds_used, "changing": changing}


def _unix(s: str) -> int:
    return to_unix(parse_iso(s))


@functools.lru_cache(maxsize=None)
def check(i: int, mode: str) -> tuple[tuple[str, ...], tuple[tuple[str, int], ...]]:
    """(問題の一覧, 通った分岐の数え上げ)。重い Simulator は持ち越さない。"""
    sc = scenario(i)
    sim = Simulator(mode)
    emitted = []
    for ts, perps in sc["samples"]:
        res = sim.run(perps, ts)
        emitted.extend([_unix(ts)] * len(res.funding_rows))
    live, probs = sim.funding_rows, []

    def bad(msg):
        probs.append(f"seed#{i} {mode}: {msg}")

    if sorted(map(full_row, live)) != sorted(map(full_row, rebuild_from_obs(sim.obs_rows, mode))):
        bad("rebuild_from_obs != live")
    keys = [(r["ticker"], r["pay_ts"]) for r in live]
    if len(keys) != len(set(keys)):
        bad("duplicate (ticker, pay_ts)")
    strict = not (sc["kinds"] & ACCEPTABLE_GLITCHES)
    for r, e in zip(live, emitted):
        P, iv, t, st = int(r["pay_ts"]), int(r["interval_seconds"]), r["ticker"], r["status"]
        where = f"{t} pay {fmt_iso(from_unix(P))} {st}"
        if st == ST_MISSING:
            if r["interval_rate"] is not None or r["annualized_rate"] is not None or r["basis_obs_ts"] is not None:
                bad(f"{where}: missing row carries a value")
        elif r["interval_rate"] is None or r["basis_obs_ts"] is None:
            bad(f"{where}: value row without rate/basis")
        if P > sc["times"][-1] + 120:
            bad(f"{where}: pay_ts after the last fetch")
        if P % iv:
            bad(f"{where}: pay_ts not aligned to its interval")
        if st != ST_MISSING:
            got = sc["served"].get((r["basis_obs_ts"], t))
            if got is None:
                bad(f"{where}: basis {r['basis_obs_ts']} was not served")
                continue
            nt, siv, srate, _ = got
            if Decimal(srate) != Decimal(r["interval_rate"]):
                bad(f"{where}: rate differs from the basis sample")
            if st == ST_ESTIMATED:
                if nt != P or siv != iv:
                    bad(f"{where}: basis sample is not the window ending at pay_ts")
                if not is_fresh(_unix(r["basis_obs_ts"]), P, iv):
                    bad(f"{where}: stale basis")
            else:
                # 確定値の支払時刻は「旧窓の支払時刻と新しい窓の開始の遅い方」。境目で間隔が延びたときは
                # 新しい窓の開始(nt − siv)より後の旧窓の支払時刻になり、そのときの間隔は旧窓のもの(siv と違う)
                if not (nt - siv == P or (nt - siv < P < nt and siv != iv)):
                    bad(f"{where}: basis sample's window does not start at pay_ts")
                # 旧窓の支払時刻の確定値は旧窓の(最後に観測した)間隔で記録する
                if siv != iv and iv not in sc["seen_nt_iv"].get((t, P), ()):
                    bad(f"{where}: interval {iv} is neither the sample's nor the old window's")
        if strict:   # 乱れが受け付けられないシナリオ: 根拠は窓の最後(見込み)/ 最初(確定)の乱れの無い観測
            if mode == ST_ESTIMATED:
                fs = sc["by_nt"].get((t, P), [])
                fs = fs[:bisect.bisect_left(fs, e)]                 # この行を書いた回より前
                fresh = bool(fs) and is_fresh(fs[-1], P, iv)
                if st == ST_ESTIMATED and (not fs or _unix(r["basis_obs_ts"]) != fs[-1]):
                    bad(f"{where}: basis is not the last clean observation of the window")
                if st == ST_MISSING and fresh:
                    bad(f"{where}: missing although a fresh observation existed")
            else:
                got = sc["served"].get((r["basis_obs_ts"], t)) if st == ST_CONFIRMED else None
                if got and got[0] - got[1] != P:
                    # 境目で間隔が延びた: 根拠は新しい窓(開始 nt − siv)のうち、旧窓の支払時刻 P 以降の最初の観測
                    fs = [f for f in sc["by_pay"].get((t, got[0] - got[1]), []) if f >= P - 120 and sc["served"].get((fmt_iso(from_unix(f)), t), (None,))[0] == got[0]]
                else:
                    fs = sc["by_pay"].get((t, P), [])
                fs = fs[:bisect.bisect_right(fs, e)]                # この行を書いた回を含む
                if st == ST_CONFIRMED and (not fs or _unix(r["basis_obs_ts"]) != fs[0]):
                    bad(f"{where}: basis is not the first clean observation of the window")
                if st == ST_MISSING and [f for f in fs if f < e]:
                    bad(f"{where}: missing although the window was observed")
    by: dict[str, list[tuple[int, int]]] = {}   # 間隔が変わらない銘柄の支払時刻は 1 間隔ずつ切れ目なく続く
    for r in live:
        if r["ticker"] not in sc["changing"]:
            by.setdefault(r["ticker"], []).append((int(r["pay_ts"]), int(r["interval_seconds"])))
    if by:
        for t, ps in by.items():
            ps.sort()
            if any(b - a != iv for (a, _), (b, iv) in zip(ps, ps[1:])):
                bad(f"{t}: pay times are not contiguous")
    cov = Counter()
    for res in sim.results:
        cov["mid_window_interval_change"] += res.n_interval_changes
        cov["absent_ticker_runs"] += len(res.absent)
        for a in res.anomalies:
            cov["same_window_interval_change"] += "inside window" in a and ": interval " in a
            cov["ignored_before_payment"] += "before the payment time (ignored)" in a
            cov["ahead_of_clock"] += "ahead of clock" in a
    for r in live:
        cov[f"rows_{r['status']}"] += 1
    return tuple(probs), tuple(sorted(cov.items()))


@pytest.mark.parametrize("chunk", range(CHUNKS))
@pytest.mark.parametrize("mode", [ST_ESTIMATED, ST_CONFIRMED])
def test_random_scenarios_rebuild_equals_live_and_bases_match_the_api(mode, chunk):
    per = N_SCENARIOS // CHUNKS
    problems = [p for i in range(chunk * per, (chunk + 1) * per) for p in check(i, mode)[0]]
    assert problems == [], f"{len(problems)} problems, first: {problems[:5]}"


def test_random_scenarios_exercise_the_interesting_branches():
    total = Counter()
    for i in range(N_SCENARIOS):
        for mode in (ST_ESTIMATED, ST_CONFIRMED):
            total.update(dict(check(i, mode)[1]))
    for key in ("mid_window_interval_change", "same_window_interval_change", "ignored_before_payment", "ahead_of_clock",
                "absent_ticker_runs", "rows_missing", "rows_estimated", "rows_confirmed"):
        assert total[key] >= 20, (key, dict(total))


def test_random_scenarios_are_deterministic():
    a, b = scenario(7), scenario(7)
    assert a is b                                              # キャッシュ
    scenario.cache_clear()
    c = scenario(7)
    assert [(ts, [(p.ticker, p.interval_seconds, p.next_timestamp, p.interval_rate) for p in ps]) for ts, ps in c["samples"]] == \
        [(ts, [(p.ticker, p.interval_seconds, p.next_timestamp, p.interval_rate) for p in ps]) for ts, ps in a["samples"]]
