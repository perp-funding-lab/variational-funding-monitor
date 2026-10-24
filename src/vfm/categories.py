"""種類(category)の判定。優先順: 上書き CSV → 名前のキーワード(株式・ETF → コモディティ → 指数)→ HL 本体にある → HIP-3 の株式にある → 暗号資産。"""
from __future__ import annotations

import csv
import re
from pathlib import Path

CRYPTO, EQUITY, COMMODITY, INDEX, PREIPO, OTHER = "crypto", "equity", "commodity", "index", "preipo", "other"
CATEGORIES = (CRYPTO, EQUITY, COMMODITY, INDEX, PREIPO, OTHER)
LABEL_JA = {CRYPTO: "暗号資産", EQUITY: "株式・ETF", COMMODITY: "コモディティ", INDEX: "指数", PREIPO: "プレIPO", OTHER: "その他"}

# 単語境界で見る(大文字小文字は区別しない)。"Inc." の "." は \b の外なので別扱い
_EQUITY = re.compile(r"\b(Inc|Incorporated|Corp|Corporation|Company|Co|Ltd|Limited|PLC|Holdings|Holding|ETF|Trust|Group|S\.A|N\.V|A/S|Oyj|S\.p\.A|GmbH|AG|SE|Bancorp|Technologies|Pharmaceuticals|Therapeutics|Fund|Platforms|Industries|Enterprises|Partners|Motors|Systems|Common Stock|Depositary Shares|Class [AB])\b\.?", re.I)
_COMMODITY = re.compile(r"\b(Gold|Silver|Crude|Crude Oil|Brent|WTI|Natural Gas|Gasoline|Heating Oil|Copper|Platinum|Palladium|Aluminum|Aluminium|Nickel|Zinc|Wheat|Corn|Soybeans?|Coffee|Cocoa|Sugar|Cotton|Uranium|Lithium|Iron Ore|Steel|Lumber|Cattle|Hogs)\b", re.I)
_INDEX = re.compile(r"\b(Index|S&P|SP500|Nasdaq|Russell|Dow Jones|Dow|Nikkei|KOSPI|DAX|FTSE|CAC|Hang Seng|TOPIX|US500|US MidCap|US SmallCap|VIX|Volatility)\b", re.I)


def load_overrides(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not Path(path).exists():
        return out
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            t, c = (r.get("ticker") or "").strip(), (r.get("category") or "").strip()
            if t and c in CATEGORIES:
                out[t] = c
    return out


def keyword_category(name: str) -> str | None:
    if not name:
        return None
    if _EQUITY.search(name):
        return EQUITY
    if _COMMODITY.search(name):
        return COMMODITY
    if _INDEX.search(name):
        return INDEX
    return None


def base_symbol(ticker: str) -> str:
    """'BTC-PERP' → 'BTC'、'XAU-SWAP' → 'XAU'。"""
    return re.sub(r"-(PERP|SWAP)$", "", ticker)


def classify(ticker: str, name: str, overrides: dict[str, str], hl_main: set[str], hip3_equity: set[str]) -> str:
    if ticker in overrides:
        return overrides[ticker]
    k = keyword_category(name)
    if k:
        return k
    b = base_symbol(ticker)
    if b in hl_main or ("k" + b) in hl_main or (b.startswith("1000") and b[4:] in hl_main) or (b.startswith("1000") and "k" + b[4:] in hl_main):
        return CRYPTO
    if b in hip3_equity:
        return EQUITY
    return CRYPTO
