"use strict";
/* Variational Funding Rate Monitor — 画面。
   起動時に data/summary.json と data/swaps.json を、銘柄を選んだとき(とヒートマップの OI 上位分)に data/tickers/<T>.json を読む。
   パスはすべて相対(GitHub Pages のプロジェクトパス配下でも動く)。純粋関数は lib.js(window.VFM)。時刻はすべて UTC。 */
const {HOUR, DAY, iso, parseTs, fmtTs, staleWarning, anomalyWarning, pct, cls, esc, big, CATEGORIES, categoryLabel, intervalLabel, shortTicker,
       PRESETS, presetLabel, windowRange, parsePayments, aggPayments, summarizePayments, monthlyCum, monthlyFromSummary} = VFM;
const UP = "#34d399", DOWN = "#fb7185", CUM = "#93c5fd", GRID = "#1e293b";
const STOP_DAYS = 2;            // 最終観測がこれより古い銘柄に「停止?」
const HEAT_MAX = 60, HEAT_MONTHS = 18;

// ───────── チャートの共通設定 ─────────
const LAYOUT = {paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)", font: {color: "#cbd5e1", size: 12, family: "Inter, 'Noto Sans JP', sans-serif"},
                margin: {l: 10, r: 10, t: 56, b: 10}, hovermode: "x unified",
                legend: {orientation: "h", y: 1.0, yanchor: "bottom", x: 1, xanchor: "right", bgcolor: "rgba(0,0,0,0)"},
                hoverlabel: {bgcolor: "#111a2e", bordercolor: "#334155", font: {color: "#e2e8f0"}}};
const AXIS = {gridcolor: GRID, zerolinecolor: "#475569", linecolor: GRID, automargin: true};
const CONFIG = {responsive: true, displaylogo: false, modeBarButtonsToRemove: ["lasso2d", "select2d"]};
const title = text => ({text, font: {size: 15}, x: 0, xanchor: "left", y: 0.97, yanchor: "top"});
const xs = series => series.map(p => iso(p.x, true));

// ───────── 状態(ブラウザに記憶。読めなくても既定値で動く) ─────────
const state = {ticker: null, preset: "90", freq: "D", kind: "all", sort: "oi", desc: true, minOi: 0, minVol: 0, q: "", swSort: "oi", swDesc: true};
try { Object.assign(state, JSON.parse(localStorage.getItem("vfm-state") || "{}")); } catch (e) {}
if (!PRESETS.some(p => p[1] === state.preset)) state.preset = "90";
if (!["raw", "D", "W", "M"].includes(state.freq)) state.freq = "D";
function save() { try { localStorage.setItem("vfm-state", JSON.stringify(state)); } catch (e) {} }

// ───────── データ ─────────
let SUMMARY = null, SWAPS = null, ROWS = [];
let MONTHLY = null;   // data/monthly.json(月ごとの累積。無ければ銘柄別ファイルから計算)
let END_MS = Date.now();        // 期間の終わり(summary.generated)
let REF_MS = Date.now();        // 「停止?」の基準(summary.last_success)
const BY_TICKER = new Map();
const DETAIL = new Map();       // ticker → Promise<{ticker, interval, payments}>。失敗したものは残さない
const oiOf = r => r.oi != null ? r.oi : (r.long_oi || 0) + (r.short_oi || 0);
const isStopped = r => { const t = parseTs(r.last_obs); return Number.isFinite(t) && REF_MS - t > STOP_DAYS * DAY; };
const num = v => { if (v == null || v === "") return null; const n = Number(v); return Number.isFinite(n) ? n : null; };

async function fetchJson(path) {
  const res = await fetch(path, {cache: "no-cache"});
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}
function getDetail(ticker) {
  if (!DETAIL.has(ticker)) {
    const p = fetchJson(`data/tickers/${encodeURIComponent(ticker)}.json`).then(d => ({ticker, interval: num(d && d.interval), payments: parsePayments(d)}));
    p.catch(() => DETAIL.delete(ticker));
    DETAIL.set(ticker, p);
  }
  return DETAIL.get(ticker);
}

