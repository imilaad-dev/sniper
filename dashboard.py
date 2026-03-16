#!/opt/polybot/venv/bin/python3
"""
dashboard.py — Sniper Bot Dashboard.
Runs on port 5001 (polybot dashboard is on 5050 via nginx).
Styled to match Milaw HQ (polybot dashboard).
"""

import json
import logging
from collections import deque
from pathlib import Path
from datetime import datetime, timezone
from flask import Flask, jsonify, render_template_string

log = logging.getLogger(__name__)

DATA_DIR    = Path("/opt/sniper/data")
STATE_FILE  = DATA_DIR / "state.json"
TRADES_FILE = DATA_DIR / "trades.jsonl"
LOG_FILE    = DATA_DIR / "sniper.log"

app = Flask(__name__)


def _load_state() -> dict:
    if STATE_FILE.exists():
        try: return json.loads(STATE_FILE.read_text())
        except Exception: pass
    return {}


def _load_trades(max_lines: int = 2000) -> list:
    """Load trades from JSONL. Reads last max_lines for efficiency."""
    if not TRADES_FILE.exists():
        return []
    trades = []
    with open(TRADES_FILE) as f:
        lines = deque(f, maxlen=max_lines)
    for line in lines:
        try: trades.append(json.loads(line))
        except Exception: pass
    return trades


@app.route("/api/stats")
def api_stats():
    state = _load_state()
    trades = _load_trades()
    outcomes = [t for t in trades if t.get("type") == "OUTCOME"]
    bets = [t for t in trades if t.get("type") == "BET"]
    misses = [t for t in trades if t.get("type") == "MISS"]

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    today_outcomes = [o for o in outcomes if o.get("timestamp", "").startswith(today)]
    today_misses = [m for m in misses if m.get("timestamp", "").startswith(today)]

    wins_today = sum(1 for o in today_outcomes if o.get("pnl_usdc", 0) > 0)
    losses_today = sum(1 for o in today_outcomes if o.get("pnl_usdc", 0) < 0)
    pnl_today = sum(o.get("pnl_usdc", 0) for o in today_outcomes)

    total_wins = state.get("total_wins", 0)
    total_losses = state.get("total_losses", 0)
    total = total_wins + total_losses
    wr = (total_wins / total * 100) if total > 0 else 0

    # Fill rate: bets / (bets + misses)
    total_bets = state.get("total_bets", 0)
    total_misses = state.get("total_misses", len(misses))
    total_attempts = total_bets + total_misses
    fill_rate = round((total_bets / total_attempts * 100), 1) if total_attempts > 0 else 0

    # Avg profit per trade
    avg_profit = round(state.get("pnl_usdc", 0) / total, 4) if total > 0 else 0

    # Build trade_id → timeframe map from BET entries
    tf_map = {b.get("trade_id"): b.get("timeframe", "5m") for b in bets}

    # Recent trades (last 50 outcomes)
    recent = []
    for o in outcomes[-50:]:
        recent.append({
            "time": o.get("timestamp", "")[:19].replace("T", " "),
            "asset": o.get("asset", "?").upper(),
            "side": o.get("side", "?"),
            "odds": o.get("odds", 0),
            "pnl": round(o.get("pnl_usdc", 0), 4),
            "result": o.get("result", "?"),
            "shares": round(o.get("shares", 0), 2),
            "tf": o.get("timeframe") or tf_map.get(o.get("trade_id"), "?"),
        })
    recent.reverse()

    return jsonify({
        "bankroll": round(state.get("bankroll_usdc", 0), 2),
        "total_deposited": round(state.get("total_deposited", 0), 2),
        "pnl_total": round(state.get("pnl_usdc", 0), 2),
        "pnl_today": round(pnl_today, 4),
        "total_bets": total_bets,
        "total_wins": total_wins,
        "total_losses": total_losses,
        "total_misses": total_misses,
        "misses_today": len(today_misses),
        "win_rate": round(wr, 1),
        "fill_rate": fill_rate,
        "avg_profit": avg_profit,
        "wins_today": wins_today,
        "losses_today": losses_today,
        "open_positions": len(state.get("open_positions", [])),
        "pending_redemptions": len(state.get("pending_redemptions", [])),
        "recent": recent,
    })


