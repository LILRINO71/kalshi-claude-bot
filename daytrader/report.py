"""A self-contained HTML report for a backtest or the live paper account."""
import html
import json

from . import clock, store

_CSS = """
:root{color-scheme:light;--page:#f4f3ef;--surface:#fcfcfb;--border:#e2e0da;--text:#0b0b0b;--text-2:#52514e;
--text-3:#7d7b75;--accent:#2a78d6;--good:#0b7a0b;--bad:#b02a2a;--grid:#e8e6e1}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--page:#111110;
--surface:#1a1a19;--border:#2e2e2b;--text:#fff;--text-2:#c3c2b7;--text-3:#8d8c84;--accent:#3987e5;
--good:#4cc74c;--bad:#ef7272;--grid:#2a2a28}}
*{box-sizing:border-box}body{margin:0;background:var(--page);color:var(--text);
font:14px/1.5 Inter,system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1180px;margin:0 auto;padding:24px 16px 64px}h1{font-size:24px;margin:0 0 4px}
h2{font-size:16px;margin:28px 0 10px}.sub{color:var(--text-2)}.muted{color:var(--text-3)}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px;margin:16px 0}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px}
.tile .l{color:var(--text-2);font-size:12px}.tile .v{font-size:20px;font-weight:600;margin-top:2px}
.tile .s{color:var(--text-3);font-size:12px}.pos{color:var(--good)}.neg{color:var(--bad)}
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;padding:6px 8px;
border-bottom:1px solid var(--border);vertical-align:top}th{color:var(--text-2);font-weight:500}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}.verdict{padding:10px 14px;border-radius:10px;
border:1px solid var(--border);background:var(--surface);margin:12px 0}.warn{border-color:#d08a00}
.chart{height:300px}details{margin:6px 0}summary{cursor:pointer;color:var(--accent)}
code{font-family:JetBrains Mono,Consolas,monospace;font-size:12px}
"""


def _e(x):
    return html.escape(str(x if x is not None else ""))


def _money(v, sign=False):
    if v is None:
        return "—"
    s = f"${abs(v):,.2f}"
    return ("+" if sign and v > 0 else "−" if v < 0 else "") + s


def _pct(v, d=1, sign=True):
    return "—" if v is None else f"{v:+.{d}f}%" if sign else f"{v:.{d}f}%"


def _cls(v):
    return "pos" if v and v > 0 else "neg" if v and v < 0 else ""


def _tile(label, value, sub="", cls=""):
    return f'<div class="tile"><div class="l">{_e(label)}</div><div class="v {cls}">{value}</div><div class="s">{_e(sub)}</div></div>'


