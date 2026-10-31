#!/usr/bin/env python3
"""開発用のモックデータを site/data/ に生成する(DESIGN.md 5 章の契約。vfm build の出力と同じ形)。

  python3 scripts/make_mock_site_data.py                   # 通常
  python3 scripts/make_mock_site_data.py --stale-hours 3   # 最後の取得を 3 時間前にして「取得停止」の警告を試す
  python3 scripts/make_mock_site_data.py --anomaly         # 判定の矛盾の警告を試す
  python3 scripts/make_mock_site_data.py --details 60      # 詳細ファイルを OI 上位 60 銘柄分作る(既定 20)
  python3 scripts/make_mock_site_data.py --mode estimated  # 見込み値型(支払いが全部「推定」)
  python3 scripts/make_mock_site_data.py --no-monthly      # monthly.json を作らない(ヒートマップが銘柄別ファイルから計算する経路を試す)

生成物: summary.json(約 560 行)、tickers/<T>.json(OI 上位 N 銘柄 + 例の銘柄、約 3 か月分)、monthly.json、swaps.json(18 行)。
- 詳細ファイルのある銘柄の cum(期間の累積・年率・※)と月ごとの累積は src/vfm/aggregate.py(build と同じ関数)で計算する。
  ページの指標カード(site/lib.js の cumStat)と一覧の列が一致することを確かめられる。
- 例の銘柄: DOGE = 欠測と推定が混ざる、SOL = 40 日前に支払間隔が 8h→4h(古い支払いに 4 つ目の間隔が付く)、
  XRP と LTC = いちばん新しい支払いが欠測(last_status = missing)、GALA = 3 日前に停止(上場廃止の例)、
  約 1% = 収集開始直後でまだ支払いが無い(cur_rate だけ)。
- 実データに戻すには: PYTHONPATH=src python3 -m vfm build --out site
"""
import argparse
import json
import math
import random
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "site" / "data"
sys.path.insert(0, str(ROOT / "src"))
from vfm.aggregate import cum_stat, monthly_sums  # noqa: E402

