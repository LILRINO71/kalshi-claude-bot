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
const pct = (v, d = 0) => v == null ? "—" : (v * 100).toFixed(d) + "%";
const rr = (v) => v == null ? "—" : (v > 0 ? "+" : "") + v.toFixed(2) + "R";
const int = (v) => v == null ? "—" : Math.round(v).toLocaleString();
const cls = (v) => v > 0 ? "pos" : v < 0 ? "neg" : "";
const when = (ts) => new Date(ts * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
const ymd = (d) => d.toLocaleDateString("en-CA");  // local YYYY-MM-DD

function toast(msg, ms = 3200) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), ms);
}

let META = null, SETTINGS = null;

// ------------------------------------------------------------------ tabs

$$(".tab").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
function showTab(name) {
  $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
  try { localStorage.setItem("tab", name); } catch (e) {}
  if (name === "backtest") loadRuns();
  if (name === "usage") loadUsage();
  if (name === "live") loadLive();
  if (name === "settings") fillSettings();
}

// ------------------------------------------------------------------ top bar

async function init() {
  [META, SETTINGS] = await Promise.all([api("/api/meta"), api("/api/settings")]);
  $("#q-model").innerHTML = Object.entries(META.models).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
  $("#q-model").addEventListener("change", (e) => saveQuick({ model: e.target.value }));
  $("#q-effort").innerHTML = META.efforts.map((e) => `<button data-v="${e}">${e}</button>`).join("");
  $$("#q-effort button").forEach((b) => b.addEventListener("click", () => saveQuick({ effort: b.dataset.v })));
  $("#bt-brain").innerHTML = Object.entries(META.brains).map(([k, v]) => `<option value="${k}">${esc(v.split(":")[0])}</option>`).join("");
  $("#s-cli").textContent = META.claude_path || "not found (install Claude Code or set CLAUDE_BIN)";
  $("#bt-end").value = ymd(new Date());
  renderHistory();
  syncTop();
  let tab = "backtest";
  try { tab = localStorage.getItem("tab") || tab; } catch (e) {}
  const linked = location.hash.slice(1);
  if (linked && $("#tab-" + linked)) tab = linked;
  showTab(tab);
  refreshPills();
  setInterval(refreshPills, 10000);
}

function renderHistory() {
  $("#bt-start").value = META.history_start;  // the longest window with bars and no days the model may remember
  $("#bt-history").innerHTML = META.alpaca
    ? `History reaches back to the model's cutoff: ${esc(META.clean_start)} (Yahoo for the last ~60 days, Alpaca before that).`
    : `5-minute history starts ${esc(META.yahoo_oldest)} (Yahoo keeps ~60 days). To go back to the model's cutoff (${esc(META.clean_start)}), add a free Alpaca key under Settings.`;
}

function syncTop() {
  $("#q-model").value = SETTINGS.model;
  $$("#q-effort button").forEach((b) => b.classList.toggle("on", b.dataset.v === SETTINGS.effort));
}

async function saveQuick(patch) {
  try {
    SETTINGS = await api("/api/settings", { method: "POST", body: { ...SETTINGS, ...patch } });
    syncTop();
    toast((patch.effort ? `Effort → ${patch.effort}` : `Model → ${patch.model}`) + " (used by the next run)");
  } catch (e) { toast(e.message); }
}

async function refreshPills() {
  try {
    const [live, u] = await Promise.all([api("/api/live"), api("/api/usage")]);
    const p = $("#pill-live");
    const fresh = live.status && Date.now() / 1000 - live.status.updated < 120;
    p.classList.toggle("on", !!fresh);
    $("span", p).textContent = live.equity == null ? "Live: not started" : `Live ${money(live.equity)}${fresh ? " · " + live.status.phase : ""}`;
    $("#pill-usage").textContent = `${u.today.calls} calls · ${int(u.today.tokens)} tokens today`;
  } catch (e) {}
}

// ------------------------------------------------------------------ backtests

let btTimer = null;
const picked = new Set();

async function loadRuns() {
  clearTimeout(btTimer);
  try {
    const r = await api("/api/runs");
    const job = r.job;
    $("#bt-run").disabled = job.running;
    $("#bt-stop").classList.toggle("hidden", !job.running);
    $("#bt-status").textContent = job.running ? `Running${job.run_id ? " " + job.run_id : ""}…` : (job.error ? "Error: " + job.error : "");
    const log = $("#bt-log");
    log.classList.toggle("hidden", !job.lines.length);
    log.textContent = job.lines.join("\n");
    log.scrollTop = log.scrollHeight;
    const sel = $("#bt-profile"), cur = sel.value;
    sel.innerHTML = `<option value="">Fresh memory (seed rules only)</option>` + r.profiles.map((p) => `<option value="${esc(p)}">Profile: ${esc(p)}</option>`).join("");
    sel.value = r.profiles.includes(cur) ? cur : "";
    renderRuns(r.runs);
    if (job.running && $("#tab-backtest").classList.contains("active")) btTimer = setTimeout(loadRuns, 3000);
  } catch (e) { $("#bt-status").textContent = e.message; }
}