// ───────── 共通の部品 ─────────
const $ = id => document.getElementById(id);
function notice(id, msg, kind) {
  /* 最上部の警告。msg が空なら消す */
  let el = $(id);
  if (!msg) { if (el) el.remove(); return; }
  if (!el) { el = document.createElement("div"); el.id = id; $("alerts").appendChild(el); }
  el.className = "warnbox" + (kind === "err" ? " err" : ""); el.textContent = msg;
}
function seg(id, items, current, onpick) {
  const el = $(id); el.innerHTML = "";
  for (const [label, key] of items) {
    const b = document.createElement("button"); b.type = "button"; b.textContent = label;
    b.setAttribute("aria-pressed", current === key);
    b.addEventListener("click", () => onpick(key));
    el.appendChild(b);
  }
}
const hasPlotly = () => typeof Plotly !== "undefined";
function showEmpty(el, msg, warn) {
  if (hasPlotly() && el._fullLayout) Plotly.purge(el);
  el.innerHTML = msg ? `<div class="empty${warn ? " warn" : ""}">${msg}</div>` : "";
}
function plot(el, traces, layout) {
  if (!hasPlotly()) { el.innerHTML = "<div class='empty warn'>チャートのライブラリ(Plotly)を読み込めませんでした。</div>"; return; }
  if (el.querySelector(".empty")) el.innerHTML = "";
  Plotly.react(el, traces, layout, CONFIG);
}
const DASH = "<span class='mute'>—</span>";
const mute = s => `<span class="mute">${s}</span>`;
const sgn = (v, d = 2) => (v == null || Number.isNaN(v)) ? DASH : `<span class="${cls(v)}">${pct(v, d)}</span>`;
const sgnPt = (v, d = 1) => (v == null || Number.isNaN(v)) ? DASH : `<span class="${cls(v)}">${(v >= 0 ? "+" : "") + v.toFixed(d)}pt</span>`;
const usdBig = v => (v == null || !Number.isFinite(v)) ? DASH : "$" + big(v);
const usd = v => (v == null || !Number.isFinite(v)) ? DASH : "$" + v.toLocaleString(undefined, {maximumFractionDigits: v >= 100 ? 2 : 4});
const share = v => (v == null || !Number.isFinite(v)) ? DASH : (v * 100).toFixed(0) + "%";
const tsShort = sec => sec == null ? DASH : mute(iso(sec * 1000, true).slice(5));
const partialMark = (start, days) => `<span class="tip mute" data-tip="${esc(start)} 開始(${Math.round(days)} 日分)">※</span>`;
const EST_TAG = "<span class='tag est'>推定</span>";
const CUR_TAG = `<span class="tag tip" data-tip="進行中の窓の見込み値(次回支払で確定)">進行中</span>`;
/* 直近FR: 最後に記録した支払い(last_rate)。収集開始後まだ支払時刻を越えていない銘柄は、進行中の窓の値(cur_rate)を「進行中」の印付きで出す */
function lastFr(r) {
  if (r.last_rate != null) return `${sgn(r.last_rate * 100, 4)} / ${sgn(r.last_apr == null ? null : r.last_apr * 100, 1)}${r.last_status === "estimated" ? EST_TAG : ""}`;
  if (r.cur_rate != null) return `${sgn(r.cur_rate * 100, 4)} / ${sgn(r.cur_apr == null ? null : r.cur_apr * 100, 1)}${CUR_TAG}`;
  return DASH;
}
/* summary の cum[k]({sum, apr, partial, start, days})→ 「累積 / 年率※」 */
const winFmt = v => (v == null || v.sum == null) ? DASH : `${sgn(v.sum * 100)} / ${sgn(v.apr == null ? null : v.apr * 100, 1)}${v.partial ? partialMark(v.start || "?", v.days || 0) : ""}`;

// ───────── ヒーローと警告 ─────────
function renderHero() {
  const s = SUMMARY;
  const mode = s.funding_mode === "confirmed" ? "確定値" : "推定値(窓の直前の値)";
  $("badges").innerHTML = [
    `最終更新 ${fmtTs(s.last_success)} UTC`, `収集開始 ${fmtTs(s.collection_start, false)}`, `銘柄数 ${num(s.n_tickers) ?? ROWS.length}`,
    `欠測 ${num(s.missing_total) == null ? "—" : num(s.missing_total).toLocaleString()} 件`, `矛盾 ${s.anomalies_today ?? "—"} / ${s.anomalies_checked_today ?? "—"}`,
    `方式 ${mode}`, `HL 取得 ${fmtTs(s.hl_fetched)} UTC`,
  ].map(b => `<span class="badge">${esc(b)}</span>`).join("");
  checkStale();
  notice("al-anom", anomalyWarning(s));
}
function checkStale() { if (SUMMARY) notice("al-stale", staleWarning(SUMMARY.last_success, Date.now())); }

