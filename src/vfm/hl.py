"""Hyperliquid との比較: 銘柄の対応付けと 7 日年率(fundingHistory)。1 日 1 回だけ動かす。"""
from __future__ import annotations

import csv
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from .categories import base_symbol
from .timeutil import fmt_iso, utc_now

HL_INFO = "https://api.hyperliquid.xyz/info"
USER_AGENT = "variational-funding-monitor/1.0"
# info のレート制限は 1 IP あたり 1200 weight/分、fundingHistory は weight 20 → 60 回/分。余裕を見て 1.1 秒間隔
PACE_SECONDS = 1.1
MAPPING_HEADER = ["ticker", "hl_coin", "note"]
SEVEN_DAYS_MS = 7 * 86400 * 1000


class HLError(Exception):
    pass


def hl_post(payload: dict, timeout: int = 20) -> object:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(HL_INFO, data=data, headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise HLError(f"HTTP {e.code} for {payload.get('type')}") from e


def fetch_universe(post: Callable[[dict], object] = hl_post, sleep: Callable[[float], None] = time.sleep) -> dict:
    """本体の銘柄と、HIP-3 の各 DEX の銘柄(OI ドル建て)を取る。"""
    meta = post({"type": "meta"})
    main = [x["name"] for x in meta.get("universe", []) if not x.get("isDelisted")]
    dexs = [d["name"] for d in (post({"type": "perpDexs"}) or []) if d and d.get("name")]
    hip3: dict[str, dict[str, dict]] = {}
    for dex in dexs:
        sleep(PACE_SECONDS)
        try:
            res = post({"type": "metaAndAssetCtxs", "dex": dex})
            uni, ctxs = res[0].get("universe", []), res[1]
        except Exception:
            continue
        coins: dict[str, dict] = {}
        for u, c in zip(uni, ctxs):
            if u.get("isDelisted"):
                continue
            short = u["name"].split(":")[-1]
            try:
                oi = float(c.get("openInterest") or 0) * float(c.get("markPx") or 0)
            except (TypeError, ValueError):
                oi = 0.0
            coins[short] = {"name": u["name"], "oi_usd": oi}
        hip3[dex] = coins
    return {"fetched": fmt_iso(utc_now()), "main": main, "hip3": hip3}


def load_mapping(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not Path(path).exists():
        return out
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("ticker"):
                out[r["ticker"]] = {"hl_coin": (r.get("hl_coin") or "").strip(), "note": r.get("note") or ""}
    return out


def resolve_one(ticker: str, universe: dict) -> tuple[str, str]:
    """XXX-PERP → HL の銘柄名。見つからなければ ('', '対応なし')。"""
    main = set(universe.get("main", []))
    b = base_symbol(ticker)
    if b in main:
        return b, "本体"
    if "k" + b in main:
        return "k" + b, "単位違い(k = 1000 倍)"
    m = re.match(r"^(1000000|1000|1)([A-Z].*)$", b)
    if m:
        rest = m.group(2)
        if rest in main:
            return rest, f"単位違い({m.group(1)} 倍)"
        if "k" + rest in main:
            return "k" + rest, f"単位違い({m.group(1)} 倍 ↔ k)"
    best: tuple[float, str, str] | None = None
    for dex, coins in universe.get("hip3", {}).items():
        if b in coins:
            oi = coins[b].get("oi_usd", 0.0)
            if best is None or oi > best[0]:
                best = (oi, dex, coins[b]["name"])
    if best:
        n_dex = sum(1 for coins in universe.get("hip3", {}).values() if b in coins)
        note = f"HIP-3 {best[1]}" + (f"(同名が {n_dex} DEX にあり OI 最大のもの)" if n_dex > 1 else "")
        return best[2], note
    return "", "対応なし"


def update_mapping(path: Path, perp_tickers: list[str], universe: dict) -> tuple[dict[str, dict], list[str]]:
    """対応表 CSV に無い銘柄だけ解決して追記する。既存行(手で直したもの)は変えない。"""
    mapping = load_mapping(path)
    added: list[str] = []
    rows = []
    for t in sorted(perp_tickers):
        if t in mapping:
            continue
        coin, note = resolve_one(t, universe)
        mapping[t] = {"hl_coin": coin, "note": note}
        rows.append({"ticker": t, "hl_coin": coin, "note": note})
        added.append(t)
    if rows:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        new = not Path(path).exists()
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=MAPPING_HEADER, lineterminator="\n")
            if new:
                w.writeheader()
            w.writerows(rows)
    return mapping, added


def apr_7d(entries: list[dict]) -> tuple[float | None, int]:
    """fundingHistory の配列 → (7 日年率, 件数)。年率 = 合計 ÷ 7 日 × 365 日。"""
    rates = []
    for e in entries:
        try:
            rates.append(float(e["fundingRate"]))
        except (KeyError, TypeError, ValueError):
            continue
    if not rates:
        return None, 0
    return sum(rates) / 7 * 365, len(rates)


def fetch_7d(coins: list[str], now_ms: int, post: Callable[[dict], object] = hl_post,
             sleep: Callable[[float], None] = time.sleep, deadline_s: float = 14 * 60) -> dict[str, dict]:
    """銘柄ごとに直近 7 日の fundingHistory を取る。合計 deadline_s を超えたら残りは諦める(タイムアウト付き)。"""
    out: dict[str, dict] = {}
    t0 = time.monotonic()
    for i, c in enumerate(sorted(set(coins))):
        if time.monotonic() - t0 > deadline_s:
            out.setdefault("_truncated", {"after": i})
            break
        if i:
            sleep(PACE_SECONDS)
        try:
            hist = post({"type": "fundingHistory", "coin": c, "startTime": now_ms - SEVEN_DAYS_MS})
        except HLError as e:
            if "429" in str(e):
                sleep(10)
                try:
                    hist = post({"type": "fundingHistory", "coin": c, "startTime": now_ms - SEVEN_DAYS_MS})
                except HLError:
                    continue
            else:
                continue
        if not isinstance(hist, list):
            continue
        apr, n = apr_7d(hist)
        if apr is not None:
            out[c] = {"apr7d": apr, "n": n}
    return out
