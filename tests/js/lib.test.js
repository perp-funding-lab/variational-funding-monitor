// node --test tests/js/
const test = require("node:test");
const assert = require("node:assert/strict");
const L = require("../../site/lib.js");

const T0 = Date.parse("2026-10-25T00:00:00Z"), MIN = 60000;

test("staleWarning: 1 時間 59 分なら警告なし", () => {
  assert.equal(L.staleWarning("2026-10-25T00:00:00Z", T0 + 119 * MIN), null);
});
test("staleWarning: ちょうど 2 時間は警告なし、2 時間 1 分で警告", () => {
  assert.equal(L.staleWarning("2026-10-25T00:00:00Z", T0 + 120 * MIN), null);
  const m = L.staleWarning("2026-10-25T00:00:00Z", T0 + 121 * MIN);
  assert.equal(typeof m, "string");
  assert.match(m, /最後の取得から 2\.0 時間が経過/);
  assert.match(m, /自動更新が止まっている可能性/);
});
test("staleWarning: 長い停止は時間数が丸められる", () => {
  assert.match(L.staleWarning("2026-10-25T00:00:00Z", T0 + 100 * 60 * MIN), /100 時間/);
});
test("staleWarning: 日付が欠落・不正なら警告文", () => {
  for (const bad of [undefined, null, "", "not a date", 12345, {}]) {
    const m = L.staleWarning(bad, T0);
    assert.equal(typeof m, "string", `input ${String(bad)}`);
    assert.match(m, /読めません/);
  }
});

test("anomalyWarning: anomaly_warn をそのまま使い、方式で文面が変わる", () => {
  assert.equal(L.anomalyWarning({anomaly_warn: false, anomalies_today: 30, anomalies_checked_today: 40}), null);
  assert.equal(L.anomalyWarning(null), null);
  assert.equal(L.anomalyWarning({anomaly_warn: true, funding_mode: "estimated", anomalies_today: 12, anomalies_checked_today: 20, anomaly_warn_ratio: 0.5, anomaly_warn_per_day: 20}),
               "判定(見込み値型)と矛盾する動き: 今日 12 件 / 検査 20 件(半分を超えています)");
  assert.equal(L.anomalyWarning({anomaly_warn: true, funding_mode: "confirmed", anomalies_today: 25, anomalies_checked_today: 40, anomaly_warn_ratio: 0.5, anomaly_warn_per_day: 20}),
               "判定(確定値型)と矛盾する動き: 今日 25 件(閾値 20)");
});

