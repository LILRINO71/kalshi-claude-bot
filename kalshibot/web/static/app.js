"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const api = async (path, opts = {}) => {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
};
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (v, sign = false) => v == null ? "—" : (sign && v > 0 ? "+" : v < 0 ? "−" : "") + "$" + Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const pct = (v, d = 1) => v == null ? "—" : (v * 100).toFixed(d) + "%";
const num = (v, d = 3) => v == null ? "—" : Number(v).toFixed(d);
const int = (v) => v == null ? "—" : Math.round(v).toLocaleString();
const cls = (v) => v > 0 ? "pos" : v < 0 ? "neg" : "";
const when = (ts) => new Date(ts * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
const clock = (ts) => new Date(ts * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });

function toast(msg, ms = 3200) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), ms);
}

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const SERIES = () => [css("--series-1"), css("--series-2"), css("--series-3")];

let META = null, SETTINGS = null;

// ------------------------------------------------------------------ tabs

$$(".tab").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
function showTab(name) {
  $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
  try { localStorage.setItem("tab", name); } catch (e) {}
  if (name === "usage") loadUsage();
  if (name === "live") loadLive();
}

// ------------------------------------------------------------------ settings + top bar

async function init() {
  [META, SETTINGS] = await Promise.all([api("/api/meta"), api("/api/settings")]);
  fillSelect($("#q-strategy"), META.strategies, SETTINGS.strategy);
  fillSelect($("#q-model"), META.models, SETTINGS.model);
  const eff = $("#q-effort");
  eff.innerHTML = META.efforts.map((e) => `<button data-v="${e}">${e}</button>`).join("");
  $$("button", eff).forEach((b) => b.addEventListener("click", () => saveQuick({ effort: b.dataset.v })));
  $("#q-strategy").addEventListener("change", (e) => saveQuick({ strategy: e.target.value }));
  $("#q-model").addEventListener("change", (e) => saveQuick({ model: e.target.value }));
  fillSelect($("#s-sizing"), META.sizing, SETTINGS.sizing);
  $("#s-sizing").addEventListener("change", syncSizing);
  $("#s-cli").textContent = META.claude_path || "not found (install Claude Code or set CLAUDE_BIN)";
  renderChips($("#bt-assets"), SETTINGS.assets);
  renderChips($("#s-assets"), SETTINGS.assets);
  $("#bt-delay").value = SETTINGS.time_delay;
  $("#bt-idx-delay").value = SETTINGS.index_time_delay;
  $("#bt-end").value = new Date().toISOString().slice(0, 10);
  $("#bt-start").value = "2026-07-19";
  syncTop();
  fillSettingsForm();
  ["#bt-start", "#bt-end", "#bt-count", "#bt-sample", "#bt-seed", "#bt-delay", "#bt-idx-delay"].forEach((s) => $(s).addEventListener("input", estimateSoon));
  let tab = "backtest";
  try { tab = localStorage.getItem("tab") || tab; } catch (e) {}
  showTab(tab);
  loadRuns();
  estimateSoon();
  refreshPills();
  setInterval(refreshPills, 5000);
}

function fillSelect(sel, options, value) {
  sel.innerHTML = Object.entries(options).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
  sel.value = value;
}

function renderChips(box, selected) {
  box.innerHTML = META.assets.map((a, i) => {
    const gap = a === META.indices[0] ? `<span class="chip-sep">Stocks</span>` : i === 0 ? `<span class="chip-sep">Crypto</span>` : "";
    return `${gap}<button type="button" class="chip ${selected.includes(a) ? "on" : ""}" data-a="${a}">${esc(META.labels[a])}</button>`;
  }).join("");
  $$(".chip", box).forEach((c) => c.addEventListener("click", () => {
    c.classList.toggle("on");
    if (!$$(".chip.on", box).length) c.classList.add("on");
    if (box.id === "bt-assets") estimateSoon();
  }));
}
const chipValues = (box) => $$(".chip.on", box).map((c) => c.dataset.a);

