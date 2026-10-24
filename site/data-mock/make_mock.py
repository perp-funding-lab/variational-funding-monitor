#!/usr/bin/env python3
"""開発用のモックデータを site/data/ に生成する(標準ライブラリのみ)。DESIGN.md 5 章の契約に合わせる。

  python3 site/data-mock/make_mock.py                   # 通常
  python3 site/data-mock/make_mock.py --stale-hours 3   # 最後の取得を 3 時間前にして「取得停止」の警告を試す
  python3 site/data-mock/make_mock.py --anomaly         # 判定の矛盾の警告を試す
  python3 site/data-mock/make_mock.py --details 60      # 詳細ファイルを OI 上位 60 銘柄分作る(既定 20)
  python3 site/data-mock/make_mock.py --mode estimated  # 見込み値型(支払いが全部「推定」)

生成物: summary.json(約 560 行)、tickers/<T>.json(OI 上位 N 銘柄 + 欠測・停止の例、約 3 か月分)、swaps.json(18 行)。
"""
import argparse
import json
import math
import random
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "data"
UTC = timezone.utc
INTEREST_H = 0.0000125          # 金利成分 0.00125% / 時(8 時間で 0.01%、年 10.95%)
YEAR = 365 * 86400

# ───────── 銘柄 ─────────
CRYPTO = ("BTC ETH SOL XRP DOGE ADA AVAX LINK DOT POL LTC BCH UNI ATOM NEAR APT ARB OP SUI SEI TIA INJ PEPE WIF BONK FET RENDER TAO HYPE ENA ONDO JUP "
          "PENDLE AAVE MKR LDO CRV SNX GMX DYDX STX FIL ICP HBAR ALGO VET EOS XLM TRX TON SHIB FLOKI ORDI SATS WLD STRK ZK BLUR IMX GALA SAND MANA AXS "
          "APE ENS COMP SUSHI 1INCH BAL RUNE KAS CFX FTM ROSE MINA CELO ZIL ONE KAVA OSMO JTO PYTH W ETHFI REZ BB IO ZRO NOT DOGS CATI EIGEN GOAT PNUT "
          "ACT MOODENG VIRTUAL AI16Z FARTCOIN TRUMP MELANIA BERA IP KAITO LAYER PAXG S PLUME WAL BABY INIT SIGN DEEP ZORA SOPH HOME SPK PUMP ASTER "
          "AVNT WLFI XPL LINEA 0G STBL MYX").split()
CRYPTO_NAMES = {"BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana", "XRP": "XRP", "DOGE": "Dogecoin", "ADA": "Cardano", "AVAX": "Avalanche",
                "LINK": "Chainlink", "DOT": "Polkadot", "LTC": "Litecoin", "BCH": "Bitcoin Cash", "UNI": "Uniswap", "HYPE": "Hyperliquid",
                "PEPE": "Pepe", "SHIB": "Shiba Inu", "TON": "Toncoin", "TRX": "TRON", "SUI": "Sui", "TAO": "Bittensor", "AAVE": "Aave"}
K_COINS = {"PEPE", "BONK", "FLOKI", "SHIB", "SATS", "DOGS"}      # HL では k 付き(kPEPE = 1000 PEPE)
EQUITY = ("AAPL MSFT NVDA TSLA AMZN GOOGL META NFLX AMD INTC COIN MSTR HOOD PLTR SPY QQQ IWM TLT GLD SLV BRK JPM GS V MA DIS BA XOM CVX PFE JNJ "
          "UNH WMT COST HD NKE SBUX MCD KO PEP ORCL CRM ADBE AVGO QCOM TXN MU ARM SMCI DELL IBM CSCO UBER ABNB SHOP SQ PYPL SOFI RIVN LCID NIO "
          "BABA PDD JD TSM ASML SONY TM SAP NVO LLY MRNA GME AMC RBLX SNAP PINS SPOT RDDT CRWD PANW NET DDOG SNOW MDB ZS OKTA TWLO U ROKU TTD "
          "DKNG CVNA UPST AFRM MARA RIOT CLSK IREN CORZ WULF APLD CIFR BTBT CRCL BMNR SBET GLXY BLSH FIG ONDS OKLO SMR NNE LEU CCJ UUUU VST CEG "
          "TLN NRG BE PLUG FCEL").split()
