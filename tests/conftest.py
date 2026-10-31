"""共有の道具: 固定データ(API 応答の抜粋)の読み込み、メモリ上での改変、process_run を複数回まわす模擬。"""
from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest

from vfm.funding import KIND_CHANGE, KIND_FIRST, ST_MISSING, process_run, rebuild_from_obs
from vfm.model import Perp, Stats, parse_stats
from vfm.timeutil import fmt_iso, from_unix, parse_iso, to_unix

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "stats_sample.json"
MIN_LISTINGS = 50          # model.parse_stats が要求する最小件数(これ未満は SchemaError)
PAD_PREFIX = "PAD"         # 件数合わせに足す合成銘柄の接頭辞

T0 = 1792886400            # 固定データの next_timestamp = 2026-10-25T00:00:00Z
RUN1 = "2026-10-24T23:19:15Z"
RUN2 = "2026-10-24T23:31:30Z"
RUN3 = "2026-10-25T00:09:00Z"


def load_doc() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def perp_listings(doc: dict) -> list[dict]:
    return [x for x in doc["listings"] if x.get("contract_type") == "perpetual"]


def swap_listings(doc: dict) -> list[dict]:
    return [x for x in doc["listings"] if x.get("contract_type") == "swap"]


def funded_listings(doc: dict) -> list[dict]:
    return [x for x in perp_listings(doc) if isinstance(x.get("funding"), dict)]


def get_listing(doc: dict, ticker: str) -> dict:
    return next(x for x in doc["listings"] if x["ticker"] == ticker)


def pad_listings(doc: dict, n: int = MIN_LISTINGS) -> dict:
    """listings が n 件未満なら BTC-PERP の写し(PADnn-PERP)で埋める。doc を書き換えて返す。"""
    base = get_listing(doc, "BTC-PERP")
    i = 0
    while len(doc["listings"]) < n:
        x = copy.deepcopy(base)
        x.pop("quotes", None)
        x["ticker"] = f"{PAD_PREFIX}{i:02d}-PERP"
        x["name"] = f"Pad {i}"
        doc["listings"].append(x)
        i += 1
    return doc


def body(doc: dict, pad: bool = True) -> bytes:
    d = pad_listings(copy.deepcopy(doc)) if pad else doc
    return json.dumps(d, ensure_ascii=False).encode("utf-8")


def parse(doc: dict, pad: bool = True) -> Stats:
    return parse_stats(body(doc, pad))


def real_perps(doc: dict) -> list[Perp]:
    """固定データにある銘柄だけ(合成した PAD は除く)。"""
    return [p for p in parse(doc).perps if not p.ticker.startswith(PAD_PREFIX)]


def advance(doc: dict, k: int = 1, tickers: set[str] | None = None) -> dict:
    """funding のある perp の next_timestamp を k 窓分進める(窓の切り替わりの模擬)。"""
    for x in funded_listings(doc):
        if tickers is None or x["ticker"] in tickers:
            f = x["funding"]
            f["next_timestamp"] += k * f["interval_seconds"]
    return doc