// ───────── 一覧の列: [見出し, キー, 値を取る関数, 表示の関数, 左寄せなら "l"] ─────────
const PERP_COLS = [
  ["銘柄", "ticker", r => shortTicker(r.ticker), (v, r) => `<span title="${esc(r.ticker)}">${esc(v)}</span>${isStopped(r) ? "<span class='tag stop'>停止?</span>" : ""}`, "l"],
  ["名前", "name", r => r.name || "", v => mute(esc(v)), "l"],
  ["種類", "category", r => r.category || "other", v => esc(categoryLabel(v)), "l"],
  ["支払間隔", "interval", r => r.interval, v => intervalLabel(v)],
  ["次回支払(UTC)", "next_ts", r => r.next_ts, v => tsShort(v)],
  ["データ開始", "data_start", r => r.data_start || "", v => v ? mute(esc(v)) : DASH],
  ["直近FR(1回分 / 年率)", "last_rate", r => r.last_rate != null ? r.last_rate : r.cur_rate, (v, r) => lastFr(r)],
  ["7日年率", "apr7", r => r.apr7, v => v == null ? DASH : sgn(v * 100, 1)],
  ["30日年率", "apr30", r => r.apr30, v => v == null ? DASH : sgn(v * 100, 1)],
  [() => `${presetLabel(state.preset)}の累積FR / 年率`, "cum", r => (r.cum && r.cum[state.preset]) || null, v => winFmt(v)],
  ["HL 7日年率", "hl_apr7", r => r.hl_apr7, (v, r) => v == null ? DASH : `<span class="tip" data-tip="HL ${esc(r.hl_coin || "?")}">${sgn(v * 100, 1)}</span>`],
  ["差(V−HL)", "diff_pp", r => r.diff_pp, v => sgnPt(v, 1)],
  ["OI", "oi", r => oiOf(r), v => usdBig(v)],
  ["ロング比率", "long_share", r => r.long_share, v => share(v)],
  ["24h出来高", "vol24", r => r.vol24, v => usdBig(v)],
  ["スプレッド", "spread_bps", r => r.spread_bps, v => v == null ? DASH : v.toFixed(1) + "bp"],
];
const TEXT_KEYS = ["ticker", "name", "category", "data_start"];   // 文字の列は最初のクリックで昇順
const colHead = h => typeof h === "function" ? h() : h;