@app.route("/api/logs")
def api_logs():
    lines = []
    if LOG_FILE.exists():
        with open(LOG_FILE) as f:
            lines = [l.rstrip() for l in deque(f, maxlen=30)]
    return jsonify({"lines": lines})


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Milaw HQ — SNIPER</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Inter:wght@400;600;700&display=swap');

  * { margin: 0; padding: 0; box-sizing: border-box; }

  :root {
    --bg: #0a0a0f;
    --card: rgba(15, 15, 25, 0.9);
    --border: rgba(0, 255, 100, 0.12);
    --green: #00ff64;
    --green-dim: rgba(0, 255, 100, 0.6);
    --red: #ff4444;
    --cyan: #00d4ff;
    --yellow: #ffaa00;
    --purple: #bb88ff;
    --text: #e0e0e0;
    --text-dim: rgba(255, 255, 255, 0.4);
    --mono: 'JetBrains Mono', monospace;
    --sans: 'Inter', sans-serif;
  }

  /* Light theme */
  [data-theme="light"] {
    --bg: #f0f2f5;
    --card: rgba(255, 255, 255, 0.95);
    --border: rgba(0, 80, 40, 0.12);
    --green: #00a843;
    --green-dim: rgba(0, 168, 67, 0.6);
    --red: #d32f2f;
    --cyan: #0077b6;
    --yellow: #c67c00;
    --purple: #7744cc;
    --text: #1a1a2e;
    --text-dim: rgba(0, 0, 0, 0.45);
  }
  [data-theme="light"] .header {
    background: rgba(255, 255, 255, 0.95);
    border-bottom-color: rgba(0, 80, 40, 0.15);
  }
  [data-theme="light"] .header h1 { color: #1a1a2e; text-shadow: none; }
  [data-theme="light"] body::before {
    background:
      linear-gradient(rgba(0, 80, 40, 0.04) 1px, transparent 1px),
      linear-gradient(90deg, rgba(0, 80, 40, 0.04) 1px, transparent 1px);
    background-size: 50px 50px;
  }
  [data-theme="light"] .card:hover { border-color: rgba(0, 80, 40, 0.3); }
  [data-theme="light"] .card-title { color: rgba(0, 0, 0, 0.55); }
  [data-theme="light"] #logTail {
    background: rgba(240, 242, 245, 0.8) !important;
    border-color: var(--border) !important;
    color: rgba(0, 0, 0, 0.5) !important;
  }
  [data-theme="light"] .status-running {
    background: rgba(0, 168, 67, 0.1);
    border-color: rgba(0, 168, 67, 0.3);
  }
  [data-theme="light"] .badge-win { background: rgba(0, 168, 67, 0.12); }
  [data-theme="light"] .badge-loss { background: rgba(211, 47, 47, 0.12); }
  [data-theme="light"] .data-table td { border-bottom-color: rgba(0, 0, 0, 0.06); }
  [data-theme="light"] .data-table tr:hover td { background: rgba(0, 168, 67, 0.04); }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--sans);
    min-height: 100vh;
  }

  body::before {
    content: '';
    position: fixed;
    inset: 0;
    background:
      linear-gradient(rgba(0,255,100,0.02) 1px, transparent 1px),
      linear-gradient(90deg, rgba(0,255,100,0.02) 1px, transparent 1px);
    background-size: 50px 50px;
    z-index: 0;
    pointer-events: none;
  }

  /* Header */
  .header {
    position: sticky;
    top: 0;
    z-index: 100;
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 16px 24px;
    border-bottom: 1px solid var(--border);
    background: rgba(10, 10, 15, 0.95);
    backdrop-filter: blur(10px);
  }

  .header h1 {
    font-family: var(--mono);
    font-size: 22px;
    font-weight: 700;
    color: #fff;
    text-shadow: 0 0 10px rgba(0,255,100,0.5), 0 0 30px rgba(0,255,100,0.2);
    letter-spacing: 3px;
    margin: 0;
  }

  .header .dot {
    display: inline-block;
    width: 8px;
    height: 8px;
    background: var(--green);
    border-radius: 50%;
    margin-left: 8px;
    animation: pulse 2s ease-in-out infinite;
    box-shadow: 0 0 10px rgba(0,255,100,0.5);
    vertical-align: middle;
  }

  .header-right {
    display: flex;
    align-items: center;
    gap: 16px;
    font-family: var(--mono);
    font-size: 12px;
  }

  .status-badge {
    padding: 4px 12px;
    border-radius: 20px;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 1px;
    text-transform: uppercase;
  }

  .status-running { background: rgba(0,255,100,0.15); color: var(--green); border: 1px solid rgba(0,255,100,0.3); }

  .mode-badge {
    font-family: var(--mono);
    font-size: 11px;
    letter-spacing: 1px;
    padding: 3px 8px;
    border: 1px solid var(--border);
    border-radius: 4px;
    color: var(--purple);
  }

  .btn-theme {
    background: transparent;
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 6px 8px;
    color: var(--text-dim);
    cursor: pointer;
    transition: all 0.3s;
    display: flex;
    align-items: center;
  }
  .btn-theme:hover { border-color: var(--green); color: var(--green); }

  .ticker {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--text-dim);
  }

  /* Main grid */
  .grid {
    position: relative;
    z-index: 1;
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 16px;
    padding: 20px 24px;
    max-width: 1200px;
    margin: 0 auto;
  }

  /* Cards */
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 20px;
    backdrop-filter: blur(10px);
    transition: border-color 0.3s;
    min-width: 0;
    overflow: hidden;
  }

  .card:hover { border-color: rgba(0,255,100,0.25); }

  .card-title {
    font-family: var(--mono);
    font-size: 12px;
    color: var(--text-dim);
    letter-spacing: 2px;
    text-transform: uppercase;
    margin-bottom: 8px;
  }

  .card-value {
    font-family: var(--mono);
    font-size: 28px;
    font-weight: 700;
  }

  .card-sub {
    font-family: var(--mono);
    font-size: 12px;
    color: var(--text-dim);
    margin-top: 4px;
  }

  .green { color: var(--green); }
  .red { color: var(--red); }
  .cyan { color: var(--cyan); }
  .yellow { color: var(--yellow); }

  .span-2 { grid-column: span 2; }
  .span-4 { grid-column: span 4; }

  /* Tables */
  .data-table {
    width: 100%;
    border-collapse: collapse;
    margin-top: 12px;
    font-family: var(--mono);
    font-size: 12px;
  }

  .data-table th {
    text-align: left;
    font-size: 10px;
    color: var(--text-dim);
    letter-spacing: 1px;
    text-transform: uppercase;
    padding: 8px 10px;
    border-bottom: 1px solid var(--border);
  }

  .data-table td {
    padding: 8px 10px;
    border-bottom: 1px solid rgba(255,255,255,0.04);
  }

  .data-table tr:hover td { background: rgba(0,255,100,0.03); }

  .badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 10px;
    font-weight: 700;
  }

  .badge-win { background: rgba(0,255,100,0.15); color: var(--green); }
  .badge-loss { background: rgba(255,68,68,0.15); color: var(--red); }

  /* Log tail */
  #logTail {
    background: rgba(0,0,0,0.4);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px;
    font-family: var(--mono);
    font-size: 11px;
    line-height: 1.6;
    max-height: 260px;
    overflow-y: auto;
    color: var(--text-dim);
    white-space: pre;
    word-wrap: break-word;
  }

  .log-snipe { color: var(--purple); font-weight: 700; }
  .log-win { color: var(--green); font-weight: 700; }
  .log-loss { color: var(--red); font-weight: 700; }
  .log-miss { color: var(--yellow); }
  .log-candidate { color: var(--cyan); }

  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.6; }
  }

  /* Responsive */
  @media (max-width: 1000px) {
    .grid { grid-template-columns: repeat(2, 1fr); }
    .span-4 { grid-column: span 2; }
  }

  @media (max-width: 768px) {
    .grid { grid-template-columns: 1fr; padding: 8px; gap: 10px; }
    .span-2, .span-4 { grid-column: span 1; }
    .card { padding: 14px; border-radius: 10px; }
    .card-value { font-size: 22px; }
    .header { flex-direction: column; gap: 10px; text-align: center; padding: 12px 16px; }
    .header h1 { font-size: 18px; letter-spacing: 2px; }
    .header-right { flex-wrap: wrap; justify-content: center; gap: 10px; }
    .data-table { display: block; overflow-x: auto; -webkit-overflow-scrolling: touch; font-size: 11px; }
    .data-table th, .data-table td { padding: 8px; white-space: nowrap; }
    #logTail { font-size: 10px !important; max-height: 200px !important; padding: 8px !important; }
    .btn-theme { padding: 10px 12px; min-height: 44px; }
  }

  @media (max-width: 400px) {
    .grid { padding: 6px; gap: 8px; }
    .card { padding: 10px; }
    .card-value { font-size: 20px; }
    .header h1 { font-size: 16px; }
    .header { padding: 10px 12px; }
  }