def advance_to(doc: dict, t_unix: int) -> dict:
    """next_timestamp が t_unix 以下の銘柄は、t_unix より後の最初の窓境界まで進める(長い欠測の模擬)。"""
    for x in funded_listings(doc):
        f = x["funding"]
        iv, nt = f["interval_seconds"], f["next_timestamp"]
        if nt <= t_unix:
            f["next_timestamp"] = nt + iv * ((t_unix - nt) // iv + 1)
    return doc


def set_rate(doc: dict, ticker: str, rate: str) -> dict:
    f = get_listing(doc, ticker)["funding"]
    f["last"]["interval_rate"] = rate
    ann = Decimal(rate) * Decimal(365 * 86400) / Decimal(f["interval_seconds"])
    f["last"]["annualized_rate"] = format(ann.normalize(), "f")
    return doc


def set_interval(doc: dict, ticker: str, interval_seconds: int) -> dict:
    get_listing(doc, ticker)["funding"]["interval_seconds"] = interval_seconds
    return doc


class Simulator:
    """collect.run_once が CSV から復元する状態(直近の観測、窓内の C 行数、直前の取得時刻、最後の pay_ts)を
    メモリ上で持ち、process_run を繰り返す。"""

    def __init__(self, mode: str):
        self.mode = mode
        self.prev_obs: dict[str, dict] = {}
        self.prev_fetch_ts: str | None = None
        self.last_pay: dict[str, int] = {}
        self.absent: list[str] = []            # meta["absent"]: 直前の取得に居なかった銘柄
        self.obs_rows: list[dict] = []
        self.funding_rows: list[dict] = []
        self.results = []
        # (ticker, next_timestamp) ごとの C 行の数と最後の F 行の時刻(prev_changes を毎回全行なめずに済ませる)
        self._n_change: dict[tuple[str, int], int] = {}
        self._first_ts: dict[tuple[str, int], str] = {}
        self._first_iv: dict[tuple[str, int], int] = {}

    def prev_changes(self) -> tuple[dict[str, int], dict[str, str], dict[str, int]]:
        """collect.load_prev_changes と同じ: 現在の窓(直近の観測の next_timestamp)の最新の F 行の時刻と間隔、その後の C 行の数。
        F 行が来たら C 行の数を 0 に戻すことで「最新の F 行より後の C 行だけ数える」を増分で持つ。"""
        counts: dict[str, int] = {}
        first: dict[str, str] = {}
        first_iv: dict[str, int] = {}
        for t, cur in self.prev_obs.items():
            k = (t, int(cur["next_timestamp"]))
            if self._n_change.get(k):
                counts[t] = self._n_change[k]
            if k in self._first_ts:
                first[t], first_iv[t] = self._first_ts[k], self._first_iv[k]
        return counts, first, first_iv

    def run(self, perps, fetch_ts: str):
        changes, first, first_iv = self.prev_changes()
        res = process_run(perps, fetch_ts, self.prev_obs, changes, self.prev_fetch_ts, self.last_pay, self.mode, first,
                          absent_prev=self.absent, prev_first_iv=first_iv)
        self.absent = list(res.absent)
        for r in res.obs_rows:
            cur = self.prev_obs.get(r["ticker"])
            if cur is None or r["obs_ts"] > cur["obs_ts"]:
                self.prev_obs[r["ticker"]] = r
        for r in res.funding_rows:
            if int(r["pay_ts"]) > self.last_pay.get(r["ticker"], -1):
                self.last_pay[r["ticker"]] = int(r["pay_ts"])
        for r in res.obs_rows:
            k = (r["ticker"], int(r["next_timestamp"]))
            if r["kind"] == KIND_CHANGE:
                self._n_change[k] = self._n_change.get(k, 0) + 1
            elif r["kind"] == KIND_FIRST:
                self._first_ts[k], self._first_iv[k] = r["obs_ts"], int(r["interval_seconds"])
                self._n_change[k] = 0
        self.obs_rows.extend(res.obs_rows)
        self.funding_rows.extend(res.funding_rows)
        self.prev_fetch_ts = fetch_ts
        self.results.append(res)
        return res


# ---------------------------------------------------------------- 窓のシナリオを短く書く道具(時刻は 2026-10-24 UTC 基準)

H = 3600
DAY = 1792800000                     # 2026-10-24T00:00:00Z


def at(hm: str, day: int = 0) -> str:
    """'HH:MM' または 'HH:MM:SS'(2026-10-24 + day 日)→ ISO。"""
    h, m, *s = (int(x) for x in hm.split(":"))
    return fmt_iso(from_unix(DAY + day * 86400 + h * H + m * 60 + (s[0] if s else 0)))


def hm(ts: int) -> str:
    """unix 秒 → 'HH:MM'(2026-10-24 以外は '+1d' などを付ける)。"""
    d, rest = divmod(ts - DAY, 86400)
    s = f"{rest // H:02d}:{rest % H // 60:02d}"
    return s if d == 0 else f"{s}{d:+d}d"


def sample(iv_h: float, nt_h: float, rate: str | None = "0.00011", ticker: str = "X-PERP") -> Perp:
    """1 銘柄の標本。間隔 iv_h 時間、next_timestamp = 2026-10-24T00:00 + nt_h 時間。rate=None は funding.last が null。"""
    return Perp(ticker, ticker, "1", "1", "1", "1", "1", int(iv_h * H), DAY + int(nt_h * H), rate, None)


def play(mode: str, steps) -> Simulator:
    """steps: [(時刻 'HH:MM', day, 標本 / 標本の list / None)] を順に process_run に通した Simulator。"""
    sim = Simulator(mode)
    for hm_, day, perps in steps:
        perps = [] if perps is None else (perps if isinstance(perps, list) else [perps])
        sim.run(perps, at(hm_, day))
    return sim


def pay_rows(sim: Simulator, ticker: str = "X-PERP") -> list[tuple]:
    """(支払時刻, 状態の頭文字, 間隔(時間), rate, 根拠の時刻) を pay_ts 順に。"""
    out = []
    for r in sorted(sim.funding_rows, key=lambda r: r["pay_ts"]):
        if r["ticker"] == ticker:
            basis = hm(to_unix(parse_iso(r["basis_obs_ts"]))) if r["basis_obs_ts"] else None
            out.append((hm(r["pay_ts"]), r["status"][0], r["interval_seconds"] // H, r["interval_rate"], basis))
    return out


def full_row(r: dict) -> tuple:
    return (int(r["pay_ts"]), r["ticker"], int(r["interval_seconds"]), r["interval_rate"], r["annualized_rate"],
            r["status"], r["basis_obs_ts"])


def assert_rebuild_matches_live(sim: Simulator) -> None:
    """観測ログからの作り直しが本番と全列一致、(銘柄, pay_ts) の重複なし、missing は rate なし(0 にもしない)。"""
    assert sorted(map(full_row, rebuild_from_obs(sim.obs_rows, sim.mode))) == sorted(map(full_row, sim.funding_rows))
    keys = [(r["ticker"], r["pay_ts"]) for r in sim.funding_rows]
    assert len(keys) == len(set(keys))
    for r in sim.funding_rows:
        if r["status"] == ST_MISSING:
            assert r["interval_rate"] is None and r["annualized_rate"] is None and r["basis_obs_ts"] is None
        else:
            assert r["interval_rate"] is not None


@pytest.fixture
def doc() -> dict:
    return load_doc()