function filteredRows() {
  /* 種類、OI・24h 出来高の下限(百万ドル)、ティッカーか名前の部分一致で絞る */
  const q = String(state.q || "").trim().toLowerCase();
  return ROWS.filter(r => (state.kind === "all" || (r.category || "other") === state.kind)
    && oiOf(r) >= (state.minOi || 0) * 1e6 && (r.vol24 || 0) >= (state.minVol || 0) * 1e6
    && (!q || String(r.ticker).toLowerCase().includes(q) || String(r.name || "").toLowerCase().includes(q)));
}
function sortRows(rows, cols, sortKey, desc) {
  /* 累積の列は {sum, apr} なので累積で並べる。null は向きに関係なく最後 */
  const col = cols.find(c => c[1] === sortKey) || cols[0], f = col[2], dir = desc ? -1 : 1;
  const key = r => { const v = f(r); return (v != null && typeof v === "object") ? v.sum : v; };
  return rows.map(r => [key(r), r]).sort((a, b) => {
    const x = a[0], y = b[0], xn = x == null || x === "", yn = y == null || y === "";
    if (xn || yn) return xn && yn ? 0 : xn ? 1 : -1;
    return (typeof x === "string" ? x.localeCompare(y) : x - y) * dir;
  }).map(p => p[1]);
}
function buildTable(tbl, cols, rows, sortKey, desc, rowAttr) {
  let html = "<thead><tr>" + cols.map(([h, key, , , al]) => `<th data-key="${key}" class="${al || ""}" style="cursor:pointer">${esc(colHead(h))}${key === sortKey ? (desc ? " ▼" : " ▲") : ""}</th>`).join("") + "</tr></thead><tbody>";
  for (const r of rows) html += `<tr ${rowAttr(r)}>` + cols.map(([, , f, fmt, al]) => `<td class="${al || ""}">${fmt(f(r), r)}</td>`).join("") + "</tr>";
  tbl.innerHTML = html + "</tbody>";
}
function renderTable() {
  const rows = sortRows(filteredRows(), PERP_COLS, state.sort, state.desc);
  $("count").textContent = `${rows.length} / ${ROWS.length} 銘柄`;
  const tbl = $("perpTable");
  buildTable(tbl, PERP_COLS, rows, state.sort, state.desc, r => `data-ticker="${esc(r.ticker)}" aria-selected="${r.ticker === state.ticker}"`);
  tbl.querySelectorAll("tbody tr").forEach(tr => tr.addEventListener("click", () => { selectTicker(tr.dataset.ticker); $("kpis").scrollIntoView({behavior: "smooth", block: "start"}); }));
  tbl.querySelectorAll("th").forEach(th => th.addEventListener("click", () => {
    if (state.sort === th.dataset.key) state.desc = !state.desc; else { state.sort = th.dataset.key; state.desc = !TEXT_KEYS.includes(th.dataset.key); }
    save(); renderTable();
  }));
}
function selectTicker(t) {
  if (!BY_TICKER.has(t)) return;
  state.ticker = t; save();
  $("perpTable").querySelectorAll("tbody tr").forEach(tr => tr.setAttribute("aria-selected", tr.dataset.ticker === t));
  $("tickerSel").value = t;
  renderDetail();
}

// ───────── 操作バー ─────────
function renderControls() {
  seg("kindSeg", [["すべて", "all"], ...CATEGORIES.map(c => [categoryLabel(c), c])], state.kind, k => { state.kind = k; save(); onFilter(); });
  for (const key of ["minOi", "minVol"]) {
    const el = $(key); el.value = String(state[key] || 0);
    el.onchange = () => { const v = Number(el.value); state[key] = v >= 0 ? v : 0; save(); onFilter(); };
  }
  const q = $("q"); q.value = state.q || "";
  let timer = 0;
  q.oninput = () => { clearTimeout(timer); timer = setTimeout(() => { state.q = q.value; save(); onFilter(); }, 200); };
  renderPeriodSegs();
  const sel = $("tickerSel");
  sel.innerHTML = [...ROWS].sort((a, b) => a.ticker.localeCompare(b.ticker))
    .map(r => `<option value="${esc(r.ticker)}">${esc(shortTicker(r.ticker))}  ${esc(r.name || "")}(${esc(categoryLabel(r.category))})</option>`).join("");
  sel.value = state.ticker; sel.onchange = () => selectTicker(sel.value);
  const fq = $("freq"); fq.value = state.freq; fq.onchange = () => { state.freq = fq.value; save(); renderDetail(); };
}
function renderPeriodSegs() {
  /* 期間は表の列と銘柄の詳細で共通。2 か所にある切り替えを同じ状態に結ぶ */
  for (const id of ["periodSegTable", "periodSeg"]) seg(id, PRESETS.map(([l, k]) => [l, k]), state.preset, k => { state.preset = k; save(); renderPeriodSegs(); renderTable(); renderDetail(); });
}
function onFilter() { renderTable(); renderHeat(); }

