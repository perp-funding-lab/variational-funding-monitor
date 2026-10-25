# 設計メモ(Variational Funding Rate Monitor)

時刻はすべて UTC。数値は文字列で受け取り、保存時に精度を落とす箇所は明記する。

## 1. データの流れ

```
Cloudflare Worker (cron 8,18,…,58 分) ──workflow_dispatch──▶ GitHub Actions collect.yml
GitHub Actions schedule (保険、30 分ごと) ───────────────────▶ 同上
   collect.yml: python -m vfm collect  → data/ を更新 → commit/push(再試行あり)
                python -m vfm build    → site/data/*.json を生成(コミットしない)
                actions/deploy-pages   → GitHub Pages
hl.yml (1 日 1 回): python -m vfm hl-update → data/hl/hl_7d.json を更新 → commit/push
```

## 2. 保存ファイル(すべて UTF-8、CSV はヘッダ付き、追記のみ)

| パス | 1 行 | 分割 | 保持 |
|---|---|---|---|
| `data/runs/YYYY-MM.csv` | 実行ログ: `scheduled_ts,fetch_ts,delay_s,http_status,n_listings(= perp + swap),n_perp,n_swap,n_obs_rows,n_funding_rows,n_anomalies,trigger,note` | 月 | 無期限 |
| `data/obs/YYYY-MM-DD.csv` | 観測ログ: `obs_ts,ticker,interval_seconds,next_timestamp,interval_rate,annualized_rate,kind` (`kind` = `F` 窓で最初 / `C` 値が変わった / `L` 窓で最後) | 日 | 30 日(古いファイルは削除。git 履歴には残る) |
| `data/funding/YYYY-MM.csv` | 確定 FR: `pay_ts,ticker,interval_seconds,interval_rate,annualized_rate,status,basis_obs_ts` (`status` = `confirmed` / `estimated` / `missing`。`missing` は rate 空欄) | 月(pay_ts 基準) | 無期限 |
| `data/snapshots/YYYY-MM-DD.csv` | 毎時スナップショット: `snap_ts,ticker,contract_type,mark_price,long_oi,short_oi,volume_24h,base_spread_bps`(各 UTC 時の最初の成功回。価格は有効数字 7 桁、OI/出来高は整数ドル、スプレッドは小数 2 桁) | 日 | 無期限(月が終わると `data/archive/snapshots/YYYY-MM.csv.gz` に移す) |
| `data/swaps_latest.json` | swap の最新値(全件) | - | 最新のみ |
| `data/meta.json` | `collection_start`, `last_success_ts`, `last_run_ts`, `funding_mode`, `anomalies_by_day`(直近 7 日。`{日: {n: 矛盾, checked: 検査}}`) | - | 最新のみ |
| `data/names.json` | ticker → 名前(新規上場のときだけ変わる。取得に失敗した回の build 用) | - | 最新のみ |
| `data/hl/hl_7d.json` | HL 7 日年率: `{fetched, coins: {"BTC": {"apr7d": 0.11, "n": 168, "dex": "main"}}}` | - | 最新のみ |
| `config/settings.json` | `funding_mode` (`confirmed`/`estimated`)、判定の根拠、閾値 | - | 手で編集 |
| `config/category_overrides.csv` | `ticker,category,reason` | - | 手で編集 |
| `config/hl_mapping.csv` | `ticker,hl_coin,note`(空の `hl_coin` は「対応なし」を明示) | - | 手で編集 |

### 2.1 観測ログの間引き
- 各 run で全 perp を見て、直前の観測(直近の obs ファイルから復元)と `(interval_seconds, next_timestamp, interval_rate)` を比べる。
- 窓が変わった(`next_timestamp` が進んだ)→ 旧窓の最後の観測を `last` として(直前 run の fetch_ts で)書き、新窓の値を `first` として書く。
- 同じ窓で値が変わった → `change`。
- 同じ窓で同じ値 → 書かない。
- 窓の中に観測が 1 回しかなければ `first` だけ(`last` は書かない)。

### 2.2 確定 FR の作り方(funding_mode)
- `confirmed`(確定値型): `next_timestamp = T` の窓で最初に観測した `last.interval_rate` を、`pay_ts = T − interval_seconds` の FR として `confirmed` で記録する(`basis_obs_ts` はその観測時刻)。
- `estimated`(見込み値型): 窓 T が終わった(`next_timestamp` が進んだ)とき、T の直前の最後の観測値を `pay_ts = T` の FR として `estimated` で記録する。
- 欠測: 新しく記録する `pay_ts` と、その銘柄の直前の記録との間に `interval_seconds` の刻みで支払時刻があれば、その分を `missing`(rate 空欄)で埋める。0 にはしない。
- 間隔の変更: 刻みは「新しい窓の interval_seconds」を T から逆算して使う。
- 新規上場: 直前の記録が無い銘柄は欠測を作らない。上場廃止: 単に新しい行が増えない(ページでは最終観測が 2 日以上前の銘柄に印を付ける)。
- 矛盾の計数(`n_anomalies` / `n_checked`): estimated モードでは「0 でも金利成分でもない銘柄を 25 分以上観測できた窓」を検査(checked)とし、その中で一度も値が動かなかった窓を矛盾(n)とする。1 日の n / checked が `anomaly_warn_ratio`(0.5)を超えたら(checked ≥ 20 のとき)ページに警告。confirmed モードでは同じ窓の中で値が変わった件数を矛盾とし、1 日 `anomaly_warn_per_day`(20)件を超えたら警告。
- `python -m vfm rebuild-funding --mode confirmed|estimated --out <path>` で観測ログからもう一方の方式の確定 FR を作り直せる。

