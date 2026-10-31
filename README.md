# Variational Funding Rate Monitor

Variational Omni の無期限(perp)全銘柄のファンディングレート(FR)を 10 分ごとに記録し続け、銘柄別の一覧・チャート・
Hyperliquid との比較を静的ページとして公開する。**このページは Variational の公式ページではない**。
データは Variational の公開 API から取得したもので、API に履歴が無いため収集開始(2026-10-24 23:19:15 UTC)から貯まる。

- 公開ページ: https://perp-funding-lab.github.io/variational-funding-monitor/
- データ元: `GET https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/v2/stats`(キー不要、全銘柄が 1 回で返る)。
  HL の列は Hyperliquid の公開 API(`fundingHistory`)
- 仕様: [API](https://docs.variational.io/technical-documentation/api) / [Funding Rates](https://docs.variational.io/omni/trading/funding-rates)

## 構成

```
Cloudflare Worker perp-funding-lab-dispatch(cron 8,18,28,38,48,58 分)
   └─ GitHub App perp-funding-lab-collector の installation token(このリポ限定・actions: write、毎回発行)で workflow_dispatch
GitHub Actions の schedule(保険: 13,43 分。直前 12 分以内に成功していれば何もしない)
   └─ collect.yml : python -m vfm collect → data/ を更新 → commit/push(競合時は rebase して再試行)
                    python -m vfm build   → site/data/*.json を生成(コミットしない)→ GitHub Pages にデプロイ
      hl.yml      : 1 日 1 回(00:40 UTC)Hyperliquid の 7 日年率と対応表を更新
      archive.yml : 月初に先月の日次スナップショットを .csv.gz にまとめる
```

- 収集・集計は Python(標準ライブラリのみ)、ページは素の JS(Plotly は CDN)。外部サービスは Variational・Hyperliquid・GitHub・Cloudflare のみ。
- 認証は Organization 所有の GitHub App。個人の PAT は使わない。Actions の一覧には起動者として `perp-funding-lab-collector[bot]` が出る。
  コミットの作者・コミッターも App の bot(`perp-funding-lab-collector[bot]` と bot の noreply アドレス)に揃えている。
- collect.yml は同時に 1 つだけ動く(concurrency group `collect`)。hl.yml / archive.yml は別のグループ(触るファイルが重ならず、push が競合しても rebase で解ける)。
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

支払時刻(00:00 UTC)をまたぐ挙動(2026-10-25、23:59:30 / 00:01:30 / 00:12:00 の 3 回):

- 00:01:30 時点では全 564 銘柄の `next_timestamp` が 00:00 のままで、値も 23:59:30 と完全に同じ(**支払時刻で値が凍結され、窓の切り替えは数分遅れる**)。
- 00:12:00 時点では全銘柄が次の窓(1h/4h/8h)に進み、0 でも金利成分でもない 83 銘柄のうち 79 銘柄の値が凍結値から変わっていた(新しい窓の見込み値として動き出す)。
  0 の銘柄 111 は 0 のまま、金利成分の銘柄 370 のうち 368 は金利成分のまま。
- したがって 58 分の観測(支払 2 分前)と 08 分の観測(凍結中なら支払後の確定値そのもの)のどちらかが「窓の最後の値」になる。
  推定 FR の `basis_obs_ts` が支払時刻以降なら凍結後の値で、実際に適用された値と一致する見込み。
ドキュメントの「8 時間窓の BTC perp は 1〜7 時間目は 0 を適用し、8 時間目に直前 8 時間のデータから計算する」という記述とも整合する
(`last` はプレミアムの時間加重平均を反映して窓の中で更新される)。

したがって確定 FR は次のように作る(`config/settings.json` の `funding_mode = "estimated"`):

- 窓 T(`next_timestamp = T`)が閉じた(`next_timestamp` が進んだ)とき、T の直前に取れた最後の値を **時刻 T の FR(推定)** として記録する。
  58 分の起動がこのためにある(支払直前の最終観測)。ページでは「推定」と分かるようにしている。
- 「直前」の条件: 最後の観測が支払時刻の 30 分前以降(または支払後の凍結中)であること。それより古い観測しか無い窓は推定せず **欠測** にする
  (収集が止まっていた窓に、何時間も前の値を推定値として入れない)。Worker が動いていれば 2 分前、保険の schedule だけでも 17 分前に観測がある。
- 支払間隔が窓の途中で変わったとき(Bybit に合わせた 8h→4h など。next_timestamp は正当に前へ動く)は、巻き戻りではなく間隔の変更として扱う:
  旧窓は支払われずに打ち切り(FR も欠測も作らない)、新しい窓をその時点から追う。窓の境目で変わった場合は旧窓を通常どおり記録する。
  間隔が同じまま next_timestamp が戻った・支払時刻より前に動いた値は API の一時的な不整合として記録せず、その回はその銘柄を不在扱いにする。
- 取得に居なかった(funding が null・一覧から消えた・不整合だった)回しか無い窓は 0 ではなく欠測になる。
- もう一方の方式(確定値型: 窓で最初に取れた値を T − interval の確定値とする)も実装・テスト済みで、
  `python -m vfm rebuild-funding --mode confirmed --out /tmp/confirmed.csv` で観測ログ(30 日分)から作り直せる。
- 本番でも毎回、判定と矛盾する動きを数える。見込み値型なら「0 でも金利成分でもない銘柄を 25 分以上観測できた窓」を検査とし、
  その中で一度も値が動かなかった窓を矛盾として数える。1 日の矛盾 ÷ 検査が 0.5 を超える(検査 20 以上)とページに警告が出る
  (確定値型で運用する場合は、窓の中で値が変わった件数が 1 日 20 を超えたら警告)。

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

`python scripts/estimate_repo_size.py --runs 150` が、実データの 1 回分の増分を一時リポに 150 回コミットして `git gc --aggressive` 後の pack サイズから外挿する。

- 1 日の窓(支払)の数: 2,709(4 時間 304 銘柄 × 6 + 8 時間 255 銘柄 × 3 + 1 時間 5 銘柄 × 24)
- 1 回の収集で増える行: 観測ログ約 118 行(変化点 80 + 窓の最初/最後)、確定 FR 約 19 行、実行ログ 1 行、毎時スナップショット 582 行(6 回に 1 回)。
  ほかに `meta.json` と `swaps_latest.json`(8 KB)が毎回書き換わる
- 収集回数: Worker 経由 144 回/日。保険の schedule は直前 12 分以内に成功していれば何も書かないので、150 回/日で見積もる
- 模擬の結果: 1 回あたり約 5.3 KB(pack)→ 54,750 回/年で約 290 MB。月締めの .csv.gz 約 9 MB × 12 = 約 110 MB、HL の JSON 約 14 MB
- **合計 約 410 MB / 年 < 1 GB**(GitHub Pages の推奨上限)。収集ジョブは sparse checkout で `data/archive` を持たないので、毎回のチェックアウトは当月分だけ。

## ページ

元の「BTC Funding Rate Monitor」の Hyperliquid 銘柄別タブの UI・JS・説明文を流用し、Variational 向けに書き直した。

- 一覧(見出しで並べ替え): 銘柄、名前、種類、支払間隔、次回支払(UTC)、データ開始日、直近 FR(1 回分と年率)、7 日年率、30 日年率、期間の累積 FR / 年率(※ はデータが期間分ない銘柄)、HL 7 日年率、差(Variational − HL、%ポイント)、OI(ロング+ショート)、ロング比率、24h 出来高、スプレッド(bps)
- フィルタ: 種類、OI が○百万ドル以上、24h 出来高が○百万ドル以上、銘柄検索
- 行クリックで下にチャート(支払ごとの FR を生データ・日・週・月でまとめ、累積 FR)。月ごとの累積 FR の銘柄比較(ヒートマップ、OI 上位 60)
- swap タブ: 現在値のみ(ロング/ショート別の年率と 1 回分、次回支払、OI、出来高)
- 状態表示: 最終更新(UTC)、収集開始日、欠測の数、方式。最後の成功取得から 2 時間を超えると上部に警告
- 初回の読み込みは合計 2 MB 以内(Plotly の cartesian バンドルを含む)。銘柄別の詳細は行クリック時に `data/tickers/<TICKER>.json` を読む

年率換算 = 1 回分の FR × (365 日 ÷ 支払間隔)。4 時間なら ×2190、8 時間なら ×1095、1 時間なら ×8760(API の `annualized_rate` と一致)。
期間の年率 = 期間の累積 FR ÷ 観測できた日数 × 365。観測できた日数 = 期間内の(欠測でない)支払いの間隔の合計で、欠測の窓は累積にも日数にも入れない
(0 として薄めない。欠測が無ければ期間の長さと同じ)。期間内に欠測があれば ※ を付け、説明に欠測の数を出す。
ページの JS の計算(期間の累積・年率・※、日/週/月のまとめ、月ごとの累積)は Python(`src/vfm/aggregate.py`)と同じ定義で、
同じ入力で結果が一致することを `tests/fixtures/cross_lang.json` で確かめている。
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
python3 -m pytest -q && node --test tests/js/*.test.js   # テスト
python3 tests/fixtures/make_cross_lang.py               # JS と Python の一致テストの答えを作り直す(集計の定義を変えたとき)
bash scripts/check_identifiers.sh                       # 本人名が無いこと(git の全履歴・コード・識別子)
python3 scripts/check_public_api.py --extra <本人のアカウント名>   # 認証なしの公開 API に本人名が無いこと
python3 scripts/estimate_repo_size.py --runs 150        # リポサイズの見積もり
```

### GitHub App(perp-funding-lab-collector)

Organization 所有の private な App(この Organization にだけインストールできる)。権限は administration・contents・actions・workflows・pages が write、
metadata が read。Webhook は無効。App ID・installation ID・bot の ID は `config/github_app.json`(秘密ではない)。

- 秘密鍵は作成時に manifest flow の変換 API から受け取り、macOS の Keychain(サービス `perp-funding-lab-collector`)にだけ置いた。ダウンロードしていない。
- ローカルの操作はすべて `scripts/gh_app.py` が App のトークン(installation token、1 時間で失効)で行う。トークンは URL・ログ・ファイルに残さない
  (git push は環境変数経由の credential helper で渡し、既存の helper は外す)。

```bash
python3 scripts/gh_app.py check --repo                  # トークン発行の確認(期限と権限だけ表示)
python3 scripts/gh_app.py dispatch collect.yml          # 収集を手動で 1 回(起動者は App の bot)
python3 scripts/gh_app.py dispatch hl.yml
python3 scripts/gh_app.py runs --limit 20               # 実行一覧(起動者つき)
python3 scripts/gh_app.py push -- origin main           # App のトークンで push
```

### Worker(perp-funding-lab-dispatch)

```bash
cd cloudflare-worker && npx wrangler@4 deploy
python3 ../scripts/gh_app.py pkcs8 | npx wrangler@4 secret put GITHUB_APP_PRIVATE_KEY   # 鍵を PKCS#8 にして標準入力で渡す(画面に出さない)
npx wrangler@4 deploy                                    # secret put の後は必ず deploy し直す
npx wrangler@4 tail                                      # ログ
```

dispatch のたびに App の JWT を作り、このリポ限定・actions: write だけの installation token を発行して使う(PAT と違って期限切れが無い)。
`workers_dev = false`(公開 URL を作らない。workers.dev のサブドメインはアカウント名を含むので出さない)。
Workers のアカウント全体の cron 数には上限がある。deploy 後に API(`/accounts/{id}/workers/scripts/{name}/schedules`)で登録を確かめ、
拒否されていたら Worker を消して collect.yml の schedule を 10 分間隔(`8,18,28,38,48,58 * * * *`、`--skip-if-fresh` なし)の主系にする。

## 既知の制約

- 収集開始(2026-10-24 23:19:15 UTC)より前のデータは無い。7 日 / 30 日の列は日数が揃うまで「—」。
- 2026-10-25 01:25:47 UTC 〜 2026-10-31 のローカル収集の再開までは収集していない(手元での収集を止めていた)。この間の窓はすべて欠測。
- OI はトレーダー側のみ(Variational の仕様)。index 価格が無いのでプレミアムは出せない。
- 確定 FR は見込み値型の「窓の直前の最後の観測値」= 推定(支払時刻ちょうどの値ではない。支払時刻の 30 分以内に観測が無ければ欠測)。
- GitHub Actions の起動は予定より遅れる(実行ログの `delay_s`)。
- swap は現在値のみ(履歴は持たない)。

## 稼働の記録(2026-10-31〜11-01)

| 項目 | 値 |
|---|---|
| 収集開始(ローカルの最初の取得) | 2026-10-24 23:19:15 UTC |
| 収集していない期間(欠測) | 2026-10-25 01:25:47 〜 2026-10-31 08:29:44 UTC(手元の収集を止めていた。欠測 16,919 件) |
| ローカルの最後の取得(初回 push に含めた) | 2026-10-31 08:48:05 UTC |
| クラウドの最初の取得 | 2026-10-31 08:53:04 UTC(間に支払時刻は無いので、引き継ぎによる欠測は 0) |
| Worker 起動の収集 | 最初の 14 時間で 86 回すべて成功。予定時刻から取得開始まで中央値 10 秒、90% 点 13 秒、最大 26 秒 |
| 保険の schedule | 同じ 14 時間で 3 回しか起動しなかった(GitHub 側の間引き)。どれも直前に成功していたので何もしていない |
| 推定 FR の根拠の観測 | 支払時刻の中央値 75 秒前(最短 64 秒、最長 416 秒)。クラウド開始後の支払 1,217 件はすべて推定値で記録(欠測 0) |
| リポの増え方(実測) | クラウドの 88 コミットで +380 KB(1 コミット 4.3 KB)→ 1 年で約 231 MB + 月締めの圧縮ファイル約 110 MB + HL 約 14 MB ≒ 355 MB |
| GitHub App | perp-funding-lab-collector(App ID 5553022、installation 177152383、Organization の全リポ) |
| Cloudflare の cron | アカウント全体で 8 本が登録済み(公式の上限は Free 5 本・Paid 250 本。今回の 1 本も登録を拒否されなかった) |

## 判定結果(2026-11-01 時点)

### 種類別の件数(掲載中の perp)

- 暗号資産: 428
- 株式・ETF: 122
- コモディティ: 10
- 指数: 0
- プレIPO: 3
- その他: 0
- 合計: 563(掲載終了・一時的に無い銘柄 3)

### 上書き CSV(config/category_overrides.csv)に入れた銘柄

- AGLD-PERP → 暗号資産: Adventure Gold はゲームのトークン(名前の Gold はコモディティではない)
- PAXG-PERP → コモディティ: 金に裏付けられたトークン(金価格に連動するのでコモディティ扱い)
- XAUT-PERP → コモディティ: 金に裏付けられたトークン(金価格に連動するのでコモディティ扱い)
- CVX-PERP → 暗号資産: Convex Finance(HIP-3 の CVX = Chevron とは別物)
- QNT-PERP → 暗号資産: Quant(HIP-3 の同名ティッカーとは別物)
- TWT-PERP → 暗号資産: Trust Wallet(名前の Trust は信託ではない)
- GIGGLE-PERP → 暗号資産: Giggle Fund はミームコイン(名前の Fund はファンドではない)
- OPENAI-PERP → プレIPO: 未上場企業
- ANTHROPIC-PERP → プレIPO: 未上場企業
- BSP-PERP → プレIPO: Bending Spoons は未上場(イタリアの非公開企業)

### HL 比較

対応あり 249 / 563、HL 7 日年率あり 249(HL 取得 2026-10-31T08:54:54Z)。
対応表は `config/hl_mapping.csv`(566 行: 本体 155、HIP-3 78、単位違い 4、手動 15(うち対応なしと明示 3)、自動で対応なし 314)。