// ───────── 銘柄の詳細(指標カード + チャート) ─────────
let detailReq = 0;
async function renderDetail() {
  const kp = $("kpis"), ch = $("frChart"), row = BY_TICKER.get(state.ticker);
  if (!row) { kp.innerHTML = "<div class='empty'>銘柄を選んでください。</div>"; showEmpty(ch, ""); return; }
  const req = ++detailReq;
  const label = `${esc(shortTicker(row.ticker))}${row.name ? " " + esc(row.name) : ""}`;
  if (!DETAIL.has(row.ticker)) kp.innerHTML = `<div class="empty">${label} を読み込み中…</div>`;
  let d;
  try { d = await getDetail(row.ticker); }
  catch (e) {
    if (req !== detailReq) return;
    kp.innerHTML = `<div class="empty warn">${label} の詳細データ(data/tickers/${esc(row.ticker)}.json)を読み込めませんでした: ${esc(e.message)}</div>`;
    showEmpty(ch, ""); return;
  }
  if (req !== detailReq) return;
  const interval = d.interval || row.interval;
  const first = d.payments.length ? d.payments[0].t : null;
  const [start, end] = windowRange(state.preset, END_MS, first);
  const pays = d.payments.filter(p => p.t >= start && p.t <= end);
  if (!pays.length) { kp.innerHTML = `<div class="empty">${label}: この期間(${esc(presetLabel(state.preset))})のデータがありません。</div>`; showEmpty(ch, ""); return; }
  const firstPaid = d.payments.find(p => p.r != null);
  const s = summarizePayments(pays, interval, start, end, firstPaid ? firstPaid.t : null);
  const card = (n, v, s2, c = "") => `<div class="kpi"><div class="n">${n}</div><div class="v big ${c}">${v}</div><div class="s">${s2}</div></div>`;
  const perYear = interval > 0 ? 365 * 86400 / interval : NaN;
  kp.innerHTML = [
    card(`${label} 累積FR(${esc(presetLabel(state.preset))}: ${iso(Math.max(start, s.start))} 〜)`,
         `<span class="${cls(s.total)}">${pct(s.total)}</span>`,
         `年率換算 <b class="${cls(s.apr ?? 0)}">${pct(s.apr, 1)}</b>${s.partial ? partialMark(iso(s.start), s.days) : ""}・${s.count.toLocaleString()} 回(${intervalLabel(interval)} ごと)`
         + `${s.nEst ? `・推定 ${s.nEst} 回` : ""}・欠測 <span class="${s.missing ? "warn" : ""}">${s.missing} 回</span><br>`
         + `内訳 <span class="pos">+${s.pos.toFixed(2)}%</span> / <span class="neg">-${Math.abs(s.neg).toFixed(2)}%</span>・マイナス回 ${Number.isNaN(s.neg_share) ? "—" : s.neg_share.toFixed(0) + "%"}`),
    card(`FR の幅(1 回分、${intervalLabel(interval)})`, `${pct(s.max, 4)}`,
         `最大 ${pct(s.max, 4)}(年率 ${pct(s.maxApr, 0)})・最小 ${pct(s.min, 4)}(年率 ${pct(s.minApr, 0)})<br>1 回分の平均 ${pct(s.mean, 4)}(年率 ${pct(s.mean == null ? null : s.mean * perYear, 1)})`),
    card("現在の状態(進行中の窓の値)",
         `${row.cur_rate == null ? "—" : pct(row.cur_rate * 100, 4)}<span class="mute" style="font-size:14px"> /${intervalLabel(row.interval)}</span>${row.cur_rate != null ? CUR_TAG : ""}`,
         `年率換算 <b class="${cls(row.cur_apr ?? 0)}">${row.cur_apr == null ? "—" : pct(row.cur_apr * 100, 1)}</b>・次回支払 ${row.next_ts == null ? "—" : iso(row.next_ts * 1000, true)} UTC`
         + `${isStopped(row) ? " <span class='tag stop'>停止?</span>" : ""}<br>直近の支払い ${row.last_rate == null ? "まだなし" : `<b class="${cls(row.last_rate)}">${pct(row.last_rate * 100, 4)}</b>(年率 ${pct(row.last_apr == null ? null : row.last_apr * 100, 1)}${row.last_status === "estimated" ? "、推定" : ""}${row.last_pay_ts == null ? "" : "、" + iso(row.last_pay_ts * 1000, true)})`}`
         + `<br>OI ${usdBig(oiOf(row))}(ロング ${share(row.long_share)})・24h 出来高 ${usdBig(row.vol24)}・スプレッド ${row.spread_bps == null ? "—" : row.spread_bps.toFixed(1) + "bp"}・mark ${usd(row.mark)}`),
  ].join("");
  frChart(ch, row, pays, state.freq, s);
}
function frChart(el, row, pays, f, s) {
  /* 棒: 1 回(または日・週・月の合計)の FR。推定を含む棒は薄く別系列。欠測は棒を描かない(0 にしない)。線: 累積 FR(右軸) */
  const a = aggPayments(pays, f);
  let c = 0; const cum = a.map(p => (c += p.y || 0)), x = xs(a);
  const color = p => (p.y ?? 0) >= 0 ? UP : DOWN;
  const yConf = a.map(p => (p.y == null || p.est) ? null : p.y), yEst = a.map(p => (p.y != null && p.est) ? p.y : null);
  const traces = [];   // 値のない系列は出さない(見込み値型なら「FR」が無く、確定値型なら「FR(推定)」が無い)
  if (yConf.some(v => v != null)) traces.push({type: "bar", x, y: yConf, name: "FR", marker: {color: a.map(color), line: {width: 0}}, hovertemplate: "FR %{y:.4f}%<extra></extra>"});
  if (yEst.some(v => v != null)) traces.push({type: "bar", x, y: yEst, name: "FR(推定)", marker: {color: a.map(color), opacity: 0.55, line: {width: 0}}, hovertemplate: "FR(推定) %{y:.4f}%<extra></extra>"});
  traces.push({type: "scatter", mode: "lines", x, y: cum, name: "累積FR", yaxis: "y2", line: {color: CUM, width: 2}, hovertemplate: "累積 %{y:.3f}%<extra></extra>"});
  const unit = {raw: `1 回(${intervalLabel(row.interval)})`, D: "日", W: "週", M: "月"}[f] || f;
  plot(el, traces, {...LAYOUT, barmode: "overlay", bargap: 0.2,
    title: title(`<b>${esc(shortTicker(row.ticker))}</b> ${esc(row.name || "")}  累積 ${pct(s.total)}  <span style="color:${UP}">+${s.pos.toFixed(2)}%</span> / <span style="color:${DOWN}">-${Math.abs(s.neg).toFixed(2)}%</span>`
                 + (s.missing ? `  <span style="color:#fbbf24">欠測 ${s.missing} 回(空白)</span>` : "")),
    xaxis: {...AXIS, title: {text: `${unit} ごとの FR(UTC)`}},
    yaxis: {title: {text: "FR(%)"}, zeroline: true, ...AXIS},
    yaxis2: {title: {text: "累積FR(%)"}, overlaying: "y", side: "right", showgrid: false, zeroline: false, automargin: true}});
}