function engineOf(x) {
  return x.brain === "claude" ? `${x.model}/${x.effort}${x.learn === false ? " · frozen" : ""}` : x.brain;
}

function renderRuns(runs) {
  const head = `<thead><tr><th></th><th>Run</th><th>Engine</th><th>Days</th><th class="num">Trades</th><th class="num">Win</th><th class="num">Avg R</th><th class="num">PnL</th><th class="num">Return</th><th class="num">SPY</th><th>Verdict</th><th></th></tr></thead>`;
  const rows = runs.map((x) => {
    const label = x.name || (x.created ? when(x.created) : x.id);
    const ci = x.avg_r_ci && x.avg_r_ci[0] != null ? `<div class="small muted">${rr(x.avg_r_ci[0])} to ${rr(x.avg_r_ci[1])}</div>` : "";
    return `<tr><td><input type="checkbox" data-id="${esc(x.id)}" ${picked.has(x.id) ? "checked" : ""} aria-label="Select for comparison"></td>
      <td>${esc(label)}<div class="small muted">${esc(x.status)}${x.leaky ? " · leaky dates" : ""}${x.profile ? " · from " + esc(x.profile) : ""}</div></td>
      <td>${esc(engineOf(x))}</td><td>${esc(x.start)} → ${esc(x.end)}<div class="small muted">${x.days ?? 0} days</div></td>
      <td class="num">${x.trades ?? 0}</td><td class="num">${pct(x.win_rate)}</td>
      <td class="num ${cls(x.avg_r)}">${rr(x.avg_r)}${ci}</td>
      <td class="num ${cls(x.net_pnl)}">${money(x.net_pnl, true)}</td>
      <td class="num ${cls(x.return_pct)}">${x.return_pct == null ? "—" : x.return_pct.toFixed(2) + "%"}</td>
      <td class="num">${x.spy_pct == null ? "—" : x.spy_pct.toFixed(2) + "%"}</td>
      <td class="small">${esc(x.verdict || "")}</td>
      <td><div class="row-actions"><a class="btn tiny ghost" href="/report/${encodeURIComponent(x.id)}" target="_blank" rel="noopener">Report</a>
        <button class="btn tiny ghost danger" data-del="${esc(x.id)}">Delete</button></div></td></tr>`;
  }).join("");
  $("#bt-runs").innerHTML = head + `<tbody>${rows || `<tr><td colspan="12" class="empty">No runs yet. Start with the free gap-and-go control, then Claude.</td></tr>`}</tbody>`;
  $$("#bt-runs input[type=checkbox]").forEach((c) => c.addEventListener("change", () => {
    c.checked ? picked.add(c.dataset.id) : picked.delete(c.dataset.id);
    $("#cmp-go").disabled = picked.size < 2;
  }));
  $$("#bt-runs [data-del]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Delete this run and its report? This can't be undone.")) return;
    try { await api("/api/runs/" + encodeURIComponent(b.dataset.del), { method: "DELETE" }); picked.delete(b.dataset.del); loadRuns(); }
    catch (e) { toast(e.message); }
  }));
  $("#cmp-go").disabled = picked.size < 2;
}

$("#bt-run").addEventListener("click", async () => {
  try {
    await api("/api/backtest", { method: "POST", body: {
      start: $("#bt-start").value, end: $("#bt-end").value, brain: $("#bt-brain").value, learn: $("#bt-learn").checked,
      name: $("#bt-name").value, profile: $("#bt-profile").value, save_profile: $("#bt-save").value,
      model: SETTINGS.model, effort: SETTINGS.effort } });
    toast("Backtest started");
    loadRuns();
  } catch (e) { toast(e.message, 6000); }
});
$("#bt-stop").addEventListener("click", async () => { await api("/api/stop", { method: "POST" }); toast("Stopping after the current day"); });