UTC = timezone.utc
INTEREST_H = 0.0000125          # 金利成分 0.00125% / 時(8 時間で 0.01%、年 10.95%)
YEAR = 365 * 86400
PRESETS = {"7": 7, "30": 30, "90": 90, "180": 180, "365": 365, "all": None}
WORD = {"c": "confirmed", "e": "estimated", "m": "missing"}
CHANGE_IV = "SOL"               # 支払間隔の変更(8h→4h)の例
MISSING_LAST = {"XRP", "LTC"}   # いちばん新しい支払いが欠測の例

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
def gen_payments(rng, interval, start_ts, end_ts, sigma, mu, status, gaps=False, stop_before=None, file_iv=None):
    """[[pay_ts, rate|None, "c"|"e"|"m"(, 間隔)]]。pay_ts は interval の刻み。金利成分 + AR(1) のプレミアム。
    gaps=True なら欠測の連なりと推定を混ぜる。stop_before より後は作らない(上場廃止の例)。
    file_iv(ファイルの interval)と interval が違えば、build と同じく 4 つ目にその支払いの間隔を付ける。"""
    interest = INTEREST_H * interval / 3600
    t = (start_ts // interval + 1) * interval
    prem, out, miss_left = 0.0, [], 0
    extra = [interval] if (file_iv and file_iv != interval) else []
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
                out.append([t, None, "m"] + extra)
                t += interval
                continue
            if rng.random() < 0.12:
                st = "e"
        out.append([t, rate, st] + extra)
        t += interval
    return out


def _r(v, sig=6):
    """build と同じく float を有効数字 sig 桁に丸める(dict / list は再帰)。"""
    if isinstance(v, float):
        if v == 0 or v != v:
            return v
        return float(f"{v:.{sig}g}")
    if isinstance(v, dict):
        return {k: _r(x, sig) for k, x in v.items()}
    if isinstance(v, list):
        return [_r(x, sig) for x in v]
    return v


def compact_cum(c):
    """build と同じ 6 要素の配列 [sum, apr, partial(0/1), start, days(観測日数、小数 2 桁), n_missing]。"""
    return None if c is None else [c["sum"], c["apr"], 1 if c["partial"] else 0, c["start"], round(c["days"], 2), c["n_missing"]]


def iso_date(ts):
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


def iso_ts(ts):
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def synth_cum(rng, apr_base, data_start_ts, now_ts, interval, n_missing):
    """詳細ファイルを作らない銘柄の cum(cum_stat と同じ形)。欠測は期間の長さに比例して割り振り、観測日数から除く。"""
    data_days = (now_ts - data_start_ts) / 86400
    first = (data_start_ts // interval + 1) * interval
    out = {}
    for k, d in PRESETS.items():
        win = data_days if d is None else min(d, data_days)
        n_win = int(win * 86400 // interval)
        miss = min(round(n_missing * win / data_days) if data_days > 0 else 0, max(n_win - 1, 0))
        n = n_win - miss
        if n <= 0:
            out[k] = None
            continue
        covered = n * interval / 86400
        apr = apr_base * (1 + rng.gauss(0, 0.25 / math.sqrt(max(covered, 1))))
        out[k] = {"sum": apr * covered / 365, "apr": apr, "partial": d is not None and first > now_ts - d * 86400 + interval,
                  "start": iso_date(first), "days": covered, "n": n, "n_missing": miss}
    return out


def synth_monthly(rng, apr_base, data_start_ts, now_ts):
    """詳細ファイルを作らない銘柄の月ごとの累積(小数)。"""
    out, t = {}, data_start_ts
    while t < now_ts:
        d = datetime.fromtimestamp(t, UTC)
        nxt = int(datetime(d.year + (d.month == 12), d.month % 12 + 1, 1, tzinfo=UTC).timestamp())
        out[d.strftime("%Y-%m")] = (apr_base + rng.gauss(0, 0.05)) * (min(nxt, now_ts) - t) / 86400 / 365
        t = nxt
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--stale-hours", type=float, default=0.0, help="最後の取得を何時間前にするか(2 を超えると警告が出る)")
    ap.add_argument("--anomaly", action="store_true", help="判定の矛盾の警告を出す")
    ap.add_argument("--details", type=int, default=20, help="詳細ファイルを作る銘柄数(OI 上位)")
    ap.add_argument("--mode", choices=["confirmed", "estimated"], default="confirmed")
    ap.add_argument("--days", type=float, default=95, help="収集開始から何日分か")
    ap.add_argument("--no-monthly", action="store_true", help="monthly.json を作らない")
    args = ap.parse_args()
    rng = random.Random(args.seed)

    now = datetime.now(UTC).replace(microsecond=0)
    last_success = now - timedelta(hours=args.stale_hours)
    now_ts = int(last_success.timestamp())        # 最後の取得(支払いはここまで)
    gen_ts = now_ts + 7                           # build の時刻 = summary.generated。cum はこの時刻で計算する(build と同じ)
    start_ts = now_ts - int(args.days * 86400)
    status = "e" if args.mode == "estimated" else "c"

    markets = market_list(rng)
    # OI の決定 → 詳細ファイルを作る銘柄(OI 上位 + 例の銘柄)
    oi = {}
    for t, _, cat in markets:
        if t in BIG_OI:
            oi[t] = BIG_OI[t] * rng.uniform(0.95, 1.05)
        else:
            lo, hi = {"crypto": (3.3, 5.6), "equity": (3.6, 5.7), "commodity": (4.0, 5.8), "index": (3.8, 5.6), "preipo": (3.5, 5.4), "other": (3.0, 4.8)}[cat]
            oi[t] = 10 ** rng.uniform(lo, hi)
    by_oi = sorted(oi, key=lambda t: -oi[t])
    detail_set = set(by_oi[:max(args.details, 1)]) | {"DOGE", "GALA", CHANGE_IV, "XRP"}

    if (OUT / "tickers").exists():
        shutil.rmtree(OUT / "tickers")
    (OUT / "tickers").mkdir(parents=True)

    rows, missing_total, monthly_all = [], 0, {}
    for t, name, cat in markets:
        ticker = f"{t}-PERP"
        interval = 28800 if t in BIG_OI else rng.choices([28800, 14400, 3600], [55, 38, 7])[0]
        if t == CHANGE_IV:
            interval = 14400                        # 今の間隔(40 日前までは 8h)
        per_year = YEAR / interval
        # データ開始: ほとんどが収集開始、5% が 20 日前(30 日年率なし)、3% が 3 日前(7 日年率もなし)、1% が 30 分前(支払いがまだ無い)
        u = rng.random()
        data_start_ts = start_ts if u > 0.09 else (now_ts - 20 * 86400 if u > 0.04 else (now_ts - 3 * 86400 if u > 0.01 else now_ts - 1800))
        if t in detail_set or t in MISSING_LAST:
            data_start_ts = start_ts
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
            if t == CHANGE_IV:
                # 40 日前に 8h→4h。窓の途中で間隔が変わったので古い窓は支払いなしで閉じ、次の 4h 刻みから新しい窓を追う
                old = gen_payments(rng, 28800, data_start_ts, now_ts - 40 * 86400, sigma * 2, mu * 2, status, file_iv=interval)
                pays = old + gen_payments(rng, interval, old[-1][0] + interval, now_ts, sigma, mu, status, file_iv=interval)
            else:
                pays = gen_payments(rng, interval, data_start_ts, now_ts, sigma, mu, status, gaps=(t == "DOGE"),
                                    stop_before=(last_obs_ts if stopped else None))
            if t in MISSING_LAST and pays:
                pays[-1] = [pays[-1][0], None, "m"] + pays[-1][3:]
            valid = [p for p in pays if p[1] is not None]
            cum = {k: cum_stat(pays, gen_ts, d, interval) for k, d in PRESETS.items()}
            latest = pays[-1] if pays else None
            last_pay = latest if (latest and latest[1] is not None) else None
            last_rate = last_pay[1] if last_pay else None
            last_apr = last_pay[1] * YEAR / (last_pay[3] if len(last_pay) > 3 else interval) if last_pay else None
            last_status = WORD[latest[2]] if latest else None
            last_pay_ts = latest[0] if latest else None
            n_pay, n_missing = len(valid), len(pays) - len(valid)
            monthly = monthly_sums(pays)
            with (OUT / "tickers" / f"{ticker}.json").open("w", encoding="utf-8") as f:
                json.dump({"ticker": ticker, "interval": interval, "payments": pays}, f, separators=(",", ":"))
        else:
            apr_base = 0.1095 + mu * per_year + rng.gauss(0, 0.08)
            n_pay = int((now_ts - data_start_ts) // interval)
            n_missing = rng.choice([0, 0, 0, 0, 0, 0, 1, 2, 3, 6]) if n_pay > 3 else 0
            last_rate = round((apr_base + rng.gauss(0, 0.05)) / per_year, 8)
            last_status = "estimated" if (rng.random() < 0.06 or status == "e") else "confirmed"
            last_pay_ts = next_ts - interval
            if t in MISSING_LAST:              # いちばん新しい支払いが欠測
                last_rate, last_status, n_missing = None, "missing", max(n_missing, 1)
            if n_pay == 0:                     # 収集開始直後でまだ支払時刻を越えていない: 最後の支払いはまだ無い
                last_rate = last_status = last_pay_ts = None
            last_apr = last_rate * per_year if last_rate is not None else None
            cum = synth_cum(rng, apr_base, data_start_ts, gen_ts, interval, n_missing)
            monthly = synth_monthly(rng, apr_base, data_start_ts, now_ts) if n_pay else {}
        # 進行中の窓の値(API の funding.last がいま見せている値)
        cur_rate = round(((last_rate if last_rate is not None else INTEREST_H * interval / 3600) + rng.gauss(0, sigma)), 8)
        missing_total += n_missing
        apr7 = cum["7"]["apr"] if cum["7"] and not cum["7"]["partial"] else None
        apr30 = cum["30"]["apr"] if cum["30"] and not cum["30"]["partial"] else None
        if monthly:
            monthly_all[ticker] = monthly

        # HL との対応
        p_map = {"crypto": 0.85, "equity": 0.5, "commodity": 0.3, "index": 0.15, "preipo": 0.0, "other": 0.1}[cat]
        hl_coin = ((("k" + t) if t in K_COINS else t) if rng.random() < p_map else None)
        if t == "BTC":
            hl_coin = "BTC"
        hl_apr7 = (apr7 if apr7 is not None else 0.1) + rng.gauss(0, 0.06) if hl_coin else None
        if hl_coin and rng.random() < 0.04:
            hl_apr7 = None            # 対応はあるが HL 側の値がない例
        diff_pp = (apr7 - hl_apr7) * 100 if (apr7 is not None and hl_apr7 is not None) else None

        long_share = rng.uniform(0.25, 0.8)
        o = oi[t]
        long_oi, short_oi = float(round(o * long_share)), float(round(o * (1 - long_share)))
        rows.append(_r({
            "ticker": ticker, "name": name, "category": cat,
            "interval": interval, "next_ts": next_ts, "data_start": iso_date(data_start_ts),
            "cur_rate": cur_rate, "cur_apr": cur_rate * per_year,
            "last_rate": last_rate, "last_apr": last_apr, "last_status": last_status, "last_pay_ts": last_pay_ts,
            "apr7": apr7, "apr30": apr30, "cum": {k: compact_cum(c) for k, c in cum.items()},
            "hl_coin": hl_coin, "hl_apr7": hl_apr7, "diff_pp": diff_pp,
            "oi": long_oi + short_oi, "long_oi": long_oi, "short_oi": short_oi, "long_share": long_oi / (long_oi + short_oi),
            "vol24": float(round(o * rng.lognormvariate(-0.7, 0.9))), "spread_bps": rng.lognormvariate(1.6, 0.8),
            "mark": mark, "n_pay": n_pay, "n_missing": n_missing, "last_obs": iso_ts(last_obs_ts), "listed": not stopped,
        }))
    rows.sort(key=lambda r: -(r["oi"] or 0))

    # 矛盾の計数: 検査 = 0 でも金利成分でもない銘柄を 25 分以上観測できた窓、矛盾 = その中で判定と食い違った窓
    n_checked = rng.randint(30, 60)
    n_anom = (n_checked // 2 + 5 if args.mode == "estimated" else 25) if args.anomaly else rng.randint(0, 3)
    summary = {
        "generated": iso_ts(gen_ts), "last_success": iso_ts(now_ts), "collection_start": iso_ts(start_ts),
        "funding_mode": args.mode,
        "anomalies_today": n_anom, "anomalies_checked_today": n_checked, "anomaly_warn": bool(args.anomaly),
        "anomaly_warn_per_day": 20, "anomaly_warn_ratio": 0.5,
        "missing_total": missing_total, "stale_hours": round(args.stale_hours, 2), "hl_fetched": now.strftime("%Y-%m-%dT00:30:00Z"),
        "interest_apr": 0.1095, "n_tickers": len(rows), "rows": rows,
    }
    with (OUT / "summary.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, separators=(",", ":"))

    # 月ごとの累積(ヒートマップ用。build と同じ形: 直近 18 か月)
    month_keys = sorted({k for m in monthly_all.values() for k in m})[-18:]
    if args.no_monthly:
        (OUT / "monthly.json").unlink(missing_ok=True)
    else:
        mrows = {t: _r({k: v for k, v in m.items() if k in month_keys}) for t, m in monthly_all.items()}
        with (OUT / "monthly.json").open("w", encoding="utf-8") as f:
            json.dump({"generated": iso_ts(gen_ts), "months": month_keys, "rows": {t: m for t, m in mrows.items() if m}}, f, separators=(",", ":"))

    # ───────── swap ─────────
    swaps_def = [("COPP", "Swap on Copper Spot", 6.566), ("XAU", "Swap on Gold Spot", 4100.5), ("XAG", "Swap on Silver Spot", 48.2),
                 ("WTI", "Swap on WTI Crude Oil", 61.3), ("BRENT", "Swap on Brent Crude Oil", 65.1), ("NATGAS", "Swap on Natural Gas", 3.12),
                 ("SPX", "Swap on S&P 500", 6791.7), ("NDX", "Swap on Nasdaq 100", 25356.0), ("DJI", "Swap on Dow Jones Industrial", 47207.1),
                 ("AAPL", "Swap on Apple", 262.8), ("TSLA", "Swap on Tesla", 433.7), ("NVDA", "Swap on NVIDIA", 186.3),
                 ("EURUSD", "Swap on EUR/USD", 1.1624), ("USDJPY", "Swap on USD/JPY", 152.84), ("GBPUSD", "Swap on GBP/USD", 1.3315),
                 ("DAX", "Swap on DAX 40", 24239.9), ("NKY", "Swap on Nikkei 225", 49299.6), ("WHEAT", "Swap on Wheat", 5.27)]
    next_close = (now.replace(hour=21, minute=0, second=0) + (timedelta(days=1) if now.hour >= 21 else timedelta(0)))
    snap_ts = iso_ts(now_ts - rng.randint(0, 600))
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
                      "snap_ts": snap_ts})
    with (OUT / "swaps.json").open("w", encoding="utf-8") as f:
        json.dump({"generated": iso_ts(gen_ts), "snap_ts": snap_ts, "rows": srows}, f, ensure_ascii=False, separators=(",", ":"))

    n_detail = len(list((OUT / "tickers").glob("*.json")))
    print(f"wrote {OUT / 'summary.json'} ({len(rows)} rows, {(OUT / 'summary.json').stat().st_size:,} bytes), {n_detail} ticker files, "
          f"{'no monthly.json' if args.no_monthly else 'monthly.json'}, swaps.json ({len(srows)} rows); "
          f"generated={summary['generated']} last_success={summary['last_success']} mode={args.mode}")


if __name__ == "__main__":
    main()