function syncTop() {
  $("#q-strategy").value = SETTINGS.strategy;
  $("#q-model").value = SETTINGS.model;
  $$("#q-effort button").forEach((b) => b.classList.toggle("on", b.dataset.v === SETTINGS.effort));
  $$(".claude-only").forEach((el) => el.style.opacity = SETTINGS.strategy === "claude" ? 1 : 0.35);
}

async function saveQuick(patch) {
  try {
    SETTINGS = await api("/api/settings", { method: "POST", body: { ...SETTINGS, ...patch } });
    syncTop();
    estimateSoon();
    const what = patch.effort ? `Effort → ${patch.effort}` : patch.model ? `Model → ${patch.model}` : `Strategy → ${META.strategies[patch.strategy]}`;
    toast(what + " (applies to the next decision)");
  } catch (e) { toast(e.message); }
}

const S_FIELDS = ["bankroll", "fixed_stake", "percent_stake", "kelly_fraction", "max_stake_pct", "max_price", "slippage",
  "max_daily_profit", "max_daily_loss", "time_delay", "index_time_delay", "min_edge", "parallel_calls"];
function fillSettingsForm() {
  S_FIELDS.forEach((k) => $("#s-" + k).value = SETTINGS[k]);
  $("#s-simulate_fees").checked = SETTINGS.simulate_fees;
  $("#s-obey_model").checked = SETTINGS.obey_model;
  $("#s-instructions").value = SETTINGS.instructions;
  $("#s-sizing").value = SETTINGS.sizing;
  syncSizing();
}
function syncSizing() {
  const m = $("#s-sizing").value;
  $$(".sz").forEach((el) => el.classList.toggle("hidden", !el.classList.contains("sz-" + m)));
}
$("#s-save").addEventListener("click", async () => {
  const body = { ...SETTINGS };
  S_FIELDS.forEach((k) => body[k] = parseFloat($("#s-" + k).value));
  body.sizing = $("#s-sizing").value;
  body.simulate_fees = $("#s-simulate_fees").checked;
  body.obey_model = $("#s-obey_model").checked;
  body.instructions = $("#s-instructions").value;
  body.assets = chipValues($("#s-assets"));
  try {
    SETTINGS = await api("/api/settings", { method: "POST", body });
    fillSettingsForm();
    $("#bt-delay").value = SETTINGS.time_delay;
    $("#bt-idx-delay").value = SETTINGS.index_time_delay;
    $("#s-status").textContent = "Saved " + new Date().toLocaleTimeString();
    estimateSoon();
  } catch (e) { $("#s-status").textContent = e.message; }
});

async function refreshPills() {
  try {
    const [live, u] = await Promise.all([api("/api/live"), api("/api/usage")]);
    const p = $("#pill-live");
    p.classList.toggle("on", live.running);
    $("span", p).textContent = live.running ? `Live · ${money(live.equity)}` : "Live off";
    $("#pill-usage").textContent = `${u.today.calls} calls · ${int(u.today.tokens)} tokens today`;
    if ($("#tab-live").classList.contains("active")) renderLive(live);
  } catch (e) {}
}

// ------------------------------------------------------------------ backtest

function btParams() {
  return {
    start: $("#bt-start").value, end: $("#bt-end").value, count: +$("#bt-count").value,
    sample: $("#bt-sample").value, seed: +$("#bt-seed").value, name: $("#bt-name").value,
    assets: chipValues($("#bt-assets")), time_delay: +$("#bt-delay").value, index_time_delay: +$("#bt-idx-delay").value,
    strategy: SETTINGS.strategy, model: SETTINGS.model, effort: SETTINGS.effort,
  };
}

let estTimer = null;
function estimateSoon() {
  clearTimeout(estTimer);
  estTimer = setTimeout(async () => {
    const box = $("#bt-estimate");
    try {
      const e = await api("/api/backtests/estimate", { method: "POST", body: btParams() });
      if (!e.markets) { box.innerHTML = "No settled markets in that range."; return; }
      let html = `<b>${e.markets}</b> markets from ${esc(e.first.slice(0, 10))} to ${esc(e.last.slice(0, 10))}. `;
      if (e.claude_calls_max) {
        html += `Up to <b>${e.claude_calls_max}</b> Claude calls (${SETTINGS.model}/${SETTINGS.effort}), about <b>${e.est_minutes} min</b> and <b>${int(e.est_tokens)}</b> tokens of plan usage. Decisions already cached are free.`;
        if (!e.based_on_samples) html += " <span class='muted'>(Rough guess until this model/effort has been used once.)</span>";
      } else {
        html += "No Claude calls: this strategy is free and instant.";
      }
      box.innerHTML = html;
    } catch (err) { box.textContent = err.message; }
  }, 350);
}