$("#cmp-go").addEventListener("click", async () => {
  const box = $("#cmp");
  try {
    const c = await api("/api/compare?ids=" + [...picked].map(encodeURIComponent).join(","));
    const rows = c.runs.map((r, i) => {
      const v = r.vs_baseline;
      const diff = v ? `${v.mean_daily_pct_diff >= 0 ? "+" : ""}${v.mean_daily_pct_diff.toFixed(3)}%/day` +
        (v.ci[0] != null ? ` <span class="muted">(${v.ci[0].toFixed(3)} to ${v.ci[1].toFixed(3)})</span>` : "") + `<div class="small">${esc(v.verdict)}</div>` : "<b>baseline</b>";
      return `<tr><td>${esc(r.name || r.id)}</td><td>${esc(r.engine)}</td><td class="num">${r.trades}</td><td class="num ${cls(r.avg_r)}">${rr(r.avg_r)}</td>
        <td class="num ${cls(r.pnl)}">${money(r.pnl, true)}</td><td>${diff}</td></tr>`;
    }).join("");
    box.innerHTML = `<h3>Compared on ${c.days.length} shared days${c.days.length ? ` (${esc(c.days[0])} → ${esc(c.days[c.days.length - 1])})` : ""}</h3>
      <p class="small muted">The first run you selected is the baseline (select the control first). Each other run's daily P&L is paired with the baseline's on the same day; the interval is a 95% bootstrap of the mean difference.</p>
      <div class="table-wrap"><table class="table"><thead><tr><th>Run</th><th>Engine</th><th class="num">Trades</th><th class="num">Avg R</th><th class="num">PnL</th><th>vs baseline</th></tr></thead><tbody>${rows}</tbody></table></div>`;
    box.classList.remove("hidden");
  } catch (e) { toast(e.message); }
});

// ------------------------------------------------------------------ live

async function loadLive() {
  try {
    const L = await api("/api/live");
    const st = L.status, last = (L.days || []).slice(-1)[0];
    $("#lv-account").innerHTML = L.equity == null && !st ? `<p class="muted">Not started yet.</p>` :
      `<div class="tiles">
        <div class="tile"><div class="label">Equity</div><div class="value">${money(L.equity)}</div><div class="sub">after ${(L.days || []).length} days</div></div>
        <div class="tile"><div class="label">Last day</div><div class="value ${cls(last && last.pnl)}">${last ? money(last.pnl, true) : "—"}</div><div class="sub">${last ? esc(last.day) + " · " + last.trades + " trades" : ""}</div></div>
        <div class="tile"><div class="label">Now</div><div class="value">${st ? esc(st.phase) : "—"}</div><div class="sub">${st ? "focus " + esc((st.focus || []).join(", ") || "none") : ""}</div></div>
      </div>` + (last && last.summary ? `<p>${esc(last.summary)}</p>` : "") +
      (L.has_report ? `<p><a href="/report/live-live" target="_blank" rel="noopener">Open the live report</a></p>` : "");
    const pos = (st && st.positions) || [], ords = (st && st.orders) || [];
    $("#lv-positions").innerHTML = `<thead><tr><th>Stock</th><th>Side</th><th class="num">Shares</th><th class="num">Entry / price</th><th class="num">Stop</th><th class="num">Target</th></tr></thead><tbody>` +
      (pos.map((p) => `<tr><td>${esc(p.ticker)}</td><td>${esc(p.side)}</td><td class="num">${p.shares}</td><td class="num">${p.entry.toFixed(2)}</td><td class="num">${p.stop.toFixed(2)}</td><td class="num">${p.target ? p.target.toFixed(2) : "—"}</td></tr>`).join("") +
       ords.map((o) => `<tr><td>${esc(o.ticker)}</td><td>${esc(o.side)} ${esc(o.kind)} order</td><td class="num">—</td><td class="num">${o.price ? o.price.toFixed(2) : "mkt"}</td><td class="num">${o.stop.toFixed(2)}</td><td class="num">${o.target ? o.target.toFixed(2) : "—"}</td></tr>`).join("")
       || `<tr><td colspan="6" class="empty">No positions or working orders</td></tr>`) + "</tbody>";
    $("#lv-events").innerHTML = ((st && st.events) || []).slice(-30).reverse().map((e) =>
      `<div class="ev ${esc(e.kind)}"><time>${new Date(e.t * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}</time>${esc(e.msg)}</div>`).join("");
    $("#lv-playbook").innerHTML = `<thead><tr><th>Id</th><th>Status</th><th class="num">Trades</th><th class="num">R</th><th class="num">Helpful/harmful</th><th>Rule</th></tr></thead><tbody>` +
      ((L.playbook || []).map((r) => `<tr><td>${esc(r.id)}</td><td>${esc(r.status)}</td><td class="num">${r.uses}</td><td class="num ${cls(r.sum_r)}">${r.sum_r.toFixed(1)}</td><td class="num">${r.helpful}/${r.harmful}</td><td>${esc(r.text)}</td></tr>`).join("")
        || `<tr><td colspan="6" class="empty">No live profile yet: run a Claude backtest with "Save what it learns as profile: live", or start live trading.</td></tr>`) + "</tbody>";
    const g = L.ledgers || {};
    $("#lv-track").textContent = g.track || "No trades yet.";
    $("#lv-mgmt").textContent = g.mgmt || "Nothing measured yet.";
    $("#lv-base").textContent = g.base_rates || "Nothing yet.";
  } catch (e) { toast(e.message); }
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
    ["Avg per call", int(u.all_time.avg_tokens) + " tok", `${u.all_time.avg_latency_s ?? "—"} s`],
    ["API-equivalent", money(u.all_time.api_equiv_usd), "not billed: subscription"],
  ];
  $("#u-tiles").innerHTML = tiles.map(([l, v, s]) => `<div class="tile"><div class="label">${l}</div><div class="value">${esc(v)}</div><div class="sub">${esc(s)}</div></div>`).join("");
  const lim = u.last_limit_error;
  $("#u-limit").classList.toggle("hidden", !lim);
  if (lim) $("#u-limit").innerHTML = `<b>Usage limit hit</b> at ${esc(new Date(lim.time * 1000).toLocaleString())} (${esc(lim.model)}): ${esc(lim.error)}. Lower the effort, switch model, or wait for your plan to reset; an interrupted backtest resumes with <code>--resume</code>.`;
  const block = (rows, key) => rows.map((c) => `<tr><td>${esc(c[key[0]])}</td>${key[1] ? `<td>${esc(c[key[1]])}</td>` : ""}<td class="num">${c.calls}</td><td class="num">${int(c.avg_tokens)}</td><td class="num">${c.avg_latency_s ?? "—"} s</td><td class="num">${c.errors}</td></tr>`).join("");
  $("#u-combos").innerHTML = `<thead><tr><th>Model</th><th>Effort</th><th class="num">Calls</th><th class="num">Avg tokens</th><th class="num">Avg latency</th><th class="num">Errors</th></tr></thead><tbody>` +
    (block(u.by_model_effort, ["model", "effort"]) || `<tr><td colspan="6" class="empty">No calls yet</td></tr>`) + "</tbody>";
  const steps = Object.entries(u.by_step).map(([k, v]) => ({ step: k, ...v }));
  $("#u-steps").innerHTML = `<thead><tr><th>Step</th><th class="num">Calls</th><th class="num">Avg tokens</th><th class="num">Avg latency</th><th class="num">Errors</th></tr></thead><tbody>` +
    (block(steps, ["step"]) || `<tr><td colspan="5" class="empty">No calls yet</td></tr>`) + "</tbody>";
  $("#u-recent").innerHTML = `<thead><tr><th>Time</th><th>Source</th><th>Model</th><th>Effort</th><th class="num">Tokens</th><th class="num">Latency</th><th>Status</th></tr></thead><tbody>` +
    (u.recent.map((r) => `<tr><td class="num">${when(r.time)}</td><td>${esc(r.source)}</td><td>${esc(r.model_id || r.model)}</td><td>${esc(r.effort)}</td>
      <td class="num">${int((r.input_tokens || 0) + (r.cache_creation_tokens || 0) + (r.cache_read_tokens || 0) + (r.output_tokens || 0))}</td>
      <td class="num">${r.latency_s}s</td>
      <td>${r.ok ? "ok" : `<span class="neg">${esc(r.error_kind || "error")}</span> <span class="muted small">${esc((r.error || "").slice(0, 80))}</span>`}</td></tr>`).join("")
      || `<tr><td colspan="7" class="empty">No calls yet</td></tr>`) + "</tbody>";
}