EQUITY_NAMES = {"AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "NVIDIA", "TSLA": "Tesla", "AMZN": "Amazon", "GOOGL": "Alphabet", "META": "Meta Platforms",
                "NFLX": "Netflix", "AMD": "Advanced Micro Devices", "COIN": "Coinbase", "MSTR": "Strategy", "HOOD": "Robinhood", "PLTR": "Palantir",
                "SPY": "SPDR S&P 500 ETF", "QQQ": "Invesco QQQ ETF", "GLD": "SPDR Gold ETF", "CRCL": "Circle Internet", "TSM": "TSMC"}
COMMODITY = "XAU XAG XPT XPD COPP WTI BRENT NATGAS WHEAT CORN SOYB COFFEE COCOA SUGAR COTTON URAN LITH ALUM NICK ZINC".split()
COMMODITY_NAMES = {"XAU": "Gold", "XAG": "Silver", "XPT": "Platinum", "XPD": "Palladium", "COPP": "Copper", "WTI": "WTI Crude Oil", "BRENT": "Brent Crude Oil",
                   "NATGAS": "Natural Gas", "WHEAT": "Wheat", "CORN": "Corn", "SOYB": "Soybeans", "COFFEE": "Coffee", "COCOA": "Cocoa", "SUGAR": "Sugar",
                   "COTTON": "Cotton", "URAN": "Uranium", "LITH": "Lithium", "ALUM": "Aluminum", "NICK": "Nickel", "ZINC": "Zinc"}
INDEX = "SPX NDX DJI RUT VIX NKY HSI DAX FTSE CAC KOSPI STOXX TOPIX SENSEX ASX SMI IBEX TSX MIB AEX DXY".split()
INDEX_NAMES = {"SPX": "S&P 500", "NDX": "Nasdaq 100", "DJI": "Dow Jones Industrial", "RUT": "Russell 2000", "VIX": "CBOE Volatility Index", "NKY": "Nikkei 225",
               "HSI": "Hang Seng", "DAX": "DAX 40", "FTSE": "FTSE 100", "CAC": "CAC 40", "KOSPI": "KOSPI", "STOXX": "Euro Stoxx 50", "TOPIX": "TOPIX",
               "SENSEX": "BSE Sensex", "ASX": "ASX 200", "SMI": "Swiss Market Index", "IBEX": "IBEX 35", "TSX": "S&P/TSX 60", "MIB": "FTSE MIB", "AEX": "AEX", "DXY": "US Dollar Index"}
PREIPO = "OPENAI SPACEX ANTHROPIC STRIPE DATABRICKS BYTEDANCE SHEIN REVOLUT KRAKEN CANVA DISCORD EPIC RIPPLE CHIME KLARNA PLAID SCALE PERPLEXITY XAI CURSOR".split()
OTHER = "EURUSD USDJPY GBPUSD AUDUSD USDCHF US10Y US02Y JGB10Y".split()
OTHER_NAMES = {"EURUSD": "Euro / US Dollar", "USDJPY": "US Dollar / Japanese Yen", "GBPUSD": "British Pound / US Dollar", "AUDUSD": "Australian Dollar / US Dollar",
               "USDCHF": "US Dollar / Swiss Franc", "US10Y": "US 10Y Treasury Yield", "US02Y": "US 2Y Treasury Yield", "JGB10Y": "Japan 10Y Government Bond Yield"}
BIG_OI = {"BTC": 7.3e6, "ETH": 4.1e6, "SOL": 2.2e6, "HYPE": 1.5e6, "XRP": 0.9e6, "DOGE": 0.7e6, "NVDA": 1.1e6, "TSLA": 0.95e6, "AAPL": 0.5e6,
          "XAU": 1.3e6, "XAG": 0.6e6, "SPX": 0.4e6, "OPENAI": 0.3e6, "MSTR": 0.45e6, "COIN": 0.35e6, "LINK": 0.3e6, "SUI": 0.28e6, "PEPE": 0.25e6, "WIF": 0.2e6, "TAO": 0.22e6}