def render(state, rules, history):
    s = state.get("summary") or {}
    cfg = state["config"]
    engine = f"Claude {cfg['model']} · {cfg['effort']}" if cfg["brain"] == "claude" else cfg["brain"]
    lr = s.get("learning") or {}
    f, l = lr.get("first_half") or {}, lr.get("second_half") or {}
    tiles = [
        _tile("Ending equity", _money(s.get("ending_equity")), f"from {_money(cfg['bankroll'])} ({_pct(s.get('return_pct'), 2)})",
              _cls(s.get("net_pnl"))),
        _tile("Net PnL", _money(s.get("net_pnl"), True), f"after {_money(s.get('fees'))} fees", _cls(s.get("net_pnl"))),
        _tile("SPY over the same days", _pct(s.get("benchmark_spy_pct"), 2), "buy and hold, for scale"),
        _tile("Trades", s.get("trades", 0), f"on {s.get('days_traded', 0)} of {s.get('days', 0)} days"),
        _tile("Win rate", _pct((s.get("win_rate") or 0) * 100 if s.get("win_rate") is not None else None, 0, False),
              "95% CI " + ("—" if not s.get("win_rate_ci") or s["win_rate_ci"][0] is None else
                           f"{s['win_rate_ci'][0] * 100:.0f}–{s['win_rate_ci'][1] * 100:.0f}%")),
        _tile("Average R", "—" if s.get("avg_r") is None else f"{s['avg_r']:+.2f}R", f"total {s.get('total_r', 0):+.1f}R",
              _cls(s.get("avg_r"))),
        _tile("Profit factor", "—" if not s.get("profit_factor") else f"{s['profit_factor']:.2f}", "gross wins / gross losses"),
        _tile("Max drawdown", _money(s.get("max_drawdown")), _pct(s.get("max_drawdown_pct"), 1, False) + " of peak"),
        _tile("Sharpe (daily, annualized)", "—" if s.get("sharpe_daily_annualized") is None else
              f"{s['sharpe_daily_annualized']:.2f}", "noisy below ~60 days"),
        _tile("Learning: 1st half", "—" if f.get("avg_r") is None else f"{f['avg_r']:+.2f}R/trade",
              f"{f.get('trades', 0)} trades, {_money(f.get('pnl'), True)}"),
        _tile("Learning: 2nd half", "—" if l.get("avg_r") is None else f"{l['avg_r']:+.2f}R/trade",
              f"{l.get('trades', 0)} trades, {_money(l.get('pnl'), True)}"),
        _tile("Claude calls", s.get("llm_calls", 0), f"{s.get('llm_cached', 0)} cached, {s.get('llm_errors', 0)} errors"),
    ]
    leak = ""
    if state.get("leaky"):
        leak = (f'<div class="verdict warn"><b>Leaky run.</b> These dates are before the model\'s training cutoff '
                f'(~{_e(state.get("cutoff"))}); it may remember what happened. Treat the results as a smoke test.</div>')
    days_rows = []
    for d in state["days"]:
        changes = ", ".join(d.get("rule_changes") or []) or "—"
        days_rows.append(f"<tr><td>{_e(d['day'])}</td><td class='n {_cls(d['pnl'])}'>{_money(d['pnl'], True)}</td>"
                         f"<td class='n'>{d['trades']}</td><td>{_e(', '.join(d.get('focus') or []))}</td>"
                         f"<td>{_e(d.get('summary') or d.get('market_view') or '')}</td><td class='muted'>{_e(changes)}</td></tr>")
    trade_rows = []
    for t in state["trades"]:
        trade_rows.append(f"<tr><td>{_e(t['day'])} {_e(clock.hhmm(t['entry_ts']))}</td><td>{_e(t['ticker'])}</td>"
                          f"<td>{_e(t['side'])}</td><td class='n'>{t['shares']}</td><td class='n'>{t['entry']:.2f}</td>"
                          f"<td class='n'>{t['exit']:.2f}</td><td>{_e(t['exit_reason'])}</td>"
                          f"<td class='n {_cls(t['pnl'])}'>{_money(t['pnl'], True)}</td><td class='n'>{_e(t.get('r'))}</td>"
                          f"<td>{_e(t.get('setup'))}</td><td class='muted'>{_e(', '.join(t.get('rules') or []))}</td></tr>")
    rule_rows = []
    for r in sorted(rules, key=lambda r: (r["status"] == "retired", r["id"])):
        rule_rows.append(f"<tr><td><code>{_e(r['id'])}</code></td><td>{_e(r['status'])}</td><td class='n'>{r['uses']}</td>"
                         f"<td class='n {_cls(r['sum_r'])}'>{r['sum_r']:+.2f}</td><td class='n'>{r['helpful']}/{r['harmful']}</td>"
                         f"<td>{_e(r['text'])}{('<div class=muted>' + _e(r.get('retired_why')) + '</div>') if r.get('retired_why') else ''}</td>"
                         f"<td class='muted'>{_e(r['source'])} {_e(r['created'])}</td></tr>")
    hist_rows = [f"<tr><td>{_e(h.get('day'))}</td><td>{_e(h.get('op'))}</td><td>{_e(h.get('id') or h.get('into') or (h.get('rule') or {}).get('id'))}</td>"
                 f"<td>{_e(h.get('new') or (h.get('rule') or {}).get('text') or h.get('why') or '')}</td></tr>" for h in history[-300:]]
    days_detail = []
    for d in state["days"][-60:]:
        refl = d.get("reflection") or {}
        days_detail.append(f"<details><summary>{_e(d['day'])}: {_money(d['pnl'], True)}, focus "
                           f"{_e(', '.join(d.get('focus') or []) or 'none')}</summary><div class='card'>"
                           f"<p><b>Morning:</b> {_e((d.get('brief') or {}).get('market_view'))}</p>"
                           f"<p><b>Review:</b> {_e(refl.get('process_review'))}</p><p><b>Missed:</b> {_e(refl.get('missed'))}</p>"
                           f"<p><b>Lessons:</b> {_e('; '.join(x.get('text', '') for x in refl.get('lessons') or []))}</p>"
                           f"<p class='muted'>{_e(' | '.join(e['msg'] for e in (d.get('events') or [])[:40]))}</p></div></details>")
    curve = [{"x": t * 1000, "y": round(v, 2)} for t, v in state["equity_curve"]]
    title = state.get("name") or f"{engine}, {state['start']} to {state['end']}"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Day Trader Report</title>