// ───────── 月ごとの累積FR(ヒートマップ)。絞り込み後の OI 上位 60 銘柄の詳細を並行して読む ─────────
let heatReq = 0;
async function renderHeat() {
  const el = $("heat"), note = $("heatNote");
  const all = filteredRows(), list = [...all].sort((a, b) => oiOf(b) - oiOf(a)).slice(0, HEAT_MAX);
  note.textContent = `絞り込み後の銘柄のうち OI 上位 ${list.length} 銘柄${all.length > list.length ? `(${all.length} 銘柄中。上限 ${HEAT_MAX})` : ""}。値は月ごとの累積 FR(%)、直近 ${HEAT_MONTHS} か月。欠測は加えない。`;
  if (!list.length) { showEmpty(el, "表示する銘柄がありません。"); return; }
  const req = ++heatReq;
  const rows = [], pn = [], per = new Set(); let failed = 0;
  if (MONTHLY && MONTHLY.rows) {
    // 月ごとの合計は build が data/monthly.json にまとめている(銘柄別ファイルを 60 個読まない)
    for (const r of list) {
      const m = monthlyFromSummary(MONTHLY.rows[r.ticker]);
      if (!m.size) continue;
      rows.push(r); pn.push(m);
      for (const k of m.keys()) per.add(k);
    }
    if (!rows.length) { showEmpty(el, "まだ月ごとの累積がありません(最初の支払が記録されると出ます)。"); return; }
  } else {
    if (!list.every(r => DETAIL.has(r.ticker))) { el.style.height = ""; showEmpty(el, "読み込み中…"); }
    const res = await Promise.all(list.map(r => getDetail(r.ticker).then(d => d, () => null)));
    if (req !== heatReq) return;
    list.forEach((r, i) => {
      if (!res[i]) { failed++; return; }
      const m = monthlyCum(res[i].payments); rows.push(r); pn.push(m);
      for (const k of m.keys()) per.add(k);
    });
    if (failed) note.textContent += ` ${failed} 銘柄は詳細データ(data/tickers/)を読み込めなかったので省いた。`;
  }
  if (!rows.length) { showEmpty(el, "詳細データを読み込めませんでした。", true); return; }
  const cols = [...per].sort((a, b) => a - b).slice(-HEAT_MONTHS);
  const z = pn.map(m => cols.map(k => m.has(k) ? m.get(k) : null));
  const lim = Math.max(0.01, ...z.flat().filter(v => v != null).map(Math.abs));
  el.style.height = (170 + 28 * rows.length) + "px";
  plot(el, [{type: "heatmap", z, x: cols.map(k => iso(k).slice(0, 7)), y: rows.map(r => shortTicker(r.ticker)), zmid: 0, zmin: -lim, zmax: lim,
    colorscale: [[0, "#be123c"], [0.5, "#0f172a"], [1, "#059669"]], text: z.map(r => r.map(v => v == null ? "" : pct(v))), texttemplate: "%{text}",
    textfont: {size: 11}, hovertemplate: "%{y} %{x}<br>累積FR %{z:.3f}%<extra></extra>", colorbar: {title: {text: "累積FR(%)"}, thickness: 12}}],
    {...LAYOUT, hovermode: "closest", margin: {l: 110, r: 10, t: 56, b: 70}, title: title(`<b>月ごとの累積FR</b>(%。直近 ${HEAT_MONTHS} か月、OI 上位 ${rows.length} 銘柄)`),
     xaxis: {type: "category", tickangle: -45, ...AXIS}, yaxis: {type: "category", autorange: "reversed", ...AXIS}});
}