let pollTimer = null;
$("#bt-run").addEventListener("click", async () => {
  try {
    const run = await api("/api/backtests", { method: "POST", body: btParams() });
    toast("Backtest started");
    watchRun(run.id);
  } catch (e) { toast(e.message); }
});
$("#bt-cancel").addEventListener("click", async () => {
  if (watchRun.id) await api(`/api/backtests/${watchRun.id}/cancel`, { method: "POST" });
});

function watchRun(id) {
  watchRun.id = id;
  $("#bt-run").disabled = true;
  $("#bt-cancel").classList.remove("hidden");
  $("#bt-progress").classList.remove("hidden");
  clearInterval(pollTimer);
  const tick = async () => {
    try {
      const r = await api("/api/backtests/" + id);
      const phase = r.status;
      if (["queued", "loading", "deciding", "simulating"].includes(phase)) {
        const frac = r.total ? r.done / r.total : 0;
        const base = phase === "loading" ? 0 : phase === "deciding" ? 0.35 : 0.95;
        const span = phase === "loading" ? 0.35 : phase === "deciding" ? 0.6 : 0.05;
        $("#bt-progress-fill").style.width = ((base + frac * span) * 100).toFixed(1) + "%";
        const calls = r.calls ? ` · ${r.calls.made} new calls, ${r.calls.cached} cached${r.calls.errors ? `, ${r.calls.errors} errors` : ""}` : "";
        $("#bt-progress-text").textContent = (r.message || phase) + calls;
        return;
      }
      clearInterval(pollTimer);
      $("#bt-run").disabled = false;
      $("#bt-cancel").classList.add("hidden");
      $("#bt-progress-fill").style.width = "100%";
      $("#bt-progress-text").textContent = r.message || phase;
      setTimeout(() => $("#bt-progress").classList.add("hidden"), 4000);
      await loadRuns();
      if (r.summary) openRun(id);
      else toast(r.message || "Backtest failed");
    } catch (e) { $("#bt-progress-text").textContent = e.message; }
  };
  tick();
  pollTimer = setInterval(tick, 1000);
}

let RUNS = [], compare = new Set(), current = null;
async function loadRuns() {
  RUNS = await api("/api/backtests");
  const busy = RUNS.find((r) => ["queued", "loading", "deciding", "simulating"].includes(r.status));
  if (busy && !watchRun.id) watchRun(busy.id);
  const tb = $("#runs-table tbody");
  if (!RUNS.length) { tb.innerHTML = `<tr><td colspan="9" class="empty">No runs yet. Try a free strategy first, then Claude.</td></tr>`; return; }
  tb.innerHTML = RUNS.map((r) => {
    const s = r.summary;
    const engine = r.strategy === "claude" ? `${r.model}/${r.effort}` : (META.strategies[r.strategy] || r.strategy).replace(/ \(.*\)/, "");
    const label = r.params?.name || when(r.created);
    if (!s) return `<tr><td></td><td>${esc(label)}</td><td>${esc(engine)}</td><td colspan="6" class="muted">${esc(r.status)}…</td></tr>`;
    const skill = s.brier_skill_vs_market;
    return `<tr class="clickable ${current === r.id ? "selected" : ""}" data-id="${r.id}">
      <td><input type="checkbox" class="cmp" data-id="${r.id}" ${compare.has(r.id) ? "checked" : ""} aria-label="compare"></td>
      <td>${esc(label)}<div class="small muted">${esc((r.assets || []).map((a) => META.labels[a] || a).join(", "))} · ${esc(r.sizing || "")}</div></td>
      <td>${esc(engine)}</td>
      <td class="num">${s.markets}</td><td class="num">${s.trades}</td>
      <td class="num">${pct(s.win_rate, 0)}</td>
      <td class="num ${cls(s.net_pnl)}">${money(s.net_pnl, true)}</td>
      <td class="num ${cls(skill)}">${skill == null ? "—" : (skill > 0 ? "+" : "") + (skill * 100).toFixed(1) + "%"}</td>
      <td><button class="btn tiny ghost del" data-id="${r.id}" aria-label="delete run">✕</button></td></tr>`;
  }).join("");
  $$("tr.clickable", tb).forEach((tr) => tr.addEventListener("click", (e) => {
    if (e.target.closest(".cmp, .del")) return;
    openRun(tr.dataset.id);
  }));
  $$(".cmp", tb).forEach((c) => c.addEventListener("change", () => {
    if (c.checked) { if (compare.size >= 3) { c.checked = false; toast("Compare up to 3 runs"); return; } compare.add(c.dataset.id); }
    else compare.delete(c.dataset.id);
    if (current) drawEquity();
  }));
  $$(".del", tb).forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Delete this backtest run?")) return;
    await api("/api/backtests/" + b.dataset.id, { method: "DELETE" });
    compare.delete(b.dataset.id);
    if (current === b.dataset.id) { current = null; $("#bt-detail").classList.add("hidden"); }
    loadRuns();
  }));
}

