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

test("parsePayments: 欠測は r=null、壊れた要素は捨てる、時刻順", () => {
  const p = L.parsePayments({payments: [[200, 0.0002, "e"], [100, 0.0001, "c"], [300, null, "m"], "junk", [400, "x", "c"]]});
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
test("summarizePayments: 欠測は件数のみ、年率は実日数、期間分ないデータは partial", () => {
  const start = T0 - 7 * L.DAY, end = T0, first = T0 - 2 * L.DAY;
  const pays = [{t: first, r: 0.001, s: "c"}, {t: first + 8 * L.HOUR, r: null, s: "m"}, {t: first + 16 * L.HOUR, r: -0.0005, s: "e"}];
  const s = L.summarizePayments(pays, 28800, start, end);
  assert.equal(s.count, 2); assert.equal(s.missing, 1); assert.equal(s.nEst, 1);
  assert.ok(Math.abs(s.total - 0.05) < 1e-9);
  assert.ok(Math.abs(s.apr - 0.05 / 2 * 365) < 1e-9);          // 2 日分で年率換算
  assert.equal(s.partial, true); assert.equal(s.start, first);
  assert.ok(Math.abs(s.maxApr - 0.1 * 1095) < 1e-9);          // 8h → ×1095
  assert.equal(s.neg_share, 50);
});
test("windowRange / presetLabel", () => {
  assert.deepEqual(L.windowRange("7", T0, T0 - 100 * L.DAY), [T0 - 7 * L.DAY, T0]);
  assert.deepEqual(L.windowRange("all", T0, T0 - 100 * L.DAY), [T0 - 100 * L.DAY, T0]);
  assert.equal(L.presetLabel("365"), "1年"); assert.equal(L.presetLabel("bogus"), "90日");
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
