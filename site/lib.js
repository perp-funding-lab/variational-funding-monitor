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
  /* 期間のキー → 日数(全期間は null) */
  const presetDays = key => { const n = Number(key); return (key === "all" || !(n > 0)) ? null : n; };
  /* 期間 (now − days 日, now] に入るか。半開区間で、days が null なら now 以前すべて(src/vfm/aggregate.py の cum_stat と同じ) */
  function inWindow(nowMs, days) {
    const startMs = days == null ? null : nowMs - days * DAY;
    return t => (startMs == null || t > startMs) && t <= nowMs;
  }
  const windowPays = (pays, nowMs, days) => { const f = inWindow(nowMs, days); return pays.filter(p => f(p.t)); };

  // ───────── 支払いの列 ─────────
  /* tickers/<T>.json の payments([[pay_ts 秒, rate|null, "c"|"e"|"m", 間隔(秒)], ...])→ [{t: ms, r, s, iv}](時刻順)。
     4 つ目の間隔は、その支払いの間隔がファイルの interval と違うとき(間隔の変更前)だけ付き、iv に入る(無ければ iv は付けない)。
     status は confirmed / estimated / missing の語でも受け付ける(Python と同じ)。壊れた要素は捨てる */
  function parsePayments(detail) {
    const src = detail && Array.isArray(detail.payments) ? detail.payments : [];
    const out = [];
    for (const p of src) {
      if (!Array.isArray(p) || !Number.isFinite(p[0])) continue;
      const st = p[2], s = (st === "e" || st === "estimated") ? "e" : (st === "m" || st === "missing") ? "m" : "c";
      const r = (s === "m" || p[1] == null || !Number.isFinite(p[1])) ? null : p[1];
      const o = {t: p[0] * 1000, r, s: r == null ? "m" : s};
      if (Number.isFinite(p[3]) && p[3] > 0) o.iv = p[3];
      out.push(o);
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
  /* 期間 (now − days 日, now](days が null なら全期間)の累積 FR と年率換算。src/vfm/aggregate.py の cum_stat と同じ定義・同じ計算順
     (tests/js/cross_lang.test.js が Python の答えと突き合わせる):
     - sum = 期間内の(欠測でない)支払いの合計。欠測は加えない(0 にしない)
     - days = 観測できた日数 = 期間内の支払いの間隔(p.iv、無ければ intervalSec)の合計 ÷ 1 日。欠測の窓は分母にも入れない
     - apr = sum × 365 ÷ days
     - partial(ページの ※)= データが期間分ない(最初の支払いが期間の開始 + 1 間隔より後)。全期間は false
     - n = 期間内の支払いの数、n_missing = 期間内の欠測の数、start = 最初の支払いの日(YYYY-MM-DD)
     支払いが 1 つも無い、または期間内に無ければ null。pays は parsePayments の結果(時刻順) */
  function cumStat(pays, nowMs, days, intervalSec) {
    const valid = pays.filter(p => p.r != null);
    if (!valid.length) return null;
    const first = valid[0].t;
    const startMs = days == null ? null : nowMs - days * DAY;
    const partial = days != null && first > startMs + intervalSec * 1000;
    const inWin = inWindow(nowMs, days);
    const sel = valid.filter(p => inWin(p.t));
    if (!sel.length) return null;
    let ivSum = 0, sum = 0, nMissing = 0;
    for (const p of sel) ivSum += p.iv || intervalSec;
    for (const p of sel) sum += p.r;
    for (const p of pays) if (p.r == null && inWin(p.t)) nMissing++;
    const covered = ivSum / 86400;
    return {sum, apr: covered > 0 ? sum * 365 / covered : null, partial, start: iso(first), days: covered, n: sel.length, n_missing: nMissing};
  }
  /* 銘柄の詳細の指標カード。累積・年率・※(partial / 欠測)・観測日数は cumStat から取る(一覧の「期間の累積FR / 年率」の列と同じ値)。
     回数・プラス分 / マイナス分・最大 / 最小は同じ期間 (now − days 日, now] の支払いから。% の値は ×100 済み */
  function summarizePayments(pays, intervalSec, nowMs, days) {
    const win = windowPays(pays, nowMs, days), c = cumStat(pays, nowMs, days, intervalSec);
    const paid = win.filter(p => p.r != null), r = paid.map(p => p.r), n = r.length;
    const sumR = r.reduce((a, b) => a + b, 0);
    const pos = r.filter(v => v > 0).reduce((a, b) => a + b, 0), neg = r.filter(v => v < 0).reduce((a, b) => a + b, 0);
    const k = intervalSec > 0 ? 365 * 86400 / intervalSec : null;
    return {cum: c, pays: win, count: n, missing: win.length - n, nEst: paid.filter(p => p.s === "e").length,
            total: c ? c.sum * 100 : null, apr: c && c.apr != null ? c.apr * 100 : null, days: c ? c.days : null,
            partial: c ? c.partial : false, start: c ? c.start : null,
            pos: pos * 100, neg: neg * 100, neg_share: n ? r.filter(v => v < 0).length / n * 100 : NaN,
            mean: n ? sumR / n * 100 : null,
            max: n ? Math.max(...r) * 100 : null, min: n ? Math.min(...r) * 100 : null,
            maxApr: n && k ? Math.max(...r) * k * 100 : null, minApr: n && k ? Math.min(...r) * k * 100 : null};
  }
  /* summary.json の cum[k]([sum, apr, partial(0/1), start, days(観測日数), n_missing])→ {sum, apr, partial, start, days, n_missing}。
     旧形式(5 要素の配列・オブジェクト)も受け付ける。null はそのまま */
  function expandCum(c) {
    if (Array.isArray(c)) return {sum: c[0] ?? null, apr: c[1] ?? null, partial: !!c[2], start: c[3] ?? null, days: c[4] ?? null, n_missing: c[5] || 0};
    if (c && typeof c === "object") return {...c, partial: !!c.partial, n_missing: c.n_missing || 0};
    return null;
  }
  /* 日数の表示(小数 1 桁。整数なら小数なし) */
  function fmtDays(d) {
    if (d == null || !Number.isFinite(d)) return "?";
    const x = Math.round(d * 10) / 10;
    return Number.isInteger(x) ? String(x) : x.toFixed(1);
  }
  /* 累積の ※ の説明。partial →「YYYY-MM-DD 開始(観測 N 日分)」、欠測あり →「欠測 N 回は加えず、観測できた N 日分で年率換算」。
     両方なら「。」でつなぐ。どちらも無ければ null。withPartial = false なら欠測の説明だけ(7日 / 30日年率の列) */
  function cumNote(c, withPartial = true) {
    if (!c) return null;
    const parts = [];
    if (withPartial && c.partial) parts.push(`${c.start || "?"} 開始(観測 ${fmtDays(c.days)} 日分)`);
    if (c.n_missing > 0) parts.push(`欠測 ${c.n_missing} 回は加えず、観測できた ${fmtDays(c.days)} 日分で年率換算`);
    return parts.length ? parts.join("。") : null;
  }
  /* monthly.json の 1 銘柄分 {"YYYY-MM": 合計(小数)} → Map(月初 ms → 合計 %)(monthlyCum と同じ形) */
  function monthlyFromSummary(obj) {
    const m = new Map();
    for (const [k, v] of Object.entries(obj || {})) {
      const y = Number(k.slice(0, 4)), mo = Number(k.slice(5, 7));
      if (Number.isFinite(y) && Number.isFinite(mo) && typeof v === "number") m.set(Date.UTC(y, mo - 1, 1), v * 100);
    }
    return m;
  }
  /* 月ごとの累積 FR(%)。Map(月初 ms → 合計)。欠測は加えない */
  function monthlyCum(pays) {
    const m = new Map();
    for (const p of pays) { if (p.r == null) continue; const k = monthStart(p.t); m.set(k, (m.get(k) || 0) + p.r * 100); }
    return m;
  }

  return {HOUR, DAY, iso, parseTs, fmtTs, dayStart, weekStart, monthStart, nextBucket, bucketOf, STALE_HOURS, staleWarning, anomalyWarning,
          pct, cls, esc, big, CATEGORIES, CATEGORY_LABEL, categoryLabel, intervalLabel, shortTicker, annualize,
          PRESETS, presetLabel, presetDays, inWindow, windowPays, parsePayments, cumStat, aggPayments, summarizePayments,
          expandCum, fmtDays, cumNote, monthlyCum, monthlyFromSummary};
});