test("parsePayments: 欠測は r=null、壊れた要素は捨てる、時刻順", () => {
  const p = L.parsePayments({payments: [[200, 0.0002, "e"], [100, 0.0001, "c"], [300, null, "m"], "junk", [400, "x", "c"]]});
test("parsePayments: 4 つ目の要素(その支払いの間隔)は iv、語の status も受け付ける", () => {
  const p = L.parsePayments({payments: [[100, 0.0001, "c", 28800], [200, 0.0002, "estimated"], [300, null, "missing", 14400], [400, 0.0003, "confirmed", 0]]});
  assert.deepEqual(p, [{t: 100000, r: 0.0001, s: "c", iv: 28800}, {t: 200000, r: 0.0002, s: "e"}, {t: 300000, r: null, s: "m", iv: 14400}, {t: 400000, r: 0.0003, s: "c"}]);
});
  assert.deepEqual(p, [{t: 100000, r: 0.0001, s: "c"}, {t: 200000, r: 0.0002, s: "e"}, {t: 300000, r: null, s: "m"}, {t: 400000, r: null, s: "m"}]);
  assert.deepEqual(L.parsePayments(null), []);
});
test("aggPayments raw: 欠測は 0 ではなく null、推定に印", () => {
  const pays = L.parsePayments({payments: [[0, 0.0001, "c"], [28800, null, "m"], [57600, 0.0002, "e"]]});
  const a = L.aggPayments(pays, "raw");
  assert.deepEqual(a.map(p => p.y), [0.01, null, 0.02]);
  assert.deepEqual(a.map(p => p.est), [false, false, true]);
  assert.equal(a[1].miss, 1);
});
test("aggPayments D: 区間の合計、推定を含む区間は est、欠測だけの日は null、空の日も null", () => {
  const d = 86400;
  const pays = L.parsePayments({payments: [[0, 0.0001, "c"], [28800, 0.0001, "c"], [d, null, "m"], [d + 28800, null, "m"], [3 * d, 0.0003, "e"], [3 * d + 28800, 0.0001, "c"]]});
  const a = L.aggPayments(pays, "D");
  assert.equal(a.length, 4);                                   // 0, 1, 2(空), 3 日目
  assert.deepEqual(a.map(p => p.y === null ? null : +p.y.toFixed(6)), [0.02, null, null, 0.04]);
  assert.deepEqual(a.map(p => p.est), [false, false, false, true]);
  assert.deepEqual(a.map(p => p.miss), [0, 2, 0, 0]);
});
test("cumStat: 期間は (now − days, now] の半開区間(開始ちょうどは入らず、now ちょうどは入る)", () => {
  const now = T0, H8 = 8 * L.HOUR;
  const pays = [{t: now - 7 * L.DAY, r: 0.001, s: "c"}, {t: now - 7 * L.DAY + H8, r: 0.002, s: "c"}, {t: now, r: 0.003, s: "c"}, {t: now + H8, r: 0.5, s: "c"}];
  const c = L.cumStat(pays, now, 7, 28800);
  assert.equal(c.n, 2);                                        // now − 7 日ちょうどの支払いと now より後の支払いは入らない
  assert.ok(Math.abs(c.sum - 0.005) < 1e-15);
  assert.equal(c.partial, false);                              // 最初の支払いは開始 + 1 間隔より前
  const all = L.cumStat(pays, now, null, 28800);
  assert.equal(all.n, 3); assert.equal(all.partial, false);   // 全期間は now 以前すべて、partial にならない
});
test("cumStat: 年率は観測できた日数(欠測は分子にも分母にも入れない、間隔は支払いごと)", () => {
  const now = T0, H = L.HOUR;
  const pays = L.parsePayments({payments: [
    [(now - 3 * L.DAY) / 1000, 0.0008, "c", 28800],             // 間隔の変更前(8h)
    [(now - 2 * L.DAY) / 1000, null, "m"],
    [(now - L.DAY) / 1000, 0.0004, "e"],
    [(now - 12 * H) / 1000, 0.0004, "c"]]});
  const c = L.cumStat(pays, now, null, 14400);
  assert.ok(Math.abs(c.days - (28800 + 14400 + 14400) / 86400) < 1e-15);   // 8h + 4h + 4h = 2/3 日
  assert.ok(Math.abs(c.sum - 0.0016) < 1e-15);
  assert.ok(Math.abs(c.apr - 0.0016 * 365 / (2 / 3)) < 1e-12);
  assert.equal(c.n, 3); assert.equal(c.n_missing, 1); assert.equal(c.start, L.iso(now - 3 * L.DAY));
});
test("cumStat: partial は最初の支払いが開始 + 1 間隔より後のとき、データが無ければ null", () => {
  const now = T0, start = now - 7 * L.DAY;
  const at = t => [{t, r: 0.001, s: "c"}, {t: now, r: 0.001, s: "c"}];
  assert.equal(L.cumStat(at(start + 8 * L.HOUR), now, 7, 28800).partial, false);   // ちょうど 1 間隔後はまだ「期間分ある」
  assert.equal(L.cumStat(at(start + 8 * L.HOUR + 1000), now, 7, 28800).partial, true);
  assert.equal(L.cumStat([{t: now - L.DAY, r: null, s: "m"}], now, 7, 28800), null);            // 欠測だけ
  assert.equal(L.cumStat([{t: now - 30 * L.DAY, r: 0.001, s: "c"}], now, 7, 28800), null);      // 期間内に無い
  assert.equal(L.cumStat([], now, null, 28800), null);
});
test("summarizePayments: 累積・年率・※・観測日数は cumStat、その他は同じ期間の支払いから", () => {
  const now = T0, first = now - 2 * L.DAY;
  const pays = [{t: first, r: 0.001, s: "c"}, {t: first + 8 * L.HOUR, r: null, s: "m"}, {t: first + 16 * L.HOUR, r: -0.0005, s: "e"}, {t: now - 30 * L.DAY - 1, r: 0.9, s: "c"}].sort((a, b) => a.t - b.t);
  const s = L.summarizePayments(pays, 28800, now, 7), c = L.cumStat(pays, now, 7, 28800);
  assert.deepEqual(s.cum, c);
  assert.equal(s.count, 2); assert.equal(s.missing, 1); assert.equal(s.missing, c.n_missing); assert.equal(s.nEst, 1);
  assert.equal(s.total, c.sum * 100); assert.equal(s.apr, c.apr * 100); assert.equal(s.days, c.days);
  assert.ok(Math.abs(s.total - 0.05) < 1e-12);
  assert.ok(Math.abs(s.days - 2 / 3) < 1e-15);                  // 観測できた 2 回 × 8h(欠測の 8h は入れない)
  assert.ok(Math.abs(s.apr - 0.05 * 365 / (2 / 3)) < 1e-9);
  assert.equal(s.partial, false);                              // 30 日前の支払いがあるので「期間分ある」
  assert.equal(s.pays.length, 3);
  assert.ok(Math.abs(s.maxApr - 0.1 * 1095) < 1e-9);          // 8h → ×1095
  assert.equal(s.neg_share, 50);
  const none = L.summarizePayments(pays, 28800, now - 100 * L.DAY, 7);
  assert.equal(none.cum, null); assert.equal(none.total, null); assert.equal(none.count, 0);
});
test("presetDays / presetLabel / windowPays", () => {
  assert.equal(L.presetDays("7"), 7); assert.equal(L.presetDays("365"), 365); assert.equal(L.presetDays("all"), null); assert.equal(L.presetDays("x"), null);
  assert.equal(L.presetLabel("365"), "1年"); assert.equal(L.presetLabel("bogus"), "90日");
  const pays = [{t: T0 - 7 * L.DAY}, {t: T0 - 7 * L.DAY + 1}, {t: T0}, {t: T0 + 1}];
  assert.deepEqual(L.windowPays(pays, T0, 7).map(p => p.t), [T0 - 7 * L.DAY + 1, T0]);
  assert.deepEqual(L.windowPays(pays, T0, null).map(p => p.t), [T0 - 7 * L.DAY, T0 - 7 * L.DAY + 1, T0]);
});
test("expandCum: summary.json の 6 要素の配列を展開(旧形式も)", () => {
  assert.deepEqual(L.expandCum([0.0003, 0.1095, 1, "2026-10-25", 1.0, 2]), {sum: 0.0003, apr: 0.1095, partial: true, start: "2026-10-25", days: 1.0, n_missing: 2});
  assert.deepEqual(L.expandCum([0.0003, 0.1095, 0, "2026-10-25", 6.67]), {sum: 0.0003, apr: 0.1095, partial: false, start: "2026-10-25", days: 6.67, n_missing: 0});
  assert.deepEqual(L.expandCum({sum: 1, apr: 2, partial: 0, start: "s", days: 3}), {sum: 1, apr: 2, partial: false, start: "s", days: 3, n_missing: 0});
  assert.equal(L.expandCum(null), null); assert.equal(L.expandCum(undefined), null);
});
test("cumNote / fmtDays: ※ の説明(期間分ない・欠測・両方)", () => {
  assert.equal(L.fmtDays(6.6667), "6.7"); assert.equal(L.fmtDays(7), "7"); assert.equal(L.fmtDays(29.96), "30"); assert.equal(L.fmtDays(0.333), "0.3"); assert.equal(L.fmtDays(null), "?");
  assert.equal(L.cumNote({partial: false, n_missing: 0, start: "2026-10-25", days: 7}), null);
  assert.equal(L.cumNote({partial: true, n_missing: 0, start: "2026-10-25", days: 6.33}), "2026-10-25 開始(観測 6.3 日分)");
  assert.equal(L.cumNote({partial: false, n_missing: 3, start: "2026-10-01", days: 29}), "欠測 3 回は加えず、観測できた 29 日分で年率換算");
  assert.equal(L.cumNote({partial: true, n_missing: 2, start: "2026-10-25", days: 5.67}),
               "2026-10-25 開始(観測 5.7 日分)。欠測 2 回は加えず、観測できた 5.7 日分で年率換算");
  assert.equal(L.cumNote({partial: true, n_missing: 2, start: "2026-10-25", days: 5.67}, false), "欠測 2 回は加えず、観測できた 5.7 日分で年率換算");
  assert.equal(L.cumNote({partial: true, n_missing: 0, start: "x", days: 1}, false), null);
  assert.equal(L.cumNote(null), null);
});
test("表示用の文字列", () => {
  assert.equal(L.pct(1.234), "+1.23%"); assert.equal(L.pct(-0.5, 1), "-0.5%"); assert.equal(L.pct(null), "—"); assert.equal(L.pct(NaN), "—");
  assert.equal(L.big(7299715), "7.3M"); assert.equal(L.big(2.5e9), "2.50B"); assert.equal(L.big(950), "950"); assert.equal(L.big(null), "—");
  assert.equal(L.esc('<a href="x">&\'</a>'), "&lt;a href=&quot;x&quot;&gt;&amp;&#39;&lt;/a&gt;"); assert.equal(L.esc(null), "");
  assert.equal(L.intervalLabel(28800), "8h"); assert.equal(L.intervalLabel(14400), "4h"); assert.equal(L.intervalLabel(3600), "1h"); assert.equal(L.intervalLabel(null), "—");
  assert.equal(L.shortTicker("BTC-PERP"), "BTC"); assert.equal(L.shortTicker("COPP-SWAP"), "COPP"); assert.equal(L.shortTicker("X-PERP-Y"), "X-PERP-Y");
  assert.equal(L.categoryLabel("equity"), "株式・ETF"); assert.equal(L.categoryLabel("weird"), "weird");
  assert.ok(Math.abs(L.annualize(0.0001, 28800) - 0.1095) < 1e-12);
  assert.equal(L.iso(Date.UTC(2026, 9, 25, 8, 5), true), "2026-10-25 08:05");
  assert.equal(L.fmtTs("2026-10-25T00:08:05Z"), "2026-10-25 00:08"); assert.equal(L.fmtTs(null), "—");
});
test("monthlyCum: 月ごとの合計、欠測は加えない", () => {
  const m = L.monthlyCum([{t: Date.UTC(2026, 8, 30), r: 0.001, s: "c"}, {t: Date.UTC(2026, 9, 1), r: 0.002, s: "e"}, {t: Date.UTC(2026, 9, 2), r: null, s: "m"}, {t: Date.UTC(2026, 9, 3), r: -0.001, s: "c"}]);
  assert.ok(Math.abs(m.get(Date.UTC(2026, 8, 1)) - 0.1) < 1e-9);
  assert.ok(Math.abs(m.get(Date.UTC(2026, 9, 1)) - 0.1) < 1e-9);
});