const runCache = {};
async function getRun(id) {
  if (!runCache[id]) runCache[id] = await api("/api/backtests/" + id);
  return runCache[id];
}

async function openRun(id) {
  current = id;
  const r = await getRun(id);
  $$("#runs-table tr").forEach((tr) => tr.classList.toggle("selected", tr.dataset.id === id));
  $("#bt-detail").classList.remove("hidden");
  const s = r.summary, c = r.config;
  const engine = r.strategy === "claude" ? `Claude ${r.model} · ${r.effort} effort` : META.strategies[r.strategy];
  $("#d-title").textContent = r.params?.name || engine;
  const timing = [c.assets.some((a) => !META.indices.includes(a)) ? `crypto at ${c.time_delay}m left` : "",
    c.assets.some((a) => META.indices.includes(a)) ? `stocks at ${c.index_time_delay ?? 15}m left` : ""].filter(Boolean).join(", ");
  $("#d-sub").textContent = `${engine} · ${c.assets.map((a) => META.labels[a] || a).join(", ")} · ${timing} · ${META.sizing[c.sizing]} · ${r.params.start.startsWith("2000") ? "all dates" : r.params.start + " → " + r.params.end} (${r.params.sample}) · ${r.message || ""}`;
  const v = $("#d-verdict");
  const tone = s.verdict.startsWith("Profit") ? "good" : s.verdict.startsWith("Losing") ? "bad" : "warn";
  v.className = "verdict " + tone;
  v.innerHTML = `<span>${tone === "good" ? "▲" : tone === "bad" ? "▼" : "●"}</span><span>${esc(s.verdict)}</span>`;
  const ci = s.win_rate_ci?.[0] == null ? "" : `95% CI ${pct(s.win_rate_ci[0], 0)}–${pct(s.win_rate_ci[1], 0)}`;
  const pci = s.pnl_per_trade_ci?.[0] == null ? "" : `95% CI ${money(s.pnl_per_trade_ci[0], true)} to ${money(s.pnl_per_trade_ci[1], true)}`;
  const tiles = [
    ["Ending equity", money(s.ending_equity), `from ${money(c.bankroll)} (${pct(s.return_on_bankroll)})`, cls(s.net_pnl)],
    ["Net PnL", money(s.net_pnl, true), `after ${money(s.fees)} fees`, cls(s.net_pnl)],
    ["Trades", int(s.trades), `${s.skips} skipped of ${s.markets} markets`],
    ["Win rate", pct(s.win_rate), ci],
    ["Avg price paid", s.avg_price == null ? "—" : num(s.avg_price, 2), "break-even win rate ≈ price"],
    ["PnL per trade", money(s.pnl_per_trade, true), pci, cls(s.pnl_per_trade)],
    ["Max drawdown", money(s.max_drawdown), pct(s.max_drawdown_pct) + " of peak"],
    ["Call accuracy", pct(s.call_accuracy), `${s.calls} UP/DOWN calls`],
    ["Brier score", num(s.brier), `market ${num(s.brier_market)} · lower is better`],
    ["Skill vs market", s.brier_skill_vs_market == null ? "—" : (s.brier_skill_vs_market > 0 ? "+" : "") + pct(s.brier_skill_vs_market), "above 0 = sharper than Kalshi's price", cls(s.brier_skill_vs_market)],
  ];
  $("#d-tiles").innerHTML = tiles.map(([l, val, sub, k]) =>
    `<div class="tile"><div class="label">${l}</div><div class="value ${k || ""}">${val}</div><div class="sub">${esc(sub || "")}</div></div>`).join("");
  drawEquity();
  drawCalibration(s.calibration);
  renderDecisions();
}

