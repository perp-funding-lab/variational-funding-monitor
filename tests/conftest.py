"""共有の道具: 固定データ(API 応答の抜粋)の読み込み、メモリ上での改変、process_run を複数回まわす模擬。"""
from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest

from vfm.funding import KIND_CHANGE, KIND_FIRST, process_run
from vfm.model import Perp, Stats, parse_stats

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

    def prev_changes(self) -> tuple[dict[str, int], dict[str, str]]:
        """collect.load_prev_changes と同じ: 現在の窓の C 行の数と、窓の最初の観測時刻(F 行)。"""
        counts: dict[str, int] = {}
        first: dict[str, str] = {}
        for r in self.obs_rows:
            cur = self.prev_obs.get(r["ticker"])
            if not cur or int(r["next_timestamp"]) != int(cur["next_timestamp"]):
                continue
            if r["kind"] == KIND_CHANGE:
                counts[r["ticker"]] = counts.get(r["ticker"], 0) + 1
            elif r["kind"] == KIND_FIRST:
                first[r["ticker"]] = r["obs_ts"]
        return counts, first

    def run(self, perps, fetch_ts: str):
        changes, first = self.prev_changes()
        res = process_run(perps, fetch_ts, self.prev_obs, changes, self.prev_fetch_ts, self.last_pay, self.mode, first,
                          absent_prev=self.absent)
        self.absent = list(res.absent)
        for r in res.obs_rows:
            cur = self.prev_obs.get(r["ticker"])
            if cur is None or r["obs_ts"] > cur["obs_ts"]:
                self.prev_obs[r["ticker"]] = r
        for r in res.funding_rows:
            if int(r["pay_ts"]) > self.last_pay.get(r["ticker"], -1):
                self.last_pay[r["ticker"]] = int(r["pay_ts"])
        self.obs_rows.extend(res.obs_rows)
        self.funding_rows.extend(res.funding_rows)
        self.prev_fetch_ts = fetch_ts
        self.results.append(res)
        return res


@pytest.fixture
def doc() -> dict:
    return load_doc()