ALPHABET = "ABCDEFGHIJKLMNOPQRSTVWXZ"      # 合成ティッカー用(U・Y を含めない)
SUFFIX = ["Protocol", "Network", "Chain", "Finance", "Labs", "DAO", "Swap", "AI", "Coin", "Token"]


def gen_synthetic(rng, n, used):
    out = []
    while len(out) < n:
        t = "".join(rng.choice(ALPHABET) for _ in range(rng.choice([3, 3, 4, 4, 5])))
        if t in used:
            continue
        used.add(t)
        out.append(t)
    return out


def market_list(rng):
    """[(ticker, name, category)]。約 560 銘柄"""
    used = set(CRYPTO + EQUITY + COMMODITY + INDEX + PREIPO + OTHER)
    rows = []
    for t in CRYPTO:
        rows.append((t, CRYPTO_NAMES.get(t, t.title() if t.isalpha() else t), "crypto"))
    for t in gen_synthetic(rng, 240, used):
        rows.append((t, f"{t.title()} {rng.choice(SUFFIX)}", "crypto"))
    for t in EQUITY:
        rows.append((t, EQUITY_NAMES.get(t, f"{t.title()} Inc."), "equity"))
    for t in COMMODITY:
        rows.append((t, COMMODITY_NAMES[t], "commodity"))
    for t in INDEX:
        rows.append((t, INDEX_NAMES[t], "index"))
    for t in PREIPO:
        rows.append((t, f"{t.title()} (pre-IPO)", "preipo"))
    for t in OTHER:
        rows.append((t, OTHER_NAMES[t], "other"))
    return rows