// ------------------------------------------------------------------ charts

const charts = {};
function baseOptions(yFmt) {
  const grid = css("--grid"), ink = css("--text-2");
  return {
    responsive: true, maintainAspectRatio: false, animation: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: css("--surface"), titleColor: css("--text"), bodyColor: css("--text-2"),
        borderColor: css("--border"), borderWidth: 1, padding: 10, boxPadding: 4,
        callbacks: { label: (ctx) => ` ${ctx.dataset.label}: ${yFmt(ctx.parsed.y)}` },
      },
    },
    scales: {
      x: { type: "linear", grid: { color: grid, drawTicks: false }, border: { display: false },
           ticks: { color: ink, maxTicksLimit: 6, callback: (v) => new Date(v * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" }) } },
      y: { grid: { color: grid, drawTicks: false }, border: { display: false }, ticks: { color: ink, callback: yFmt, maxTicksLimit: 6 } },
    },
  };
}

function lineChart(id, datasets, yFmt, xFmt) {
  charts[id]?.destroy();
  const opts = baseOptions(yFmt);
  if (xFmt) opts.scales.x.ticks.callback = xFmt;
  opts.plugins.tooltip.callbacks.title = (items) => items.length ? new Date(items[0].parsed.x * 1000).toLocaleString() : "";
  charts[id] = new Chart($("#" + id), { type: "line", data: { datasets }, options: opts });
}

async function drawEquity() {
  const ids = [current, ...[...compare].filter((x) => x !== current)].slice(0, 3);
  const colors = SERIES();
  const runs = await Promise.all(ids.map(getRun));
  const datasets = runs.map((r, i) => ({
    label: r.params?.name || (r.strategy === "claude" ? `${r.model}/${r.effort}` : r.strategy),
    data: r.equity_curve.map(([x, y]) => ({ x, y })),
    borderColor: colors[i], backgroundColor: colors[i], borderWidth: 2, pointRadius: 0, pointHoverRadius: 4,
    stepped: true,
  }));
  const bank = runs[0].config.bankroll;
  const xs = datasets.flatMap((d) => d.data.map((p) => p.x));
  datasets.push({ label: "Starting bankroll", data: [{ x: Math.min(...xs), y: bank }, { x: Math.max(...xs), y: bank }],
    borderColor: css("--text-3"), borderWidth: 1, pointRadius: 0, pointHoverRadius: 0 });
  lineChart("eq-chart", datasets, (v) => "$" + Math.round(v).toLocaleString());
  $("#eq-legend").innerHTML = datasets.slice(0, -1).length > 1
    ? datasets.slice(0, -1).map((d) => `<span><i style="background:${d.borderColor}"></i>${esc(d.label)}</span>`).join("") : "";
}

let calTable = false;
$("#cal-toggle").addEventListener("click", () => {
  calTable = !calTable;
  $("#cal-chart-box").classList.toggle("hidden", calTable);
  $("#cal-table-wrap").classList.toggle("hidden", !calTable);
  $("#cal-toggle").textContent = calTable ? "Chart" : "Table";
});

function drawCalibration(cal) {
  charts.cal?.destroy();
  const c1 = SERIES()[0];
  const opts = baseOptions((v) => pct(v, 0));
  opts.interaction = { mode: "nearest", intersect: false };
  opts.scales.x = { type: "linear", min: 0, max: 1, grid: { color: css("--grid"), drawTicks: false }, border: { display: false },
    title: { display: true, text: "Model P(up)", color: css("--text-2") }, ticks: { color: css("--text-2"), callback: (v) => pct(v, 0), maxTicksLimit: 6 } };
  opts.scales.y.min = 0; opts.scales.y.max = 1;
  opts.scales.y.title = { display: true, text: "Actual UP rate", color: css("--text-2") };
  opts.plugins.tooltip.callbacks = {
    title: () => "",
    label: (ctx) => ctx.datasetIndex === 1 ? "" : ` predicted ${pct(ctx.raw.x, 0)} → actual ${pct(ctx.raw.y, 0)} (n=${ctx.raw.n})`,
  };
  charts.cal = new Chart($("#cal-chart"), {
    type: "scatter",
    data: { datasets: [
      { label: "Buckets", data: cal.map((b) => ({ x: b.predicted, y: b.actual, n: b.n })), backgroundColor: c1,
        borderColor: css("--surface"), borderWidth: 2, pointRadius: cal.map((b) => Math.max(5, Math.min(12, 3 + Math.sqrt(b.n)))), pointHoverRadius: 9 },
      { label: "Perfect", type: "line", data: [{ x: 0, y: 0 }, { x: 1, y: 1 }], borderColor: css("--text-3"), borderWidth: 1, pointRadius: 0 },
    ] },
    options: opts,
  });
  $("#cal-table").innerHTML = `<thead><tr><th class="num">Predicted</th><th class="num">Actual</th><th class="num">n</th></tr></thead><tbody>` +
    (cal.map((b) => `<tr><td class="num">${pct(b.predicted)}</td><td class="num">${pct(b.actual)}</td><td class="num">${b.n}</td></tr>`).join("") || `<tr><td colspan="3" class="empty">No data</td></tr>`) + "</tbody>";
}

// ------------------------------------------------------------------ decisions table

let decFilter = "all";
$$("#dec-filter button").forEach((b) => b.addEventListener("click", () => {
  decFilter = b.dataset.f;
  $$("#dec-filter button").forEach((x) => x.classList.toggle("on", x === b));
  renderDecisions();
}));

function decisionRow(d, tsKey = "decision_ts") {
  const actual = d.actual_up == null ? "pending" : d.actual_up ? "UP" : "DOWN";
  const t = d.trade;
  const tradeCell = t ? `${t.side} ×${t.contracts} @ ${num(t.price, 3)}` : `<span class="muted">${esc(d.skip_reason || "")}</span>`;
  const pnl = t && t.pnl != null ? `<span class="${cls(t.pnl)}">${money(t.pnl, true)}</span>` : "";
  const result = t && t.won != null ? `<span class="tag ${t.won ? "won" : "lost"}">${t.won ? "WON" : "LOST"}</span> ` : "";
  const dir = d.direction;
  return `<tr><td class="num">${when(d[tsKey])}</td><td>${esc(META.labels[d.asset] || d.asset)}${META.indices.includes(d.asset) ? `<div class="small muted">&gt; ${esc(d.strike)}</div>` : ""}</td>
    <td><span class="tag ${dir === "UP" ? "up" : dir === "DOWN" ? "down" : ""}">${esc(dir)}</span></td>
    <td class="num">${num(d.probability_up, 2)}</td><td class="num">${num(d.market_up, 2)}</td>
    <td>${result}${esc(actual)}</td><td>${tradeCell}</td><td class="num">${pnl}</td>
    <td class="reason">${esc(d.reason)}</td></tr>`;
}

async function renderDecisions() {
  if (!current) return;
  const r = await getRun(current);
  const rows = r.decisions.filter((d) => decFilter === "all" || (decFilter === "trades" && d.trade) ||
    (decFilter === "wins" && d.trade?.won) || (decFilter === "losses" && d.trade && d.trade.won === false) ||
    (decFilter === "skips" && !d.trade));
  $("#dec-table tbody").innerHTML = rows.length ? rows.slice().reverse().map((d) => decisionRow(d)).join("")
    : `<tr><td colspan="9" class="empty">Nothing here</td></tr>`;
}

// ------------------------------------------------------------------ live

$("#live-start").addEventListener("click", async () => { await api("/api/live/start", { method: "POST" }); toast("Live paper trading started"); loadLive(); });
$("#live-stop").addEventListener("click", async () => { await api("/api/live/stop", { method: "POST" }); toast("Stopping after the current step"); setTimeout(loadLive, 1500); });
$("#live-reset").addEventListener("click", async () => {
  if (!confirm(`Reset the live paper account to ${money(SETTINGS.bankroll)}? History in the activity feed is cleared.`)) return;
  try { await api("/api/live/reset", { method: "POST" }); loadLive(); } catch (e) { toast(e.message); }
});

async function loadLive() { try { renderLive(await api("/api/live")); } catch (e) { toast(e.message); } }

let liveCurveLen = -1;
function renderLive(L) {
  const wins = L.closed.filter((t) => t.won).length;
  const tiles = [
    ["Equity", money(L.equity), `started ${money(L.bankroll)}`, cls(L.equity - L.bankroll)],
    ["Realized PnL", money(L.realized_pnl, true), `${L.closed.length} settled trades`, cls(L.realized_pnl)],
    ["Cash", money(L.cash), `${L.positions.length} open positions`],
    ["Win rate", L.closed.length ? pct(wins / L.closed.length) : "—", `${wins} wins`],
    ["Engine", SETTINGS.strategy === "claude" ? `${SETTINGS.model}` : META.strategies[SETTINGS.strategy].replace(/ \(.*\)/, ""), SETTINGS.strategy === "claude" ? `${SETTINGS.effort} effort` : "no AI"],
  ];
  $("#live-tiles").innerHTML = tiles.map(([l, v, s, k]) => `<div class="tile"><div class="label">${l}</div><div class="value ${k || ""}">${esc(v)}</div><div class="sub">${esc(s)}</div></div>`).join("");
  $("#live-assets").innerHTML = Object.entries(L.waiting).map(([a, s]) => `<span class="pill"><b>${esc(a)}</b>${esc(s)}</span>`).join("")
    || `<span class="small muted">${L.running ? "Starting…" : "Not running. Press Start."}</span>`;
  $("#live-start").disabled = L.running;
  $("#live-feed").innerHTML = L.events.slice().reverse().map((e) => `<div class="ev ${e.kind}"><time>${clock(e.t)}</time>${esc(e.msg)}</div>`).join("")
    || `<div class="empty">No activity yet</div>`;
  $("#live-pos").innerHTML = `<thead><tr><th>Asset</th><th>Side</th><th class="num">Contracts</th><th class="num">Price</th><th class="num">Cost</th><th>Closes</th></tr></thead><tbody>` +
    (L.positions.map((p) => `<tr><td>${esc(p.asset)}</td><td><span class="tag ${p.side === "UP" ? "up" : "down"}">${p.side}</span></td><td class="num">${p.contracts}</td><td class="num">${num(p.price, 3)}</td><td class="num">${money(p.cost + p.fees)}</td><td>${when(p.close_ts)}</td></tr>`).join("")
      || `<tr><td colspan="6" class="empty">No open positions</td></tr>`) + "</tbody>";
  const rows = [...L.pending, ...L.history.slice().reverse()];
  $("#live-dec").innerHTML = `<thead><tr><th>Time</th><th>Asset</th><th>Call</th><th class="num">P(up)</th><th class="num">Mkt</th><th>Outcome</th><th>Trade</th><th class="num">PnL</th><th>Reason</th></tr></thead><tbody>` +
    (rows.map((d) => decisionRow(d)).join("") || `<tr><td colspan="9" class="empty">No decisions yet</td></tr>`) + "</tbody>";
  if (L.equity_curve.length !== liveCurveLen) {
    liveCurveLen = L.equity_curve.length;
    const pts = L.equity_curve.map(([x, y]) => ({ x, y }));
    if (pts.length === 1) pts.push({ x: Date.now() / 1000, y: pts[0].y });
    lineChart("live-chart", [{ label: "Equity", data: pts, borderColor: SERIES()[0], borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, stepped: true }],
      (v) => "$" + Math.round(v).toLocaleString(), (v) => new Date(v * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }));
  }
}