<style>{_CSS}</style>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script></head><body><main>
<h1>{_e(title)}</h1>
<div class="sub">{_e(engine)} · {_e(state['start'])} → {_e(state['end'])} · run {_e(state['id'])} · status {_e(state.get('status'))}
{(' · ' + _e(state.get('message'))) if state.get('message') else ''}</div>
{leak}<div class="verdict">{_e(s.get('verdict', ''))}</div>
<div class="tiles">{''.join(tiles)}</div>
<h2>Equity</h2><div class="card"><div class="chart"><canvas id="eq"></canvas></div></div>
<h2>Days</h2><div class="card"><table><thead><tr><th>Day</th><th class="n">PnL</th><th class="n">Trades</th><th>Focus</th>
<th>Journal</th><th>Playbook changes</th></tr></thead><tbody>{''.join(days_rows) or '<tr><td colspan=6>none</td></tr>'}</tbody></table></div>
<h2>Trades</h2><div class="card"><table><thead><tr><th>Entry</th><th>Stock</th><th>Side</th><th class="n">Shares</th>
<th class="n">Entry</th><th class="n">Exit</th><th>Exit</th><th class="n">PnL</th><th class="n">R</th><th>Setup</th><th>Rules</th></tr></thead>
<tbody>{''.join(trade_rows) or '<tr><td colspan=11>no trades</td></tr>'}</tbody></table></div>
<h2>Playbook now</h2><div class="card"><table><thead><tr><th>Id</th><th>Status</th><th class="n">Trades</th><th class="n">R</th>
<th class="n">Helpful/harmful</th><th>Rule</th><th>Origin</th></tr></thead><tbody>{''.join(rule_rows)}</tbody></table></div>
<h2>How the playbook evolved</h2><div class="card"><table><thead><tr><th>Day</th><th>Change</th><th>Rule</th><th>Detail</th></tr></thead>
<tbody>{''.join(hist_rows) or '<tr><td colspan=4>no changes</td></tr>'}</tbody></table></div>
<h2>Daily journal</h2>{''.join(days_detail)}
<script>
const pts = {json.dumps(curve)};
const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
if (window.Chart && pts.length) new Chart(document.getElementById("eq"), {{type: "line",
  data: {{datasets: [{{label: "Equity", data: pts, borderColor: css("--accent"), pointRadius: 0, borderWidth: 2, stepped: true}}]}},
  options: {{responsive: true, maintainAspectRatio: false, animation: false, plugins: {{legend: {{display: false}}}},
    scales: {{x: {{type: "linear", ticks: {{color: css("--text-2"), maxTicksLimit: 8,
      callback: (v) => new Date(v).toLocaleDateString(undefined, {{month: "short", day: "numeric"}})}}, grid: {{color: css("--grid")}}}},
      y: {{ticks: {{color: css("--text-2"), callback: (v) => "$" + Math.round(v).toLocaleString()}}, grid: {{color: css("--grid")}}}}}}}}}});
</script></main></body></html>"""


def write(state, memory, path=None):
    history = store.read_jsonl(memory.folder / "playbook_history.jsonl")
    path = path or (store.RUNS / state["id"] / "report.html")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(state, memory.rules, history), encoding="utf-8")
    return path