</style>
</head>
<body>

<div class="header">
  <div style="display:flex;align-items:center;gap:12px">
    <a href="/" style="color:var(--text-dim);text-decoration:none;font-size:18px;transition:color 0.3s" title="Back to Hub">&larr;</a>
    <h1>MILAW HQ<span class="dot"></span></h1>
    <span class="mode-badge">SNIPER</span>
  </div>
  <div class="header-right">
    <span class="ticker" id="lastUpdated"></span>
    <span class="status-badge status-running" id="statusBadge">LIVE</span>
    <button id="themeToggle" class="btn-theme" title="Toggle light/dark mode" aria-label="Toggle theme">
      <svg id="themeIcon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>
      </svg>
    </button>
    <a href="/logout" style="color:var(--text-dim);text-decoration:none;font-family:var(--mono);font-size:11px;letter-spacing:1px;transition:color 0.3s" onmouseover="this.style.color='var(--red)'" onmouseout="this.style.color='var(--text-dim)'">LOGOUT</a>
  </div>
</div>

<div class="grid">
  <!-- Row 1: Stats cards -->
  <div class="card">
    <div class="card-title">Bankroll</div>
    <div class="card-value" id="bankroll">—</div>
    <div class="card-sub" id="deposited"></div>
  </div>
  <div class="card">
    <div class="card-title">PnL (Total)</div>
    <div class="card-value" id="pnlTotal">—</div>
    <div class="card-sub" id="pnlToday"></div>
  </div>
  <div class="card">
    <div class="card-title">Win Rate</div>
    <div class="card-value" id="winRate">—</div>
    <div class="card-sub" id="wlCount"></div>
  </div>
  <div class="card">
    <div class="card-title">Fill Rate</div>
    <div class="card-value" id="fillRate">—</div>
    <div class="card-sub" id="missCount"></div>
  </div>

  <!-- Row 2: Secondary stats -->
  <div class="card">
    <div class="card-title">Today</div>
    <div class="card-value" id="todayWL">—</div>
    <div class="card-sub" id="openPos"></div>
  </div>
  <div class="card">
    <div class="card-title">Avg Profit</div>
    <div class="card-value" id="avgProfit">—</div>
    <div class="card-sub" id="totalBets"></div>
  </div>
  <div class="card span-2">
    <div class="card-title">Status</div>
    <div class="card-value" id="pendingRedeem" style="font-size:18px">—</div>
  </div>

  <!-- Row 2: Recent trades -->
  <div class="card span-4">
    <div class="card-title">Recent Trades</div>
    <table class="data-table">
      <thead><tr><th>Time</th><th>Asset</th><th>TF</th><th>Side</th><th>Odds</th><th>Shares</th><th>Result</th><th>PnL</th></tr></thead>
      <tbody id="recentBody"></tbody>
    </table>
  </div>

  <!-- Row 3: Live log -->
  <div class="card span-4">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
      <div class="card-title" style="margin-bottom:0">Live Log</div>
      <span class="ticker" id="logTicker"></span>
    </div>
    <div id="logTail">Loading...</div>
  </div>