// ------------------------------------------------------------------ usage

async function loadUsage() {
  const u = await api("/api/usage");
  const t = u.today, w = u.last_7d, h = u.last_5h;
  const tiles = [
    ["Calls today", int(t.calls), `${t.errors} errors`],
    ["Tokens today", int(t.tokens), `${int(t.output_tokens)} output`],
    ["Last 5 hours", int(h.calls) + " calls", `${int(h.tokens)} tokens`],
    ["Last 7 days", int(w.calls) + " calls", `${int(w.tokens)} tokens`],
    ["Avg per call", int(u.all_time.avg_tokens) + " tok", `${u.all_time.avg_latency_s ?? "—"} s latency`],
    ["API-equivalent", money(u.all_time.api_equiv_usd), "not billed: subscription"],
  ];
  $("#u-tiles").innerHTML = tiles.map(([l, v, s]) => `<div class="tile"><div class="label">${l}</div><div class="value">${esc(v)}</div><div class="sub">${esc(s)}</div></div>`).join("");
  const lim = u.last_limit_error;
  $("#u-limit").classList.toggle("hidden", !lim);
  if (lim) $("#u-limit").innerHTML = `<b>Usage limit hit</b> at ${esc(new Date(lim.time * 1000).toLocaleString())} (${esc(lim.model)}): ${esc(lim.error)}. Switch to haiku or lower effort, or wait for your plan to reset.`;

  charts.usage?.destroy();
  const opts = baseOptions((v) => int(v));
  opts.scales.x = { grid: { display: false }, border: { display: false }, ticks: { color: css("--text-2"), maxTicksLimit: 7 } };
  opts.plugins.tooltip.callbacks = { label: (ctx) => ` ${int(ctx.parsed.y)} tokens · ${u.per_day[ctx.dataIndex].calls} calls` };
  charts.usage = new Chart($("#u-chart"), {
    type: "bar",
    data: { labels: u.per_day.map((d) => new Date(d.date + "T12:00").toLocaleDateString(undefined, { month: "short", day: "numeric" })),
      datasets: [{ label: "Tokens", data: u.per_day.map((d) => d.tokens), backgroundColor: SERIES()[0], borderRadius: { topLeft: 4, topRight: 4 }, borderSkipped: "bottom", maxBarThickness: 28 }] },
    options: opts,
  });

  $("#u-combos").innerHTML = `<thead><tr><th>Model</th><th>Effort</th><th class="num">Calls</th><th class="num">Avg tokens</th><th class="num">Avg latency</th><th class="num">Errors</th></tr></thead><tbody>` +
    (u.by_model_effort.map((c) => `<tr><td>${esc(c.model)}</td><td>${esc(c.effort)}</td><td class="num">${c.calls}</td><td class="num">${int(c.avg_tokens)}</td><td class="num">${c.avg_latency_s ?? "—"} s</td><td class="num">${c.errors}</td></tr>`).join("")
      || `<tr><td colspan="6" class="empty">No calls yet</td></tr>`) + "</tbody>";
  $("#u-recent").innerHTML = `<thead><tr><th>Time</th><th>Source</th><th>Model</th><th>Effort</th><th class="num">Tokens</th><th class="num">Thinking</th><th class="num">Latency</th><th>Status</th></tr></thead><tbody>` +
    (u.recent.map((r) => `<tr><td class="num">${when(r.time)}</td><td>${esc(r.source)}</td><td>${esc(r.model_id || r.model)}</td><td>${esc(r.effort)}</td>
      <td class="num">${int((r.input_tokens || 0) + (r.cache_creation_tokens || 0) + (r.cache_read_tokens || 0) + (r.output_tokens || 0))}</td>
      <td class="num">${int(r.thinking_tokens || 0)}</td><td class="num">${r.latency_s}s</td>
      <td>${r.ok ? "ok" : `<span class="neg">${esc(r.error_kind || "error")}</span> <span class="muted small">${esc((r.error || "").slice(0, 80))}</span>`}</td></tr>`).join("")
      || `<tr><td colspan="8" class="empty">No calls yet</td></tr>`) + "</tbody>";
}

init().catch((e) => toast("Failed to load: " + e.message, 8000));