// ───────── swap ─────────
const pair = (o, a, b, d) => (!o || (o[a] == null && o[b] == null)) ? DASH : `${sgn(o[a] == null ? null : o[a] * 100, d)} / ${sgn(o[b] == null ? null : o[b] * 100, d)}`;
const SWAP_COLS = [
  ["銘柄", "ticker", r => shortTicker(r.ticker), (v, r) => `<span title="${esc(r.ticker)}">${esc(v)}</span>`, "l"],
  ["名前", "name", r => r.name || "", v => mute(esc(v)), "l"],
  ["直近の年率(ロング / ショート)", "ann_long", r => r.last ? r.last.ann_long : null, (v, r) => pair(r.last, "ann_long", "ann_short", 1)],
  ["1回分(ロング / ショート)", "int_long", r => r.last ? r.last.int_long : null, (v, r) => pair(r.last, "int_long", "int_short", 4)],
  ["次回の年率(ロング / ショート)", "next_ann", r => r.next ? r.next.ann_long : null, (v, r) => pair(r.next, "ann_long", "ann_short", 1)],
  ["次回支払(UTC)", "next_ts", r => r.next_ts, v => tsShort(v)],
  ["OI", "oi", r => oiOf(r), v => usdBig(v)],
  ["ロング比率", "long_share", r => { const t = (r.long_oi || 0) + (r.short_oi || 0); return t > 0 ? (r.long_oi || 0) / t : null; }, v => share(v)],
  ["24h出来高", "vol24", r => r.vol24, v => usdBig(v)],
  ["価格", "mark", r => r.mark, v => usd(v)],
];
function normalizeSwaps(j) {
  /* 数値が文字列で来ても動くように揃える */
  const fix = o => o ? Object.fromEntries(Object.entries(o).map(([k, v]) => [k, num(v)])) : null;
  const snap = j.snap_ts || ((j.rows || []).map(r => r && r.snap_ts).find(Boolean)) || null;
  return {generated: j.generated, snap_ts: snap, rows: (j.rows || []).filter(r => r && typeof r.ticker === "string").map(r => ({...r,
    mark: num(r.mark), long_oi: num(r.long_oi), short_oi: num(r.short_oi), vol24: num(r.vol24), next_ts: num(r.next_ts), last: fix(r.last), next: fix(r.next)}))};
}
function renderSwap() {
  const rows = sortRows(SWAPS.rows, SWAP_COLS, state.swSort, state.swDesc);
  $("swapBadges").innerHTML = [`取得 ${fmtTs(SWAPS.snap_ts || SWAPS.generated)} UTC`, `生成 ${fmtTs(SWAPS.generated)} UTC`, `銘柄数 ${SWAPS.rows.length}`].map(b => `<span class="badge">${esc(b)}</span>`).join("");
  $("swapCount").textContent = `${rows.length} 銘柄(現在値のみ)`;
  const tbl = $("swapTable");
  buildTable(tbl, SWAP_COLS, rows, state.swSort, state.swDesc, () => "");
  tbl.querySelectorAll("th").forEach(th => th.addEventListener("click", () => {
    if (state.swSort === th.dataset.key) state.swDesc = !state.swDesc; else { state.swSort = th.dataset.key; state.swDesc = !["ticker", "name"].includes(th.dataset.key); }
    save(); renderSwap();
  }));
}

