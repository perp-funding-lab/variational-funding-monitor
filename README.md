# Variational Funding Rate Monitor

Variational Omni の無期限(perp)全銘柄のファンディングレート(FR)を 10 分ごとに自前で記録し続け、
銘柄別の一覧・チャート・Hyperliquid との比較を静的ページとして公開する。Variational の API には履歴が無いので、
データは収集開始時点(2026-10-24 23:19 UTC)から貯まる。

- 公開ページ: https://__ORG__.github.io/variational-funding-monitor/
- データ元: `GET https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/v2/stats`(キー不要、全銘柄が 1 回で返る)
- 仕様: [API](https://docs.variational.io/technical-documentation/api) / [Funding Rates](https://docs.variational.io/omni/trading/funding-rates)

## 構成

```
Cloudflare Worker (cron 8,18,28,38,48,58 分) ─workflow_dispatch→ GitHub Actions collect.yml
GitHub Actions schedule(保険: 13,43 分)        ────────────────→   同上
  collect.yml : python -m vfm collect → data/ を更新 → commit/push(競合時は rebase して再試行)
                python -m vfm build   → site/data/*.json を生成(コミットしない)→ GitHub Pages にデプロイ
  hl.yml      : 1 日 1 回(00:40 UTC)Hyperliquid の 7 日年率と対応表を更新
  archive.yml : 月初に先月の日次スナップショットを .csv.gz にまとめる
```

- 収集・集計は Python(標準ライブラリのみ)、ページは素の JS(Plotly は CDN)。外部サービスは Variational・Hyperliquid・GitHub・Cloudflare のみ。
- データを書く 3 つのワークフローは同じ concurrency group で直列に動く。push が競合したら `git rebase` して最大 4 回やり直す。
- 収集は毎回 stats を 1 回取得する(タイムアウト 20 秒、429 は `retry_after_seconds` を待って最大 3 回再試行)。形が想定と違えば何も記録しない。
- 取得に失敗してもサイトは作り直す(最終取得から 2 時間を超えるとページ上部に警告)。

## funding.last の意味の判定(2026-10-24)

Variational の `funding.last` が「直前に確定した値(窓の中で動かない)」か「進行中の窓の見込み値(窓の中で動く)」かは
ドキュメントから読み取れないので、実装前に実データで判定した。

| 項目 | 値 |
|---|---|
| 取得 1 回目 | 2026-10-24 23:19:15 UTC |
| 取得 2 回目 | 2026-10-24 23:31:30 UTC(12 分 15 秒後) |
| 2 回で `next_timestamp` が同じ perp | 564 / 564(全銘柄の次回支払が 2026-10-25 00:00 UTC) |
| そのうち金利成分(0.0000125 × 間隔の時間数)と違う値の銘柄 = 比較対象 | 193(うち 0 以外 82) |
| 値が変わった銘柄(数値として比較) | **80** |
| 判定 | **見込み値型**(1 件でも変われば見込み値型。80 件が変わった) |

追加の観測: 23:31:30 → 23:49:30 の 18 分でも同じ窓の中で 80 銘柄が変わった。金利成分ちょうどの銘柄(371)と 0 の銘柄(111)は動かなかった。
ドキュメントの「8 時間窓の BTC perp は 1〜7 時間目は 0 を適用し、8 時間目に直前 8 時間のデータから計算する」という記述とも整合する
(`last` はプレミアムの時間加重平均を反映して窓の中で更新される)。

したがって確定 FR は次のように作る(`config/settings.json` の `funding_mode = "estimated"`):

- 窓 T(`next_timestamp = T`)が閉じた(`next_timestamp` が進んだ)とき、T の直前に取れた最後の値を **時刻 T の FR(推定)** として記録する。
  58 分の起動がこのためにある(支払直前の最終観測)。ページでは「推定」と分かるようにしている。
- もう一方の方式(確定値型: 窓で最初に取れた値を T − interval の確定値とする)も実装・テスト済みで、
  `python -m vfm rebuild-funding --mode confirmed --out /tmp/confirmed.csv` で観測ログ(30 日分)から作り直せる。
- 本番でも毎回、判定と矛盾する動きを数える(見込み値型なのに、0 でも金利成分でもない銘柄の値が 25 分以上一度も動かなかった件数)。
  1 日の合計が `anomaly_warn_per_day`(20)を超えるとページに警告が出る。

## 保存ファイル(`data/`)

| パス | 内容 | 分割 | 保持 |
|---|---|---|---|
| `runs/YYYY-MM.csv` | 実行ログ(予定時刻・取得時刻・遅れ・HTTP 状態・件数・矛盾の数) | 月 | 無期限 |
| `obs/YYYY-MM-DD.csv` | 観測ログ(取得時刻, ticker, interval_seconds, next_timestamp, interval_rate, annualized_rate, kind)。前回と同じ値は保存せず、窓の最初(F)・変化点(C)・窓の最後(L)だけ残す | 日 | 30 日(git 履歴には残る) |
| `funding/YYYY-MM.csv` | 確定 FR: 1 支払 1 行(支払時刻, ticker, 間隔, レート, 年率, 確定/推定/欠測, 根拠の観測時刻)。観測が無かった窓は `missing`(レート空欄)で残し 0 にしない | 月 | 無期限 |
| `snapshots/YYYY-MM-DD.csv` | 毎時スナップショット(各 UTC 時の最初の成功回: ticker, mark_price, long_oi, short_oi, volume_24h, base_spread_bps) | 日 → 月締めで `archive/snapshots/YYYY-MM.csv.gz` | 無期限 |
| `swaps_latest.json` | swap の最新値のみ | - | 最新のみ |
| `hl/hl_7d.json`, `hl/hl_universe.json` | HL の 7 日年率、HL 本体・HIP-3 の銘柄一覧 | - | 最新のみ |
| `meta.json` | 収集開始、最終成功、矛盾の件数(直近 7 日) | - | 最新のみ |

間隔の変更は「新しい窓の interval_seconds」で刻みを揃える。新規上場は欠測を作らず、上場廃止は行が増えなくなるだけ(ページでは最終観測が 2 日以上前の銘柄に印)。

### リポサイズの見積もり(1 年、git 履歴込み)

`python scripts/estimate_repo_size.py --runs 288` が、実データの 1 回分の増分を一時リポに 288 回コミットして `git gc --aggressive` 後の pack サイズから外挿する。

- 1 日の窓(支払)の数: 2,709(4 時間 304 銘柄 × 6 + 8 時間 255 銘柄 × 3 + 1 時間 5 銘柄 × 24)
- 1 回の収集で増える行: 観測ログ約 118 行(変化点 80 + 窓の最初/最後)、確定 FR 約 19 行、実行ログ 1 行、毎時スナップショット 582 行(6 回に 1 回)
- 模擬の結果: 1 回あたり約 4.8 KB(pack)→ 52,560 回/年で約 250 MB。月締めの .csv.gz 約 9 MB × 12 = 約 110 MB、HL の JSON 約 14 MB
- **合計 約 370 MB / 年 < 1 GB**(GitHub Pages の推奨上限)。収集ジョブは sparse checkout で `data/archive` を持たないので、毎回のチェックアウトは当月分だけ。

## ページ

元の「BTC Funding Rate Monitor」の Hyperliquid 銘柄別タブの UI・JS・説明文を流用し、Variational 向けに書き直した。

- 一覧(見出しで並べ替え): 銘柄、名前、種類、支払間隔、次回支払(UTC)、データ開始日、直近 FR(1 回分と年率)、7 日年率、30 日年率、期間の累積 FR / 年率(※ はデータが期間分ない銘柄)、HL 7 日年率、差(Variational − HL、%ポイント)、OI(ロング+ショート)、ロング比率、24h 出来高、スプレッド(bps)
- フィルタ: 種類、OI が○百万ドル以上、24h 出来高が○百万ドル以上、銘柄検索
- 行クリックで下にチャート(支払ごとの FR を生データ・日・週・月でまとめ、累積 FR)。月ごとの累積 FR の銘柄比較(ヒートマップ、OI 上位 60)
- swap タブ: 現在値のみ(ロング/ショート別の年率と 1 回分、次回支払、OI、出来高)
- 状態表示: 最終更新(UTC)、収集開始日、欠測の数、方式。最後の成功取得から 2 時間を超えると上部に警告
- 初回の読み込みは合計 2 MB 以内(Plotly の cartesian バンドルを含む)。銘柄別の詳細は行クリック時に `data/tickers/<TICKER>.json` を読む

年率換算 = 1 回分の FR × (365 日 ÷ 支払間隔)。4 時間なら ×2190、8 時間なら ×1095、1 時間なら ×8760(API の `annualized_rate` と一致)。
金利成分は 0.00125 % / 時(8 時間で 0.01 %、年 10.95 %)で、プレミアムが無い銘柄の FR はこの値ちょうどになる。
index 価格が公開されていないのでプレミアムは出せない。OI はトレーダー側のみ(片側)。

## 種類(category)

`crypto` 暗号資産 / `equity` 株式・ETF / `commodity` コモディティ / `index` 指数 / `preipo` プレ IPO / `other` その他。判定順(上から優先):

1. `config/category_overrides.csv`
2. 名前のキーワード: Inc. / Corp / Company / Ltd / PLC / Holdings / ETF / Trust 等 → 株式・ETF、Gold / Silver / Crude / Natural Gas / Copper 等 → コモディティ、Index / S&P / Nasdaq / Russell 等 → 指数
3. HL 本体にある銘柄 → 暗号資産
4. HL の HIP-3 と同じティッカー → 株式・ETF
5. 残り → 暗号資産

2026-10-24 時点の件数と、上書き CSV に入れた銘柄は末尾の「判定結果」を参照。

## Hyperliquid との比較列

- 対応付けは `config/hl_mapping.csv`(ticker, hl_coin, note)。無い銘柄は `hl-update` が自動で解決して追記する(手で直した行はそのまま):
  `XXX-PERP` → HL 本体の `XXX`、無ければ `kXXX`(k = 1000 倍の単位違い。FR は同じ)、`1000XXX` / `1XXX` も同様。
  本体に無く HIP-3 にあれば、同名が複数の DEX にある場合は **その日の更新時点で OI(ドル建て)が最大の DEX** を選ぶ(`dex:XXX`)。
  コモディティ(金・銀・原油など)やティッカーが違う銘柄(SK hynix = `xyz:SKHX`)、別物の同名(Convex の CVX と Chevron の CVX)は手で書いた。
- HL 7 日年率 = 直近 7 日の毎時 FR の合計 ÷ 7 日 × 365 日(`fundingHistory`)。1 日 1 回、1.1 秒間隔で取得(HL の制限 1200 weight/分、fundingHistory は weight 20)。
- Variational 側の 7 日年率は収集 7 日目から表示し、それまでは「—」。差 = Variational の 7 日年率 − HL の 7 日年率(%ポイント)。

## 運用

```bash
# ローカルで 1 回収集(macOS の python.org 版 Python は SSL_CERT_FILE が要る)
SSL_CERT_FILE=/etc/ssl/cert.pem PYTHONPATH=src python3 -m vfm collect
PYTHONPATH=src python3 -m vfm build --out site          # site/data/ を作る
SSL_CERT_FILE=/etc/ssl/cert.pem PYTHONPATH=src python3 -m vfm hl-update
PYTHONPATH=src python3 -m vfm rebuild-funding --mode confirmed --out /tmp/confirmed.csv
PYTHONPATH=src python3 -m vfm status
python3 -m pytest -q && node --test tests/js/            # テスト
bash scripts/check_identifiers.sh <Organization> <リポ名> <Worker 名>   # 本人名が無いことの確認
python3 scripts/estimate_repo_size.py --runs 288        # リポサイズの見積もり

# Worker(cloudflare-worker/)
cd cloudflare-worker && npx wrangler@4 deploy
bash ../scripts/set_worker_secret.sh --dispatch          # PAT を貼り付けて secret put → deploy
npx wrangler@4 tail                                      # ログ

# 起動の確認
gh run list --workflow collect.yml --limit 10            # workflow_dispatch(Worker)と schedule(保険)の両方が見えること
```

### PAT(fine-grained)

リソース所有者は Organization、対象はこのリポのみ、権限は Actions: Read and write、期限 1 年。Worker の secret `GITHUB_TOKEN` に入れる。
期限が切れると Worker からの起動だけが止まり、Actions の保険 schedule(30 分ごと)だけで動く(`gh run list` で `workflow_dispatch` 起点が消えたら再発行)。
Worker は dispatch 応答のヘッダ `github-authentication-token-expiration` から残り日数をログに出す(残り 14 日以内で警告ログ)。

### Cloudflare の cron 数

Workers Free はアカウント全体で cron 5 本まで。上限に達していると deploy 時に登録だけ黙って拒否されるので、
deploy 後に `wrangler` または API(`/accounts/{id}/workers/scripts/{name}/schedules`)で登録を確認する。拒否されていたら Worker を消し、`collect.yml` の schedule を 10 分間隔(`8,18,28,38,48,58 * * * *`)にして主系にする。

## 既知の制約

- 収集開始(2026-10-24 23:19 UTC)より前のデータは無い。7 日 / 30 日の列は日数が揃うまで「—」。
- OI はトレーダー側のみ(Variational の仕様)。index 価格が無いのでプレミアムは出せない。
- 確定 FR は見込み値型の「窓の直前の最後の観測値」= 推定(支払時刻ちょうどの値ではない。最大で約 2 分前、保険 schedule だけで動いているときは最大約 17 分前)。
- GitHub Actions の起動は予定より遅れる(実行ログの `delay_s`)。
- swap は現在値のみ(履歴は持たない)。

## 判定結果(2026-10-24 時点)

(デプロイ後に `python -m vfm build` の出力から転記)