## 3. 年率換算
- 1 回分の FR `r`、間隔 `s` 秒 → 年率 = `r × (365 × 86400 / s)`。4 時間なら ×2190、8 時間なら ×1095、1 時間なら ×8760。API の `annualized_rate` もこの式(実測で一致)。
- 期間の累積 FR = 期間内の `confirmed`/`estimated` の `interval_rate` の合計(`missing` は加えない)。年率 = 累積 ÷ 実日数 × 365。
- データが期間分ない銘柄: 開始日からの累積を実日数で年率換算し `partial = true`(ページでは ※)。
- 7 日年率 / 30 日年率の列: データが 7 日 / 30 日分揃うまで `null`(ページでは「—」)。
- HL 7 日年率 = 直近 7 日の毎時 FR の合計 ÷ 7 × 365。

## 4. 種類(category)
`crypto` 暗号資産 / `equity` 株式・ETF / `commodity` コモディティ / `index` 指数 / `preipo` プレ IPO / `other` その他。判定順は上書き CSV → 名前のキーワード → HL 本体にある → HIP-3 株式にある → crypto。

## 5. サイトのデータ契約(`site/data/`、build が毎回生成)

### `summary.json`
```json
{
  "generated": "2026-10-25T00:08:12Z",
  "last_success": "2026-10-25T00:08:05Z",
  "collection_start": "2026-10-25T00:08:05Z",
  "funding_mode": "confirmed",
  "anomalies_today": 0, "anomalies_checked_today": 0, "anomaly_warn": false, "anomaly_warn_per_day": 20, "anomaly_warn_ratio": 0.5,
  "missing_total": 0,
  "stale_hours": 0.1,
  "hl_fetched": "2026-10-25T00:30:00Z",
  "interest_apr": 0.1095,
  "rows": [
    {
      "ticker": "BTC-PERP", "name": "Bitcoin", "category": "crypto",
      "interval": 28800, "next_ts": 1792886400, "data_start": "2026-10-25",
      "cur_rate": 0.0001, "cur_apr": 0.1095,
      "last_rate": 0.0001, "last_apr": 0.1095, "last_status": "estimated", "last_pay_ts": 1792857600,
      "apr7": null, "apr30": null,
      "cum": {"7": {"sum": 0.0003, "apr": 0.1095, "partial": true, "start": "2026-10-25", "days": 1.0},
              "30": {...}, "90": {...}, "180": {...}, "365": {...}, "all": {...}},
      "hl_coin": "BTC", "hl_apr7": 0.1012, "diff_pp": 0.83,
      "oi": 7299715.0, "long_oi": 5250294.0, "short_oi": 2049421.0, "long_share": 0.72,
      "vol24": 2151862.0, "spread_bps": 5.63, "mark": 110234.0,
      "n_pay": 3, "n_missing": 0, "last_obs": "2026-10-25T00:08:05Z"
    }
  ]
}
```
- 数値はすべて有効数字 6 桁に丸める(OI や出来高のドル額も。ファイルサイズのため)。`rate`/`apr` は小数(0.0001 = 0.01%)。`diff_pp` は 7 日年率の差(Variational − HL)を %ポイントで。`cur_rate`/`cur_apr` は API がいま表示している進行中の窓の値、`last_*` は最後に記録した支払(収集開始後の最初の支払まで null)。
- `cum[k]` は `null` の場合がある(データなし)。`apr7`/`apr30` はデータ日数不足なら `null`。

### `tickers/<TICKER>.json`(行クリック時に読む)
```json
{"ticker": "BTC-PERP", "interval": 28800,
 "payments": [[1792857600, 0.0001, "c"], [1792886400, null, "m"], [1792915200, 0.00012, "e"]]}
```
`payments` は pay_ts 昇順。status は `c` confirmed / `e` estimated / `m` missing。

### `swaps.json`
```json
{"generated": "...", "rows": [{"ticker": "COPP-SWAP", "name": "Swap on Copper Spot", "mark": 6.566,
  "long_oi": 922408, "short_oi": 1157564, "vol24": 0, "interval": 86400, "next_ts": null,
  "last": {"ann_long": 0.1955, "ann_short": 0.1955, "int_long": 0.0005431, "int_short": 0.0005431},
  "next": null, "snap_ts": "..."}]}
```

## 6. リポサイズの見積もり(1 年)
`scripts/estimate_repo_size.py` が実データの 1 run 分の増分から外挿する。目標 1 GB 未満(git 履歴込み)。