// ------------------------------------------------------------------ settings

function fillSettings() {
  const f = $("#s-form");
  for (const el of f.elements) {
    if (!el.name || !(el.name in SETTINGS)) continue;
    const v = SETTINGS[el.name];
    if (el.type === "checkbox") el.checked = !!v;
    else el.value = Array.isArray(v) ? v.join(", ") : v;
  }
}

$("#s-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const patch = {};
  for (const el of e.target.elements) {
    if (!el.name) continue;
    if (el.type === "checkbox") patch[el.name] = el.checked;
    else if (el.name === "checkins") patch.checkins = el.value.split(",").map((s) => s.trim()).filter(Boolean);
    else if (el.type === "number") patch[el.name] = Number(el.value);
    else patch[el.name] = el.value;
  }
  try {
    SETTINGS = await api("/api/settings", { method: "POST", body: { ...SETTINGS, ...patch } });
    fillSettings();
    $("#s-status").textContent = "Saved.";
  } catch (err) { $("#s-status").textContent = err.message; }
});

$("#a-save").addEventListener("click", async () => {
  $("#a-status").textContent = "Checking…";
  try {
    const r = await api("/api/alpaca", { method: "POST", body: { key_id: $("#a-key").value, secret_key: $("#a-secret").value } });
    $("#a-status").textContent = "Saved. " + r.check;
    $("#a-secret").value = "";
    META = await api("/api/meta");
    renderHistory();
  } catch (e) { $("#a-status").textContent = e.message; }
});

init().catch((e) => toast("Failed to load: " + e.message, 8000));
