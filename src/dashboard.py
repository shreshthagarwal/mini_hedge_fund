"""
dashboard.py — Flask dashboard for live paper trading

Serves a single-page dashboard that polls /api/state every 2 seconds.
Shows: live price, session P&L, open position, trade log, activity log.

Run:
    python main.py paper
Then open: http://localhost:5000
"""

import os
import json
from flask import Flask, jsonify, render_template_string
from utils import get_logger

logger = get_logger("dashboard")

DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>NIFTY Paper Trading</title>

<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Courier New', monospace; background: #0d1117; color: #c9d1d9; padding: 20px; }
  h1 { font-size: 18px; color: #58a6ff; margin-bottom: 20px; letter-spacing: 2px; }
  .grid { display: grid; grid-template-columns: 1fr 1fr 1fr 1fr; gap: 12px; margin-bottom: 20px; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; }
  .card .label { font-size: 11px; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 6px; }
  .card .value { font-size: 22px; font-weight: bold; }
  .green { color: #3fb950; } .red { color: #f85149; } .blue { color: #58a6ff; } .amber { color: #d29922; }
  .section { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; margin-bottom: 12px; }
  .section h2 { font-size: 12px; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 12px; }
  .position-box { background: #0d1117; border-radius: 6px; padding: 12px; }
  .pos-row { display: flex; justify-content: space-between; padding: 4px 0; font-size: 13px; border-bottom: 1px solid #21262d; }
  .pos-row:last-child { border-bottom: none; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; padding: 6px 8px; font-size: 11px; color: #8b949e; border-bottom: 1px solid #30363d; }
  td { padding: 6px 8px; border-bottom: 1px solid #21262d; }
  .log-box { background: #0d1117; border-radius: 6px; padding: 10px; max-height: 200px; overflow-y: auto; font-size: 12px; }
  .log-line { padding: 2px 0; color: #8b949e; }
  .log-line.entry { color: #58a6ff; }
  .log-line.exit-win { color: #3fb950; }
  .log-line.exit-loss { color: #f85149; }
  #status-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #f85149; margin-right: 6px; }
  #status-dot.running { background: #3fb950; animation: pulse 1.5s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
  .no-pos { color: #8b949e; font-size: 13px; text-align: center; padding: 20px; }
  .two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
</style>
</head>
<body>
<h1><span id="status-dot"></span>NIFTY FUTURES — PAPER TRADING</h1>

<div class="grid">
  <div class="card">
    <div class="label">Live price</div>
    <div class="value blue" id="price">--</div>
  </div>
  <div class="card">
    <div class="label">Session P&L</div>
    <div class="value" id="session-pnl">₹0</div>
  </div>
  <div class="card">
    <div class="label">Capital</div>
    <div class="value" id="capital">--</div>
  </div>
  <div class="card">
    <div class="label">Win rate</div>
    <div class="value" id="win-rate">--%</div>
  </div>
</div>

<div class="two-col">
  <div>
    <div class="section">
      <h2>Open position</h2>
      <div id="open-position"><div class="no-pos">No open position</div></div>
    </div>

    <div class="section">
      <h2>Activity log</h2>
      <div class="log-box" id="log-box"></div>
    </div>
  </div>

  <div class="section" style="height:fit-content">
    <h2>Today's trades</h2>
    <table>
      <thead>
        <tr><th>Time</th><th>Dir</th><th>Entry</th><th>Exit</th><th>P&L</th><th>Reason</th></tr>
      </thead>
      <tbody id="trades-body"></tbody>
    </table>
    <div id="no-trades" style="color:#8b949e;font-size:13px;text-align:center;padding:20px">No trades yet</div>
  </div>
</div>

<script>
function fmt(n) { return '₹' + n.toLocaleString('en-IN'); }

async function refresh() {
  try {
    const r = await fetch('/api/state');
    const d = await r.json();

    // Status dot
    const dot = document.getElementById('status-dot');
    dot.className = d.status === 'running' ? 'running' : '';

    // Cards
    document.getElementById('price').textContent = d.current_price ? d.current_price.toFixed(2) : '--';
    const pnl = d.session_pnl;
    const pnlEl = document.getElementById('session-pnl');
    pnlEl.textContent = fmt(pnl);
    pnlEl.className = 'value ' + (pnl >= 0 ? 'green' : 'red');
    document.getElementById('capital').textContent = fmt(d.capital);
    const wr = document.getElementById('win-rate');
    wr.textContent = d.win_rate + '%';
    wr.className = 'value ' + (d.win_rate >= 40 ? 'green' : 'amber');

    // Open position
    const posEl = document.getElementById('open-position');
    if (d.open_trade) {
      const t = d.open_trade;
      const uPnl = t.unrealised;
      const col = uPnl >= 0 ? 'green' : 'red';
      posEl.innerHTML = `
        <div class="position-box">
          <div class="pos-row"><span>${t.direction}</span><span class="blue">${t.entry_time}</span></div>
          <div class="pos-row"><span>Entry</span><span>${t.entry_price.toFixed(2)}</span></div>
          <div class="pos-row"><span>Stop loss</span><span class="red">${t.sl.toFixed(2)}</span></div>
          <div class="pos-row"><span>Target</span><span class="green">${t.target.toFixed(2)}</span></div>
          <div class="pos-row"><span>Unrealised</span><span class="${col}">${fmt(uPnl)}</span></div>
        </div>`;
    } else {
      posEl.innerHTML = '<div class="no-pos">No open position</div>';
    }

    // Trades table
    const tbody = document.getElementById('trades-body');
    const noTrades = document.getElementById('no-trades');
    if (d.trades.length > 0) {
      noTrades.style.display = 'none';
      tbody.innerHTML = d.trades.slice().reverse().map(t => {
        const cls = t.pnl >= 0 ? 'green' : 'red';
        return `<tr>
          <td>${t.time}</td>
          <td>${t.direction}</td>
          <td>${t.entry}</td>
          <td>${t.exit || '--'}</td>
          <td class="${cls}">${fmt(t.pnl)}</td>
          <td style="color:#8b949e;font-size:11px">${t.reason}</td>
        </tr>`;
      }).join('');
    } else {
      noTrades.style.display = 'block';
      tbody.innerHTML = '';
    }

    // Log
    const logBox = document.getElementById('log-box');
    logBox.innerHTML = d.log.slice().reverse().map(line => {
      let cls = 'log-line';
      if (line.includes('▲') || line.includes('▼')) cls += ' entry';
      else if (line.includes('✅')) cls += ' exit-win';
      else if (line.includes('❌')) cls += ' exit-loss';
      return `<div class="${cls}">${line}</div>`;
    }).join('');

  } catch(e) { console.error(e); }
}

refresh();  // initial load
setInterval(refresh, 3000);   // poll every 3 seconds
</script>
</body>
</html>
"""


def create_app(engine) -> Flask:
    """Create Flask app with reference to the paper engine."""
    import logging
    log = logging.getLogger("werkzeug")
    log.setLevel(logging.ERROR)   # suppress per-request logs

    app = Flask(__name__)
    app.config["engine"] = engine

    @app.route("/")
    def index():
        return render_template_string(DASHBOARD_HTML)

    @app.route("/api/state")
    def state():
        return jsonify(engine.get_state())

    return app