// ページの JS(site/lib.js)が Python(src/vfm/aggregate.py)と同じ答えを出すこと。
// 入力と Python の答えは tests/fixtures/cross_lang.json(tests/fixtures/make_cross_lang.py が作る)。
// node --test tests/js/*.test.js
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const L = require("../../site/lib.js");

const FIXTURE = path.join(__dirname, "..", "fixtures", "cross_lang.json");
const {cases} = JSON.parse(fs.readFileSync(FIXTURE, "utf8"));

/* 相対誤差 rel 以内(0 の近くは絶対誤差 abs 以内) */
function near(got, exp, what, rel = 1e-12, abs = 1e-15) {
  assert.equal(typeof got, "number", `${what}: number expected, got ${got}`);
  const tol = Math.max(rel * Math.max(Math.abs(got), Math.abs(exp)), abs);
  assert.ok(Math.abs(got - exp) <= tol, `${what}: got ${got}, expected ${exp} (diff ${Math.abs(got - exp)}, tol ${tol})`);
}
const ym = ms => L.iso(ms).slice(0, 7);

test("fixture を読める(9 件以上、全プリセットと raw/D/W/M の答えがある)", () => {
  assert.ok(Array.isArray(cases) && cases.length >= 9, "cases");
  for (const c of cases) {
    assert.deepEqual(Object.keys(c.expected.cum).sort(), ["180", "30", "365", "7", "90", "all"], c.name);
    assert.deepEqual(Object.keys(c.expected.agg).sort(), ["D", "M", "W", "raw"], c.name);
  }
});

for (const c of cases) {
  test(`Python と一致: ${c.name}`, () => {
    const pays = L.parsePayments({payments: c.payments});
    assert.equal(pays.length, c.payments.length, "parsePayments は要素を落とさない");

    // 期間の累積・年率・※(cum_stat)
    for (const [key, exp] of Object.entries(c.expected.cum)) {
      const days = key === "all" ? null : Number(key), what = `cum[${key}]`;
      const got = L.cumStat(pays, c.now_ms, days, c.interval);
      if (exp === null) { assert.equal(got, null, `${what} は null`); continue; }
      assert.notEqual(got, null, `${what} は null ではない`);
      near(got.sum, exp.sum, `${what}.sum`);
      near(got.apr, exp.apr, `${what}.apr`);
      near(got.days, exp.days, `${what}.days`);
      assert.equal(got.partial, exp.partial, `${what}.partial`);
      assert.equal(got.start, exp.start, `${what}.start`);
      assert.equal(got.n, exp.n, `${what}.n`);
      assert.equal(got.n_missing, exp.n_missing, `${what}.n_missing`);
    }

    // 日・週・月のまとめ(agg_payments)
    for (const f of ["raw", "D", "W", "M"]) {
      const got = L.aggPayments(pays, f), exp = c.expected.agg[f];
      assert.equal(got.length, exp.length, `agg[${f}] の長さ`);
      exp.forEach((e, i) => {
        const g = got[i], what = `agg[${f}][${i}]`;
        assert.equal(g.x, e.x, `${what}.x`);
        if (e.y === null) assert.equal(g.y, null, `${what}.y は null(欠測・空の区間は 0 にしない)`);
        else near(g.y, e.y, `${what}.y`);
        assert.equal(g.est, e.est, `${what}.est`);
        assert.equal(g.n, e.n, `${what}.n`);
        assert.equal(g.miss, e.miss, `${what}.miss`);
      });
    }

    // 月ごとの累積(monthly_sums は小数、monthlyCum は %)
    const got = new Map([...L.monthlyCum(pays)].map(([ms, v]) => [ym(ms), v]));
    assert.deepEqual([...got.keys()].sort(), Object.keys(c.expected.monthly).sort(), "monthly の月");
    for (const [k, v] of Object.entries(c.expected.monthly)) near(got.get(k), v * 100, `monthly[${k}]`, 1e-12, 1e-12);
  });
}

test("summarizePayments の累積・年率・※ は cumStat と同じ(Python の答えとも一致)", () => {
  for (const c of cases) {
    const pays = L.parsePayments({payments: c.payments});
    for (const [key, exp] of Object.entries(c.expected.cum)) {
      const days = key === "all" ? null : Number(key), what = `${c.name} cum[${key}]`;
      const s = L.summarizePayments(pays, c.interval, c.now_ms, days);
      if (exp === null) { assert.equal(s.total, null, what); assert.equal(s.cum, null, what); continue; }
      near(s.total, exp.sum * 100, `${what}.total`);
      near(s.apr, exp.apr * 100, `${what}.apr`);
      near(s.days, exp.days, `${what}.days`);
      assert.equal(s.partial, exp.partial, `${what}.partial`);
      assert.equal(s.count, exp.n, `${what}.count`);
      assert.equal(s.missing, exp.n_missing, `${what}.missing`);
    }
  }
});
