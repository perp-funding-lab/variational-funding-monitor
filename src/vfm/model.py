"""stats の JSON を検証して扱いやすい形にする。形が想定と違えば SchemaError(その回は何も記録しない)。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any


class SchemaError(Exception):
    pass


# 全銘柄が 1 回で返る想定(2026-10 時点で 582)。これより少なければ形が変わったとみなして記録しない
MIN_LISTINGS = 50


@dataclass
class Perp:
    ticker: str
    name: str
    mark_price: str | None
    long_oi: str | None
    short_oi: str | None
    volume_24h: str | None
    spread_bps: str | None
    # funding(無ければ None)
    interval_seconds: int | None = None
    next_timestamp: int | None = None
    interval_rate: str | None = None
    annualized_rate: str | None = None

    @property
    def has_funding(self) -> bool:
        return self.interval_seconds is not None and self.next_timestamp is not None and self.interval_rate is not None


@dataclass
class Swap:
    ticker: str
    name: str
    mark_price: str | None
    long_oi: str | None
    short_oi: str | None
    volume_24h: str | None
    interval_seconds: int | None
    next_timestamp: int | None
    last: dict[str, str] | None
    next: dict[str, str] | None


@dataclass
class Stats:
    perps: list[Perp]
    swaps: list[Swap]
    num_markets: int | None
    skipped: list[str] = field(default_factory=list)   # 形がおかしくて飛ばした銘柄


def _num_str(v: Any) -> str | None:
    """数値文字列(または数値)を検証して文字列で返す。壊れていれば None。"""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        v = repr(v)
    if not isinstance(v, str):
        return None
    try:
        Decimal(v)
    except InvalidOperation:
        return None
    return v


def _int(v: Any) -> int | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return int(v)


def parse_stats(body: bytes | str) -> Stats:
    try:
        d = json.loads(body)
    except Exception as e:
        raise SchemaError(f"not json: {e}") from e
    if not isinstance(d, dict) or not isinstance(d.get("listings"), list):
        raise SchemaError("listings missing")
    listings = d["listings"]
    if len(listings) < MIN_LISTINGS:
        raise SchemaError(f"too few listings: {len(listings)}")
    perps: list[Perp] = []
    swaps: list[Swap] = []
    skipped: list[str] = []
    for x in listings:
        if not isinstance(x, dict) or not isinstance(x.get("ticker"), str):
            skipped.append(repr(x)[:40])
            continue
        t = x["ticker"]
        oi = x.get("open_interest") if isinstance(x.get("open_interest"), dict) else {}
        f = x.get("funding") if isinstance(x.get("funding"), dict) else None
        ctype = x.get("contract_type")
        name = x.get("name") if isinstance(x.get("name"), str) else t
        if ctype == "perpetual":
            p = Perp(t, name, _num_str(x.get("mark_price")), _num_str(oi.get("long_open_interest")),
                     _num_str(oi.get("short_open_interest")), _num_str(x.get("volume_24h")), _num_str(x.get("base_spread_bps")))
            if f is not None:
                last = f.get("last") if isinstance(f.get("last"), dict) else None
                iv, nt = _int(f.get("interval_seconds")), _int(f.get("next_timestamp"))
                rate = _num_str(last.get("interval_rate")) if last else None
                ann = _num_str(last.get("annualized_rate")) if last else None
                if iv and iv > 0 and nt and rate is not None:
                    p.interval_seconds, p.next_timestamp, p.interval_rate, p.annualized_rate = iv, nt, rate, ann
                elif last is not None:
                    skipped.append(f"{t}: funding shape")
            perps.append(p)
        elif ctype == "swap":
            fl = f.get("last") if f and isinstance(f.get("last"), dict) else None
            fn = f.get("next") if f and isinstance(f.get("next"), dict) else None
            swaps.append(Swap(t, name, _num_str(x.get("mark_price")), _num_str(oi.get("long_open_interest")),
                              _num_str(oi.get("short_open_interest")), _num_str(x.get("volume_24h")),
                              _int(f.get("interval_seconds")) if f else None, _int(f.get("next_timestamp")) if f else None,
                              {k: v for k, v in fl.items() if _num_str(v) is not None} if fl else None,
                              {k: v for k, v in fn.items() if _num_str(v) is not None} if fn else None))
        else:
            skipped.append(f"{t}: contract_type={ctype!r}")
    if not perps:
        raise SchemaError("no perpetual listings")
    return Stats(perps, swaps, _int(d.get("num_markets")), skipped)