// ───────── 上部タブ(#swap で swap タブを開く) ─────────
function showTab(t) {
  for (const b of document.querySelectorAll(".toptabs button")) b.setAttribute("aria-selected", b.dataset.tab === t);
  $("tab-perp").hidden = t !== "perp"; $("tab-swap").hidden = t !== "swap";
  try { history.replaceState(null, "", t === "swap" ? "#swap" : location.pathname + location.search); } catch (e) {}
  if (t === "perp" && hasPlotly()) for (const id of ["frChart", "heat"]) { const el = $(id); if (el._fullLayout) Plotly.Plots.resize(el); }
}
document.querySelectorAll(".toptabs button").forEach(b => b.addEventListener("click", () => showTab(b.dataset.tab)));
window.addEventListener("hashchange", () => showTab(location.hash === "#swap" ? "swap" : "perp"));

// ───────── 起動 ─────────
async function main() {
  showTab(location.hash === "#swap" ? "swap" : "perp");
  if (!hasPlotly()) notice("al-plotly", "チャートのライブラリ(Plotly)を読み込めませんでした。表は表示できますがチャートは出ません。", "err");
  const [sum, sw, mo] = await Promise.allSettled([fetchJson("data/summary.json"), fetchJson("data/swaps.json"), fetchJson("data/monthly.json")]);
  if (mo.status === "fulfilled" && mo.value && mo.value.rows) MONTHLY = mo.value;
  if (sum.status === "fulfilled" && sum.value && Array.isArray(sum.value.rows)) {
    SUMMARY = sum.value;
    ROWS = SUMMARY.rows.filter(r => r && typeof r.ticker === "string");
    // cum は [sum, apr, partial(0/1), start, days] の配列で来る(summary.json を小さくするため)。ここで展開する
    for (const r of ROWS) {
      if (r.cum) for (const k of Object.keys(r.cum)) {
        const c = r.cum[k];
        if (Array.isArray(c)) r.cum[k] = {sum: c[0], apr: c[1], partial: !!c[2], start: c[3], days: c[4]};
      }
    }
    for (const r of ROWS) BY_TICKER.set(r.ticker, r);
    const g = parseTs(SUMMARY.generated), ls = parseTs(SUMMARY.last_success);
    END_MS = Number.isFinite(g) ? g : Date.now();
    REF_MS = Number.isFinite(ls) ? ls : END_MS;
    if (!BY_TICKER.has(state.ticker)) state.ticker = ROWS.length ? sortRows(ROWS, PERP_COLS, "oi", true)[0].ticker : null;
    renderHero(); renderControls(); renderTable(); renderDetail(); renderHeat();
    setInterval(checkStale, 60000);
  } else {
    const why = sum.status === "rejected" ? (sum.reason && sum.reason.message) || String(sum.reason) : "rows がありません";
    notice("al-summary", `data/summary.json を読み込めませんでした(${why})。一覧・チャートは表示できません。`, "err");
    $("count").textContent = "—";
    $("kpis").innerHTML = "<div class='empty warn'>summary.json を読み込めませんでした。</div>";
    showEmpty($("heat"), "summary.json を読み込めませんでした。", true);
  }
  if (sw.status === "fulfilled" && sw.value && Array.isArray(sw.value.rows)) { SWAPS = normalizeSwaps(sw.value); renderSwap(); }
  else {
    const why = sw.status === "rejected" ? (sw.reason && sw.reason.message) || String(sw.reason) : "rows がありません";
    notice("al-swaps", `data/swaps.json を読み込めませんでした(${why})。swap の一覧は表示できません。`, "err");
    $("swapCount").textContent = "swaps.json を読み込めませんでした。";
  }
}
main().catch(e => notice("al-fatal", `ページの初期化に失敗しました: ${e && e.message ? e.message : e}`, "err"));