# ───────── 支払いの列 ─────────
def gen_payments(rng, interval, start_ts, end_ts, sigma, mu, status, gaps=False, stop_before=None):
    """[[pay_ts, rate|None, "c"|"e"|"m"]]。pay_ts は interval の刻み。金利成分 + AR(1) のプレミアム。
    gaps=True なら欠測の連なりと推定を混ぜる。stop_before より後は作らない(上場廃止の例)。"""
    interest = INTEREST_H * interval / 3600
    t = (start_ts // interval + 1) * interval
    prem, out, miss_left = 0.0, [], 0
    while t <= end_ts and (stop_before is None or t < stop_before):
        prem = 0.92 * prem + rng.gauss(0, sigma)
        if rng.random() < 0.01:
            mu = rng.gauss(0, sigma * 3)
        rate = round(interest + mu + prem, 8)
        st = status
        if gaps:
            if miss_left == 0 and rng.random() < 0.02:
                miss_left = rng.randint(2, 5)
            if miss_left > 0:
                miss_left -= 1
                out.append([t, None, "m"])
                t += interval
                continue
            if rng.random() < 0.12:
                st = "e"
        out.append([t, rate, st])
        t += interval
    return out


def cum_from_payments(pays, now_ts):
    """DESIGN.md 3 章: 期間の累積 = 窓の中の confirmed/estimated の合計、年率 = 累積 ÷ 実日数 × 365。期間分ないなら partial。"""
    first = pays[0][0]
    out = {}
    for k in ["7", "30", "90", "180", "365", "all"]:
        start = first if k == "all" else now_ts - int(k) * 86400
        paid = [p[1] for p in pays if p[0] >= start and p[1] is not None]
        if not paid:
            out[k] = None
            continue
        frm = max(start, first)
        days = (now_ts - frm) / 86400
        s = sum(paid)
        out[k] = {"sum": round(s, 8), "apr": round(s / days * 365, 6) if days > 0 else None,
                  "partial": k != "all" and first > start + 3600, "start": iso_date(frm), "days": round(days, 1)}
    return out


def iso_date(ts):
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


def iso_ts(ts):
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def synth_cum(rng, apr_base, data_days, data_start_ts, now_ts):
    out = {}
    for k in ["7", "30", "90", "180", "365", "all"]:
        days = data_days if k == "all" else min(int(k), data_days)
        if days <= 0.2:
            out[k] = None
            continue
        apr = apr_base * (1 + rng.gauss(0, 0.25 / math.sqrt(max(days, 1))))
        frm = data_start_ts if (k == "all" or data_days < int(k)) else now_ts - int(k) * 86400
        out[k] = {"sum": round(apr * days / 365, 8), "apr": round(apr, 6), "partial": k != "all" and data_days < int(k),
                  "start": iso_date(frm), "days": round(days, 1)}
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--stale-hours", type=float, default=0.0, help="最後の取得を何時間前にするか(2 を超えると警告が出る)")
    ap.add_argument("--anomaly", action="store_true", help="判定の矛盾の警告を出す")
    ap.add_argument("--details", type=int, default=20, help="詳細ファイルを作る銘柄数(OI 上位)")
    ap.add_argument("--mode", choices=["confirmed", "estimated"], default="confirmed")
    ap.add_argument("--days", type=float, default=95, help="収集開始から何日分か")
    args = ap.parse_args()
    rng = random.Random(args.seed)

    now = datetime.now(UTC).replace(microsecond=0)
    last_success = now - timedelta(hours=args.stale_hours)
    now_ts = int(last_success.timestamp())
    start_ts = now_ts - int(args.days * 86400)
    collection_start = iso_ts(start_ts)
    status = "e" if args.mode == "estimated" else "c"

    markets = market_list(rng)
    # OI の決定 → 詳細ファイルを作る銘柄(OI 上位 + 欠測の例 DOGE + 停止の例 GALA)
    oi = {}
    for t, _, cat in markets:
        if t in BIG_OI:
            oi[t] = BIG_OI[t] * rng.uniform(0.95, 1.05)
        else:
            lo, hi = {"crypto": (3.3, 5.6), "equity": (3.6, 5.7), "commodity": (4.0, 5.8), "index": (3.8, 5.6), "preipo": (3.5, 5.4), "other": (3.0, 4.8)}[cat]
            oi[t] = 10 ** rng.uniform(lo, hi)
    by_oi = sorted(oi, key=lambda t: -oi[t])
    detail_set = set(by_oi[:max(args.details, 1)]) | {"DOGE", "GALA"}

    if (OUT / "tickers").exists():
        shutil.rmtree(OUT / "tickers")
    (OUT / "tickers").mkdir(parents=True)

    rows, missing_total = [], 0
    for t, name, cat in markets:
        ticker = f"{t}-PERP"
        interval = 28800 if t in BIG_OI else rng.choices([28800, 14400, 3600], [55, 38, 7])[0]
        per_year = YEAR / interval
        # データ開始: ほとんどが収集開始、5% が 20 日前(30 日年率なし)、3% が 3 日前(7 日年率もなし)、1% が 30 分前(支払いがまだ無い)
        u = rng.random()
        data_start_ts = start_ts if u > 0.09 else (now_ts - 20 * 86400 if u > 0.04 else (now_ts - 3 * 86400 if u > 0.01 else now_ts - 1800))
        data_days = (now_ts - data_start_ts) / 86400
        next_ts = (now_ts // interval + 1) * interval
        stopped = t == "GALA"
        last_obs_ts = now_ts - 3 * 86400 if stopped else now_ts - rng.randint(0, 540)
        mark = {"crypto": 10 ** rng.uniform(-3, 5), "equity": 10 ** rng.uniform(1, 3), "commodity": 10 ** rng.uniform(0, 3.6),
                "index": 10 ** rng.uniform(3, 4.7), "preipo": 10 ** rng.uniform(1, 3), "other": 10 ** rng.uniform(-0.1, 2.2)}[cat]
        if t == "BTC":
            mark = 110234.0
        elif t == "ETH":
            mark = 3890.5
        sigma = {"crypto": 0.00012, "equity": 0.00008, "commodity": 0.00006, "index": 0.00005, "preipo": 0.0002, "other": 0.00004}[cat] * interval / 14400
        if t in ("PEPE", "WIF", "BONK", "FARTCOIN", "MOODENG", "PNUT", "TRUMP"):
            sigma *= 3
        if t in BIG_OI:
            sigma *= 0.35
        mu = rng.gauss(0, sigma * 1.5)

        if t in detail_set:
            pays = gen_payments(rng, interval, data_start_ts, now_ts, sigma, mu, status, gaps=(t == "DOGE"),
                                stop_before=(last_obs_ts if stopped else None))
            paid = [p for p in pays if p[1] is not None]
            cum = cum_from_payments(pays, now_ts) if paid else {k: None for k in ["7", "30", "90", "180", "365", "all"]}
            last = paid[-1] if paid else None
            last_rate = last[1] if last else None
            last_status = {"c": "confirmed", "e": "estimated"}[last[2]] if last else None
            last_pay_ts = last[0] if last else None
            n_pay, n_missing = len(paid), len(pays) - len(paid)
            with (OUT / "tickers" / f"{ticker}.json").open("w", encoding="utf-8") as f:
                json.dump({"ticker": ticker, "interval": interval, "payments": pays}, f, separators=(",", ":"))
        else:
            apr_base = 0.1095 + mu * per_year + rng.gauss(0, 0.08)
            cum = synth_cum(rng, apr_base, data_days, data_start_ts, now_ts)
            last_rate = round((apr_base + rng.gauss(0, 0.05)) / per_year, 8)
            last_status = "estimated" if rng.random() < 0.06 else "confirmed"
            last_pay_ts = next_ts - interval
            n_pay = int(data_days * 86400 / interval)
            n_missing = rng.choice([0, 0, 0, 0, 0, 0, 1, 2, 3, 6])
            if n_pay == 0:            # 上場(収集開始)直後でまだ支払時刻を越えていない: 最後の支払いはまだ無い
                last_rate = last_status = last_pay_ts = None
                n_missing = 0
        # 進行中の窓の値(API の funding.last がいま見せている値)
        cur_rate = round(((last_rate if last_rate is not None else INTEREST_H * interval / 3600) + rng.gauss(0, sigma)), 8)
        missing_total += n_missing
        apr7 = cum["7"]["apr"] if cum["7"] and not cum["7"]["partial"] else None
        apr30 = cum["30"]["apr"] if cum["30"] and not cum["30"]["partial"] else None

        # HL との対応
        p_map = {"crypto": 0.85, "equity": 0.5, "commodity": 0.3, "index": 0.15, "preipo": 0.0, "other": 0.1}[cat]
        hl_coin = ((("k" + t) if t in K_COINS else t) if rng.random() < p_map else None)
        if t == "BTC":
            hl_coin = "BTC"
        hl_apr7 = round((apr7 if apr7 is not None else 0.1) + rng.gauss(0, 0.06), 6) if hl_coin else None
        if hl_coin and rng.random() < 0.04:
            hl_apr7 = None            # 対応はあるが HL 側の値がない例
        diff_pp = round((apr7 - hl_apr7) * 100, 2) if (apr7 is not None and hl_apr7 is not None) else None

        long_share = rng.uniform(0.25, 0.8)
        o = oi[t]
        long_oi, short_oi = round(o * long_share), round(o * (1 - long_share))
        rows.append({
            "ticker": ticker, "name": name, "category": cat,
            "interval": interval, "next_ts": next_ts, "data_start": iso_date(data_start_ts),
            "last_rate": last_rate, "last_apr": round(last_rate * per_year, 6) if last_rate is not None else None,
            "last_status": last_status, "last_pay_ts": last_pay_ts,
            "cur_rate": cur_rate, "cur_apr": round(cur_rate * per_year, 6),
            "apr7": apr7, "apr30": apr30, "cum": cum,
            "hl_coin": hl_coin, "hl_apr7": hl_apr7, "diff_pp": diff_pp,
            "oi": float(long_oi + short_oi), "long_oi": float(long_oi), "short_oi": float(short_oi), "long_share": round(long_oi / (long_oi + short_oi), 4),
            "vol24": round(o * rng.lognormvariate(-0.7, 0.9)), "spread_bps": round(rng.lognormvariate(1.6, 0.8), 2),
            "mark": float(f"{mark:.7g}"), "n_pay": n_pay, "n_missing": n_missing, "last_obs": iso_ts(last_obs_ts),
        })

    # 矛盾の計数: 検査 = 0 でも金利成分でもない銘柄を 25 分以上観測できた窓、矛盾 = その中で判定と食い違った窓
    n_checked = rng.randint(30, 60)
    n_anom = (n_checked // 2 + 5 if args.mode == "estimated" else 25) if args.anomaly else rng.randint(0, 3)
    summary = {
        "generated": iso_ts(now_ts + 7), "last_success": iso_ts(now_ts), "collection_start": collection_start,
        "funding_mode": args.mode,
        "anomalies_today": n_anom, "anomalies_checked_today": n_checked, "anomaly_warn": bool(args.anomaly), "anomaly_warn_ratio": 0.5, "anomaly_warn_per_day": 20,
        "missing_total": missing_total, "stale_hours": round(args.stale_hours, 1), "n_tickers": len(rows),
        "hl_fetched": now.strftime("%Y-%m-%dT00:30:00Z"), "interest_apr": 0.1095,
        "rows": rows,
    }
    with (OUT / "summary.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, separators=(",", ":"))

    # ───────── swap ─────────
    swaps_def = [("COPP", "Swap on Copper Spot", 6.566), ("XAU", "Swap on Gold Spot", 4100.5), ("XAG", "Swap on Silver Spot", 48.2),
                 ("WTI", "Swap on WTI Crude Oil", 61.3), ("BRENT", "Swap on Brent Crude Oil", 65.1), ("NATGAS", "Swap on Natural Gas", 3.12),
                 ("SPX", "Swap on S&P 500", 6791.7), ("NDX", "Swap on Nasdaq 100", 25356.0), ("DJI", "Swap on Dow Jones Industrial", 47207.1),
                 ("AAPL", "Swap on Apple", 262.8), ("TSLA", "Swap on Tesla", 433.7), ("NVDA", "Swap on NVIDIA", 186.3),
                 ("EURUSD", "Swap on EUR/USD", 1.1624), ("USDJPY", "Swap on USD/JPY", 152.84), ("GBPUSD", "Swap on GBP/USD", 1.3315),
                 ("DAX", "Swap on DAX 40", 24239.9), ("NKY", "Swap on Nikkei 225", 49299.6), ("WHEAT", "Swap on Wheat", 5.27)]
    next_close = (now.replace(hour=21, minute=0, second=0) + (timedelta(days=1) if now.hour >= 21 else timedelta(0)))
    srows = []
    for i, (t, name, mark) in enumerate(swaps_def):
        il, is_ = rng.uniform(0.0002, 0.0009), rng.uniform(0.0002, 0.0009)
        if i % 6 == 0:
            il, is_ = il * 3, is_ * 3        # 週末分をまとめて請求した回(約 3 倍)
        has_next = i % 4 != 3
        nl, ns = rng.uniform(0.0002, 0.0009), rng.uniform(0.0002, 0.0009)
        o = 10 ** rng.uniform(4.5, 6.3)
        ls = rng.uniform(0.3, 0.7)
        srows.append({"ticker": f"{t}-SWAP", "name": name, "mark": mark,
                      "long_oi": round(o * ls), "short_oi": round(o * (1 - ls)), "vol24": round(o * rng.uniform(0, 0.4)) if rng.random() < 0.7 else 0,
                      "interval": 86400, "next_ts": int(next_close.timestamp()) if has_next else None,
                      "last": {"ann_long": round(il * 365, 6), "ann_short": round(is_ * 365, 6), "int_long": round(il, 8), "int_short": round(is_, 8)},
                      "next": ({"ann_long": round(nl * 365, 6), "ann_short": round(ns * 365, 6), "int_long": round(nl, 8), "int_short": round(ns, 8)} if has_next else None),
                      "snap_ts": iso_ts(now_ts - rng.randint(0, 600))})
    with (OUT / "swaps.json").open("w", encoding="utf-8") as f:
        json.dump({"generated": iso_ts(now_ts + 7), "rows": srows}, f, ensure_ascii=False, separators=(",", ":"))

    n_detail = len(list((OUT / "tickers").glob("*.json")))
    print(f"wrote {OUT / 'summary.json'} ({len(rows)} rows, {(OUT / 'summary.json').stat().st_size:,} bytes), "
          f"{n_detail} ticker files, swaps.json ({len(srows)} rows); last_success={summary['last_success']} mode={args.mode}")


if __name__ == "__main__":
    main()