</div>

<footer style="text-align:center; padding:40px 24px 24px; max-width:1200px; margin:0 auto; color:var(--text-dim); font-size:11px; font-family:var(--mono); letter-spacing:0.5px;">
  Built with love by Milad & Claude &mdash; London, 2026
</footer>

<script>
function fmt(n) { return n >= 0 ? '+$' + n.toFixed(2) : '-$' + Math.abs(n).toFixed(2); }

function colorLine(line) {
  const esc = line.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  if (/SNIPE:/.test(line)) return '<span class="log-snipe">' + esc + '</span>';
  if (/\bWIN\b/.test(line)) return '<span class="log-win">' + esc + '</span>';
  if (/\bLOSS\b/.test(line)) return '<span class="log-loss">' + esc + '</span>';
  if (/MISS:/.test(line)) return '<span class="log-miss">' + esc + '</span>';
  if (/CANDIDATE:/.test(line)) return '<span class="log-candidate">' + esc + '</span>';
  return esc;
}

// Theme toggle
const themeBtn = document.getElementById('themeToggle');
const sunSvg = '<circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>';
const moonSvg = '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>';

function setTheme(t) {
  document.documentElement.setAttribute('data-theme', t);
  localStorage.setItem('polybot-theme', t);
  document.getElementById('themeIcon').innerHTML = t === 'light' ? sunSvg : moonSvg;
}
themeBtn.addEventListener('click', function() {
  setTheme(document.documentElement.getAttribute('data-theme') === 'light' ? 'dark' : 'light');
});
setTheme(localStorage.getItem('polybot-theme') || 'dark');

