/* Variational Funding Rate Monitor — 画面に依存しない純粋関数。
   ブラウザでは window.VFM、Node(テスト)では module.exports として読む。時刻はすべて UTC、rate は小数(0.0001 = 0.01%)。 */
(function (root, factory) {
  const lib = factory();
  if (typeof module === "object" && module && module.exports) module.exports = lib;
  else root.VFM = lib;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const HOUR = 3600000, DAY = 86400000;

  // ───────── 時刻 ─────────
  const pad = n => String(n).padStart(2, "0");
  function iso(ms, withTime) {
    const d = new Date(ms);
    const s = `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
    return withTime ? `${s} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}` : s;
  }
  /* ISO 文字列("2026-10-25T00:08:05Z")→ ms。読めなければ NaN */
  function parseTs(s) { return (typeof s === "string" && s) ? Date.parse(s) : NaN; }
  /* ISO 文字列 → "YYYY-MM-DD HH:MM"(読めなければ "—") */
  function fmtTs(s, withTime = true) { const t = parseTs(s); return Number.isFinite(t) ? iso(t, withTime) : "—"; }
  const dayStart = ms => Math.floor(ms / DAY) * DAY;
  function weekStart(ms) { const d = dayStart(ms); return d - ((new Date(d).getUTCDay() + 6) % 7) * DAY; }   // 月曜はじまり
  function monthStart(ms) { const d = new Date(ms); return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), 1); }
  function nextBucket(ms, f) {
    if (f === "D") return ms + DAY;
    if (f === "W") return ms + 7 * DAY;
    const d = new Date(ms); return Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 1);
  }
  const bucketOf = {D: dayStart, W: weekStart, M: monthStart};

  // ───────── 取得停止の警告 ─────────
  const STALE_HOURS = 2;
  function fmtHours(h) { return h < 48 ? h.toFixed(1) : String(Math.round(h)); }
  /* 最後の取得(summary.last_success)から 2 時間を超えていれば警告文、そうでなければ null。日付が読めないときも警告文。 */
  function staleWarning(lastSuccessIso, nowMs) {
    const t = parseTs(lastSuccessIso);
    if (!Number.isFinite(t)) return "最後の取得時刻が読めません(summary.json の last_success が欠落か不正)。自動更新が止まっている可能性があります";
    const h = (nowMs - t) / HOUR;
    if (h > STALE_HOURS) return `最後の取得から ${fmtHours(h)} 時間が経過しています(自動更新が止まっている可能性)`;
    return null;
  }

  /* 判定(確定値型 / 見込み値型)と矛盾する動きの警告。summary の anomaly_warn(サーバ側で判定)をそのまま使い、null なら警告なし */
  function anomalyWarning(s) {
    if (!s || !s.anomaly_warn) return null;
    const n = s.anomalies_today ?? "?", m = s.anomalies_checked_today ?? "?";
    if (s.funding_mode === "estimated") {
      const ratio = Number(s.anomaly_warn_ratio);
      const lim = (!Number.isFinite(ratio) || ratio === 0.5) ? "半分" : `${Math.round(ratio * 100)}%`;
      return `判定(見込み値型)と矛盾する動き: 今日 ${n} 件 / 検査 ${m} 件(${lim}を超えています)`;
    }
    return `判定(確定値型)と矛盾する動き: 今日 ${n} 件(閾値 ${s.anomaly_warn_per_day ?? 20})`;
  }

  // ───────── 表示用の文字列 ─────────
  const pct = (v, d = 2, sign = true) => (v == null || Number.isNaN(v)) ? "—" : (sign && v >= 0 ? "+" : "") + v.toFixed(d) + "%";
  const cls = v => v >= 0 ? "pos" : "neg";
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const big = v => (v == null || !Number.isFinite(v)) ? "—" : v >= 1e9 ? (v / 1e9).toFixed(2) + "B" : v >= 1e6 ? (v / 1e6).toFixed(1) + "M" : v >= 1e3 ? (v / 1e3).toFixed(0) + "K" : v.toFixed(0);
  const CATEGORIES = ["crypto", "equity", "commodity", "index", "preipo", "other"];
  const CATEGORY_LABEL = {crypto: "暗号資産", equity: "株式・ETF", commodity: "コモディティ", index: "指数", preipo: "プレIPO", other: "その他"};
  const categoryLabel = c => CATEGORY_LABEL[c] || String(c ?? "—");
  const intervalLabel = sec => (sec == null || !(sec > 0)) ? "—" : sec % 3600 === 0 ? (sec / 3600) + "h" : Math.round(sec / 60) + "m";
  const shortTicker = t => String(t ?? "").replace(/-(PERP|SWAP)$/, "");
  /* 1 回分の FR → 年率(年率 = r × 365 日 ÷ 間隔) */
  const annualize = (rate, sec) => (rate == null || !(sec > 0)) ? null : rate * 365 * 86400 / sec;

  // ───────── 期間 ─────────
  const PRESETS = [["7日", "7"], ["30日", "30"], ["90日", "90"], ["180日", "180"], ["1年", "365"], ["全期間", "all"]];
  const presetLabel = key => (PRESETS.find(p => p[1] === key) || PRESETS[2])[0];
  /* 期間の [start, end](ms)。全期間は最初の支払いから */
  function windowRange(presetKey, endMs, firstMs) {
    const n = Number(presetKey);
    if (presetKey === "all" || !(n > 0)) return [firstMs != null ? Math.min(firstMs, endMs) : endMs, endMs];
    return [endMs - n * DAY, endMs];
  }

  // ───────── 支払いの列 ─────────
  /* tickers/<T>.json の payments([[pay_ts 秒, rate|null, "c"|"e"|"m"], ...])→ [{t: ms, r, s}](時刻順)。壊れた要素は捨てる */
  function parsePayments(detail) {
    const src = detail && Array.isArray(detail.payments) ? detail.payments : [];
    const out = [];
    for (const p of src) {
      if (!Array.isArray(p) || !Number.isFinite(p[0])) continue;
      const s = p[2] === "e" ? "e" : p[2] === "m" ? "m" : "c";
      const r = (s === "m" || p[1] == null || !Number.isFinite(p[1])) ? null : p[1];
      out.push({t: p[0] * 1000, r, s: r == null ? "m" : s});
    }
    return out.sort((a, b) => a.t - b.t);
  }
  /* FR(%)を f(raw / D / W / M)でまとめた [{x, y, est, n, miss}]。
     raw は 1 回の支払いが 1 本。欠測(m)は y = null(0 にしない)。区間に推定(e)が 1 つでもあれば est。
     D/W/M は最初と最後の区間の間を埋め、支払いのない区間は y = null。 */
  function aggPayments(pays, f) {
    if (f === "raw") return pays.map(p => ({x: p.t, y: p.r == null ? null : p.r * 100, est: p.s === "e" && p.r != null, n: p.r == null ? 0 : 1, miss: p.r == null ? 1 : 0}));
    const bucket = bucketOf[f] || dayStart, m = new Map();
    for (const p of pays) {
      const k = bucket(p.t);
      let a = m.get(k); if (!a) { a = {x: k, y: null, est: false, n: 0, miss: 0}; m.set(k, a); }
      if (p.r == null) a.miss++; else { a.y = (a.y || 0) + p.r * 100; a.n++; if (p.s === "e") a.est = true; }
    }
    const keys = [...m.keys()].sort((a, b) => a - b);
    if (!keys.length) return [];
    const out = [];
    for (let k = keys[0]; k <= keys[keys.length - 1]; k = nextBucket(k, f)) out.push(m.get(k) || {x: k, y: null, est: false, n: 0, miss: 0});
    return out;
  }
  /* 期間 [startMs, endMs] の支払い(フィルタ済み)のまとめ。欠測は合計に入れず件数だけ数える。
     年率 = 累積 ÷ 実日数 × 365。データが期間分ない(最初の支払いが start より後)なら partial。 */
  function summarizePayments(pays, intervalSec, startMs, endMs) {
    const paid = pays.filter(p => p.r != null), r = paid.map(p => p.r), n = r.length;
    const sum = r.reduce((a, b) => a + b, 0);
    const pos = r.filter(v => v > 0).reduce((a, b) => a + b, 0), neg = r.filter(v => v < 0).reduce((a, b) => a + b, 0);
    const first = pays.length ? pays[0].t : null;
    // 実日数は 1 支払間隔を下限にする(builder の cum_stat と同じ。支払 1 回だけの期間で年率が跳ねないように)
    const from = first != null ? Math.max(startMs, first) : startMs, days = Math.max((endMs - from) / DAY, intervalSec > 0 ? intervalSec / 86400 : 0);
    const k = intervalSec > 0 ? 365 * 86400 / intervalSec : null;
    return {count: n, missing: pays.length - n, nEst: paid.filter(p => p.s === "e").length,
            total: sum * 100, pos: pos * 100, neg: neg * 100, neg_share: n ? r.filter(v => v < 0).length / n * 100 : NaN,
            apr: days > 0 ? sum * 100 * 365 / days : null, days, start: first, partial: first != null && first > startMs + HOUR,
            mean: n ? sum / n * 100 : null,
            max: n ? Math.max(...r) * 100 : null, min: n ? Math.min(...r) * 100 : null,
            maxApr: n && k ? Math.max(...r) * k * 100 : null, minApr: n && k ? Math.min(...r) * k * 100 : null};
  }
  /* 月ごとの累積 FR(%)。Map(月初 ms → 合計)。欠測は加えない */
  function monthlyCum(pays) {
    const m = new Map();
    for (const p of pays) { if (p.r == null) continue; const k = monthStart(p.t); m.set(k, (m.get(k) || 0) + p.r * 100); }
    return m;
  }

  return {HOUR, DAY, iso, parseTs, fmtTs, dayStart, weekStart, monthStart, nextBucket, bucketOf, STALE_HOURS, staleWarning, anomalyWarning,
          pct, cls, esc, big, CATEGORIES, CATEGORY_LABEL, categoryLabel, intervalLabel, shortTicker, annualize,
          PRESETS, presetLabel, windowRange, parsePayments, aggPayments, summarizePayments, monthlyCum};
});