async function refresh() {
  try {
    const base = window.location.pathname.replace(/\/$/, '');
    const res = await fetch(base + '/api/stats');
    const d = await res.json();

    document.getElementById('bankroll').textContent = '$' + d.bankroll.toFixed(2);
    document.getElementById('deposited').textContent = 'Deposited: $' + d.total_deposited.toFixed(2);

    const pt = document.getElementById('pnlTotal');
    pt.textContent = fmt(d.pnl_total);
    pt.className = 'card-value ' + (d.pnl_total >= 0 ? 'green' : 'red');

    const pd = document.getElementById('pnlToday');
    pd.textContent = 'Today: ' + fmt(d.pnl_today);
    pd.className = 'card-sub ' + (d.pnl_today >= 0 ? 'green' : 'red');

    document.getElementById('winRate').textContent = d.win_rate.toFixed(1) + '%';
    document.getElementById('wlCount').textContent = d.total_wins + 'W / ' + d.total_losses + 'L';

    document.getElementById('fillRate').textContent = d.fill_rate.toFixed(1) + '%';
    document.getElementById('missCount').textContent = d.total_bets + ' fills / ' + d.total_misses + ' misses';

    document.getElementById('todayWL').innerHTML =
      '<span class="green">' + d.wins_today + 'W</span> <span style="color:var(--text-dim)">/</span> <span class="red">' + d.losses_today + 'L</span> <span style="color:var(--text-dim)">/</span> <span class="yellow">' + d.misses_today + 'M</span>';
    document.getElementById('openPos').textContent = d.open_positions + ' open position' + (d.open_positions !== 1 ? 's' : '');

    const ap = document.getElementById('avgProfit');
    ap.textContent = fmt(d.avg_profit);
    ap.className = 'card-value ' + (d.avg_profit >= 0 ? 'green' : 'red');
    document.getElementById('totalBets').textContent = d.total_bets + ' resolved trades';

    var statusParts = [];
    if (d.pending_redemptions > 0) statusParts.push('<span class="yellow">' + d.pending_redemptions + ' pending redeem' + (d.pending_redemptions !== 1 ? 's' : '') + '</span>');
    else statusParts.push('<span class="green">No pending redemptions</span>');
    statusParts.push('<span style="color:var(--text-dim)">' + d.open_positions + ' open | ' + d.misses_today + ' misses today</span>');
    document.getElementById('pendingRedeem').innerHTML = statusParts.join('<br>');

    document.getElementById('lastUpdated').textContent = 'updated ' + new Date().toLocaleTimeString();

    // Status badge — reflect actual data freshness
    const sb = document.getElementById('statusBadge');
    if (d.total_bets > 0) {
      sb.textContent = 'LIVE'; sb.className = 'status-badge status-running';
    }

    const rb = document.getElementById('recentBody');
    if (!d.recent || d.recent.length === 0) {
      rb.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--text-dim);padding:24px">No trades yet</td></tr>';
    } else {
    rb.innerHTML = (d.recent || []).map(function(r) {
      const won = r.pnl > 0;
      const cls = won ? 'green' : 'red';
      const badge = won ? '<span class="badge badge-win">WIN</span>' : '<span class="badge badge-loss">LOSS</span>';
      var tfCls = r.tf === '1h' ? 'purple' : r.tf === '15m' ? 'cyan' : '';
      return '<tr>' +
        '<td>' + r.time.slice(5) + '</td>' +
        '<td><b>' + r.asset + '</b></td>' +
        '<td class="' + tfCls + '">' + (r.tf || '?') + '</td>' +
        '<td>' + r.side + '</td>' +
        '<td>' + r.odds.toFixed(2) + '</td>' +
        '<td>' + r.shares + '</td>' +
        '<td>' + badge + '</td>' +
        '<td class="' + cls + '">' + fmt(r.pnl) + '</td></tr>';
    }).join('');
    }
  } catch(e) {}
}

async function refreshLogs() {
  try {
    const base = window.location.pathname.replace(/\/$/, '');
    const res = await fetch(base + '/api/logs');
    const data = await res.json();
    const el = document.getElementById('logTail');
    el.innerHTML = (data.lines || []).map(colorLine).join('\n');
    el.scrollTop = el.scrollHeight;
    document.getElementById('logTicker').textContent = 'updated ' + new Date().toLocaleTimeString();
  } catch(e) {}
}

refresh();
refreshLogs();
setInterval(refresh, 5000);
setInterval(refreshLogs, 5000);
</script>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(TEMPLATE)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=False)
