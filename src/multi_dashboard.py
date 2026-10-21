"""
multi_dashboard.py — 5-Strategy Live Dashboard
Clean layout: price hero, intelligence panel, 1-min candlestick chart,
strategy cards (with risk state), trade history, activity feed.
"""

from flask import Flask, jsonify, render_template_string
import logging
import os
logger = logging.getLogger("multi_dashboard")

HTML = """<!DOCTYPE html>
<html class="dark" lang="en">
<head>
<meta charset="utf-8"/>
<meta content="width=device-width, initial-scale=1.0" name="viewport"/>
<script src="https://cdn.tailwindcss.com?plugins=forms,container-queries"></script>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;400;500;600;700;900&family=Manrope:wght@200;300;400;500;600;700;800&family=Inter:wght@100;200;300;400;500;600;700;800;900&display=swap" rel="stylesheet"/>
<link href="https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:wght,FILL@100..700,0..1&display=swap" rel="stylesheet"/>
<script>
tailwind.config={darkMode:"class",theme:{extend:{
  colors:{
    "primary":"#81ecff","primary-container":"#00e3fd","primary-dim":"#00d4ec",
    "on-primary":"#005762","on-primary-container":"#004d57",
    "tertiary":"#ffe792","tertiary-fixed":"#ffd709","tertiary-dim":"#efc900",
    "error":"#ff6e84","surface":"#0a0e14","surface-dim":"#0a0e14",
    "surface-bright":"#262c36","surface-container":"#151a21",
    "surface-container-low":"#0f141a","surface-container-high":"#1b2028",
    "surface-container-highest":"#20262f","surface-container-lowest":"#000000",
    "on-surface":"#f1f3fc","on-surface-variant":"#a8abb3",
    "outline":"#72757d","outline-variant":"#44484f","background":"#0a0e14",
  },
  fontFamily:{"headline":["Space Grotesk"],"body":["Manrope"],"label":["Inter"]},
  borderRadius:{"DEFAULT":"0.25rem","lg":"0.5rem","xl":"0.75rem","full":"9999px"},
}}}
</script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
.glass{background:rgba(32,38,47,.6);backdrop-filter:blur(20px);border:1px solid rgba(114,117,125,.15);box-shadow:inset 0 1px 1px rgba(129,236,255,.08)}
.neo{background:#0f141a;box-shadow:8px 8px 16px rgba(0,0,0,.4),-1px -1px 0 rgba(255,255,255,.05);transition:transform .15s,box-shadow .15s}
.neo:hover{transform:translateY(-2px);box-shadow:10px 10px 20px rgba(0,0,0,.5),-1px -1px 0 rgba(255,255,255,.07)}
.tabular{font-variant-numeric:tabular-nums}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
@keyframes fadein{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
.fadein{animation:fadein .25s ease forwards}
#chart-canvas{cursor:crosshair}
.tooltip{position:fixed;pointer-events:none;background:#1b2028;border:1px solid #44484f;border-radius:6px;padding:8px 12px;font-family:Inter,sans-serif;font-size:11px;color:#f1f3fc;z-index:999;display:none}
</style>
</head>
<body class="bg-surface font-body text-on-surface antialiased">

<!-- Ambient glows -->
<div class="fixed top-0 right-0 w-96 h-96 bg-primary/5 blur-[120px] rounded-full -z-10 pointer-events-none"></div>
<div class="fixed bottom-0 left-20 w-96 h-96 bg-error/3 blur-[150px] rounded-full -z-10 pointer-events-none"></div>

<!-- Top Nav (slim) -->
<header class="sticky top-0 z-40 h-14 px-6 flex justify-between items-center bg-slate-950/50 backdrop-blur-md border-b border-white/5">
  <div class="flex items-center gap-4">
    <span class="text-primary font-headline font-black text-xl tracking-tighter">K</span>
    <div class="h-4 w-px bg-outline-variant/40"></div>
    <span class="text-sm font-label text-on-surface-variant">NIFTY 50 Futures</span>
    <span id="live-badge" class="text-[10px] font-black px-2 py-0.5 rounded-full bg-slate-700 text-slate-400">OFFLINE</span>
  </div>
  <div class="flex items-center gap-6">
    <div class="text-right">
      <div class="text-primary font-headline font-bold tabular text-lg leading-none" id="nav-price">--</div>
      <div class="text-[10px] text-on-surface-variant font-label">Live Price</div>
    </div>
    <div class="h-8 w-px bg-outline-variant/30"></div>
    <div class="text-right">
      <div class="font-headline font-bold tabular text-lg leading-none" id="nav-pnl">₹0</div>
      <div class="text-[10px] text-on-surface-variant font-label">Session P&amp;L</div>
    </div>
    <div class="h-8 w-px bg-outline-variant/30"></div>
    <div class="flex items-center gap-2">
      <span id="status-dot" class="w-2 h-2 rounded-full bg-slate-500"></span>
      <span id="status-text" class="text-xs font-label text-slate-500">Connecting</span>
    </div>
    <span class="text-xs text-on-surface-variant font-label tabular" id="nav-ts">--:--:--</span>
  </div>
</header>

<main class="px-6 py-5 max-w-[1800px] mx-auto space-y-5">

  <!-- Row 1: Hero stats -->
  <section class="grid grid-cols-12 gap-5">

    <!-- Price hero -->
    <div class="col-span-8 glass rounded-xl p-6 relative overflow-hidden">
      <div class="absolute -top-16 -right-16 w-48 h-48 bg-primary/8 blur-[80px] rounded-full pointer-events-none"></div>
      <div class="flex justify-between items-start z-10 relative">
        <div>
          <div class="flex items-baseline gap-3">
            <h2 class="text-5xl font-headline font-black tracking-tighter tabular text-on-surface" id="hero-price">--</h2>
            <span class="text-xl font-bold tabular" id="hero-change">--</span>
          </div>
          <div class="mt-1 text-xs font-label text-on-surface-variant" id="warmup-line">Loading...</div>
        </div>
        <div class="text-right">
          <div class="text-xs uppercase tracking-widest text-on-surface-variant font-bold mb-1">Total Session P&amp;L</div>
          <div class="text-3xl font-headline font-bold tabular" id="hero-pnl">₹0</div>
          <div class="text-xs text-on-surface-variant mt-1" id="hero-trades">0 trades</div>
        </div>
      </div>
      <!-- OHLCV strip -->
      <div class="mt-5 flex flex-wrap gap-6">
        <div><div class="text-[9px] text-outline uppercase font-bold tracking-widest">Open</div><div class="font-headline tabular" id="d-open">--</div></div>
        <div><div class="text-[9px] text-outline uppercase font-bold tracking-widest">High</div><div class="font-headline tabular text-primary" id="d-high">--</div></div>
        <div><div class="text-[9px] text-outline uppercase font-bold tracking-widest">Low</div><div class="font-headline tabular text-error" id="d-low">--</div></div>
        <div class="border-l border-outline-variant/20 pl-6"><div class="text-[9px] text-outline uppercase font-bold tracking-widest">ORB High</div><div class="font-headline tabular text-tertiary-fixed" id="d-orb-h">--</div></div>
        <div><div class="text-[9px] text-outline uppercase font-bold tracking-widest">ORB Low</div><div class="font-headline tabular text-tertiary-dim" id="d-orb-l">--</div></div>
        <div class="border-l border-outline-variant/20 pl-6"><div class="text-[9px] text-outline uppercase font-bold tracking-widest">VWAP</div><div class="font-headline tabular text-on-surface-variant" id="d-vwap">--</div></div>
        <div><div class="text-[9px] text-outline uppercase font-bold tracking-widest">Bars Today</div><div class="font-headline tabular" id="d-bars">0</div></div>
      </div>
    </div>

    <!-- Portfolio overview -->
    <div class="col-span-4 neo rounded-xl p-5 border border-outline-variant/10">
      <div class="text-sm font-headline font-bold text-tertiary mb-4">Portfolio Overview</div>
      <div class="mb-3">
        <div class="flex justify-between text-xs font-label mb-1.5">
          <span class="text-on-surface-variant">Strategies Ready</span>
          <span class="text-primary font-bold" id="ov-active">0 / 5</span>
        </div>
        <div class="h-1.5 bg-surface-container-highest rounded-full overflow-hidden">
          <div id="ov-bar" class="h-full bg-gradient-to-r from-primary to-primary-container transition-all duration-700" style="width:0%"></div>
        </div>
      </div>
      <div class="space-y-2.5 text-xs font-label">
        <div class="flex justify-between"><span class="text-on-surface-variant">Total Capital</span><span class="tabular font-bold" id="ov-cap">--</span></div>
        <div class="flex justify-between"><span class="text-on-surface-variant">Combined P&amp;L</span><span class="tabular font-bold" id="ov-pnl">₹0</span></div>
        <div class="flex justify-between"><span class="text-on-surface-variant">Total Trades</span><span class="tabular" id="ov-n">0</span></div>
        <div class="flex justify-between"><span class="text-on-surface-variant">Win Rate</span><span class="tabular text-primary font-bold" id="ov-wr">--%</span></div>
      </div>
      <div class="mt-4 pt-3 border-t border-outline-variant/10">
        <div class="text-[9px] uppercase tracking-widest text-on-surface-variant font-bold mb-1">Status</div>
        <div class="text-xs font-label text-on-surface-variant leading-relaxed" id="ov-detail">Initialising...</div>
      </div>
    </div>
  </section>

  <!-- Intelligence Layer Status -->
  <section class="glass rounded-xl p-4" id="intel-panel">
    <div class="flex items-center justify-between flex-wrap gap-4">
      <div class="flex items-center gap-4">
        <span class="material-symbols-outlined text-tertiary-fixed">psychology</span>
        <div>
          <div class="text-[10px] uppercase tracking-widest text-on-surface-variant font-bold mb-0.5">Intelligence Layer</div>
          <div class="font-headline font-bold text-sm" id="intel-regime">--</div>
        </div>
        <div class="h-8 w-px bg-outline-variant/30"></div>
        <div>
          <div class="text-[10px] uppercase tracking-widest text-on-surface-variant font-bold mb-0.5">Confidence Score</div>
          <div class="font-headline font-bold text-sm" id="intel-score">--</div>
        </div>
        <div class="h-8 w-px bg-outline-variant/30"></div>
        <div>
          <div class="text-[10px] uppercase tracking-widest text-on-surface-variant font-bold mb-0.5">Gate</div>
          <div class="font-headline font-bold text-sm" id="intel-gate">--</div>
        </div>
        <div class="h-8 w-px bg-outline-variant/30"></div>
        <div>
          <div class="text-[10px] uppercase tracking-widest text-on-surface-variant font-bold mb-0.5">Best Strategies Now</div>
          <div class="font-headline font-bold text-sm" id="intel-best">--</div>
        </div>
      </div>
      <div class="text-xs font-label text-on-surface-variant max-w-md" id="intel-reason"></div>
    </div>
  </section>

  <!-- Row 2: Candlestick Chart -->
  <section class="glass rounded-xl overflow-hidden">
    <div class="flex items-center justify-between px-5 py-3 border-b border-outline-variant/10">
      <div class="flex items-center gap-4">
        <span class="font-headline font-bold text-sm">NIFTY 50 FUTURES — 1 MIN</span>
        <div class="flex items-center gap-3 text-xs font-label">
          <span class="flex items-center gap-1.5"><span class="w-3 h-2 rounded-sm inline-block" style="background:#374151"></span>Before bot start</span>
          <span class="flex items-center gap-1.5"><span class="w-3 h-2 rounded-sm inline-block" style="background:#81ecff"></span>After bot start</span>
          <span class="flex items-center gap-1.5 text-error"><span class="w-3 h-2 rounded-sm inline-block" style="background:#ff6e84"></span>Bear candle</span>
        </div>
      </div>
      <div class="flex items-center gap-3 text-xs font-label text-on-surface-variant" id="chart-crosshair">
        Hover over chart
      </div>
    </div>
    <div class="relative" style="height:280px">
      <canvas id="chart-canvas" style="width:100%;height:100%"></canvas>
    </div>
    <!-- Volume bars row -->
    <div class="relative border-t border-outline-variant/5" style="height:50px">
      <canvas id="vol-canvas" style="width:100%;height:100%"></canvas>
    </div>
  </section>

  <!-- Row 3: Strategy cards -->
  <section>
    <div class="flex items-center justify-between mb-4">
      <h3 class="font-headline font-black text-xl tracking-tight">Active Strategies</h3>
      <span class="text-xs text-on-surface-variant font-label" id="last-update">--</span>
    </div>
    <div class="grid grid-cols-5 gap-4" id="strat-cards"></div>
  </section>

  <!-- Row 4: Trades + Logs -->
  <section class="grid grid-cols-2 gap-5">

    <!-- Trade History -->
    <div class="glass rounded-xl overflow-hidden">
      <div class="flex items-center justify-between px-5 py-3 border-b border-outline-variant/10">
        <span class="font-headline font-bold text-sm">Trade History</span>
        <div class="flex gap-1" id="trade-filters">
          <button onclick="setTradeFilter('ALL')" data-k="ALL" class="tf-btn px-2.5 py-1 rounded-lg bg-primary/20 text-primary text-[10px] font-black uppercase">All</button>
          <button onclick="setTradeFilter('S1')"  data-k="S1"  class="tf-btn px-2.5 py-1 rounded-lg bg-surface-container-high text-on-surface-variant text-[10px] font-black uppercase">S1</button>
          <button onclick="setTradeFilter('S2')"  data-k="S2"  class="tf-btn px-2.5 py-1 rounded-lg bg-surface-container-high text-on-surface-variant text-[10px] font-black uppercase">S2</button>
          <button onclick="setTradeFilter('S3')"  data-k="S3"  class="tf-btn px-2.5 py-1 rounded-lg bg-surface-container-high text-on-surface-variant text-[10px] font-black uppercase">S3</button>
          <button onclick="setTradeFilter('S4')"  data-k="S4"  class="tf-btn px-2.5 py-1 rounded-lg bg-surface-container-high text-on-surface-variant text-[10px] font-black uppercase">S4</button>
          <button onclick="setTradeFilter('S5')"  data-k="S5"  class="tf-btn px-2.5 py-1 rounded-lg bg-surface-container-high text-on-surface-variant text-[10px] font-black uppercase">S5</button>
        </div>
      </div>
      <div class="overflow-x-auto">
        <table class="w-full border-collapse text-xs font-label">
          <thead><tr class="bg-surface-container-high/40 text-[10px] font-black uppercase tracking-widest text-on-surface-variant">
            <th class="px-4 py-2.5 text-left">Strat</th><th class="px-4 py-2.5 text-left">Time</th>
            <th class="px-4 py-2.5 text-left">Dir</th><th class="px-4 py-2.5 text-left">Entry</th>
            <th class="px-4 py-2.5 text-left">Exit</th><th class="px-4 py-2.5 text-right">P&amp;L</th>
            <th class="px-4 py-2.5 text-left">Reason</th>
          </tr></thead>
          <tbody id="trades-body" class="divide-y divide-outline-variant/5"></tbody>
        </table>
      </div>
      <div id="no-trades" class="py-10 text-center text-on-surface-variant text-xs">No trades yet today</div>
    </div>

    <!-- Activity Feed -->
    <div class="glass rounded-xl overflow-hidden flex flex-col">
      <div class="flex items-center justify-between px-5 py-3 border-b border-outline-variant/10">
        <span class="font-headline font-bold text-sm">Activity Feed</span>
        <div class="flex gap-1" id="log-tabs">
          <button onclick="setLogFilter('ALL')" data-k="ALL" class="lt-btn px-2.5 py-1 rounded-lg bg-primary/20 text-primary text-[10px] font-black uppercase">All</button>
          <button onclick="setLogFilter('S1')"  data-k="S1"  class="lt-btn px-2.5 py-1 rounded-lg bg-surface-container-high text-on-surface-variant text-[10px] font-black uppercase">S1</button>
          <button onclick="setLogFilter('S2')"  data-k="S2"  class="lt-btn px-2.5 py-1 rounded-lg bg-surface-container-high text-on-surface-variant text-[10px] font-black uppercase">S2</button>
          <button onclick="setLogFilter('S3')"  data-k="S3"  class="lt-btn px-2.5 py-1 rounded-lg bg-surface-container-high text-on-surface-variant text-[10px] font-black uppercase">S3</button>
          <button onclick="setLogFilter('S4')"  data-k="S4"  class="lt-btn px-2.5 py-1 rounded-lg bg-surface-container-high text-on-surface-variant text-[10px] font-black uppercase">S4</button>
          <button onclick="setLogFilter('S5')"  data-k="S5"  class="lt-btn px-2.5 py-1 rounded-lg bg-surface-container-high text-on-surface-variant text-[10px] font-black uppercase">S5</button>
        </div>
      </div>
      <div id="log-feed" class="flex-1 overflow-y-auto p-4 space-y-0.5 max-h-64 text-[11px] font-label"></div>
    </div>

  </section>
</main>

<!-- Tooltip -->
<div class="tooltip" id="tt"></div>

<script>
// ── Constants ─────────────────────────────────────────────────────────────────
const KEYS   = ["S1","S2","S3","S4","S5"];
const LABELS = {S1:"ORB + ADX",S2:"VWAP M.R.",S3:"Candle SR",S4:"RL Scalper",S5:"Basis Arb"};
const ICONS  = {S1:"bolt",S2:"auto_graph",S3:"candlestick_chart",S4:"psychology",S5:"account_balance"};
const IC     = {S1:"text-primary",S2:"text-tertiary",S3:"text-error",S4:"text-tertiary-fixed",S5:"text-primary-container"};
const TYPES  = {S1:"Breakout",S2:"Mean Rev",S3:"Pattern",S4:"AI Neural",S5:"Arbitrage"};

// ── State ─────────────────────────────────────────────────────────────────────
let tradeFilter = "ALL", logFilter = "ALL";
let allTrades = [], allLogs = [];
let sparkData = {S1:[],S2:[],S3:[],S4:[],S5:[]};
let chartData = [];   // 1-min candles for chart

// ── Formatters ────────────────────────────────────────────────────────────────
const fmtINR = n => {
  const s = Math.abs(n), p = n >= 0;
  const str = s>=100000?(s/100000).toFixed(2)+'L':s>=1000?(s/1000).toFixed(1)+'k':s.toFixed(0);
  return (p?'+₹':'-₹')+str;
};
const fmtFull = n => '₹'+Math.abs(n).toLocaleString('en-IN');
const col  = n => n >= 0 ? 'text-primary' : 'text-error';
const scls = s => ({
  long:    'bg-primary/15 text-primary border-primary/30',
  short:   'bg-error/15 text-error border-error/30',
  flat:    'bg-surface-container-highest text-on-surface-variant border-outline-variant/20',
  waiting: 'bg-tertiary-fixed/10 text-tertiary-fixed border-tertiary-fixed/20',
}[s] || 'bg-surface-container-highest text-on-surface-variant border-outline-variant/20');

// ── Candlestick chart ─────────────────────────────────────────────────────────
const WARMUP_BULL = '#374151';
const WARMUP_BEAR = '#1f2937';
const LIVE_BULL   = '#81ecff';
const LIVE_BEAR   = '#ff6e84';
const WICK_WARM   = '#4b5563';
const WICK_LIVE_B = '#81ecff';
const WICK_LIVE_R = '#ff6e84';

let chartDPR = window.devicePixelRatio || 1;

function resizeCanvas(canvas) {
  const rect = canvas.parentElement.getBoundingClientRect();
  canvas.width        = rect.width  * chartDPR;
  canvas.height       = rect.height * chartDPR;
  canvas.style.width  = rect.width  + 'px';
  canvas.style.height = rect.height + 'px';
}

function drawChart(candles) {
  if (!candles || candles.length === 0) return;
  const canvas = document.getElementById('chart-canvas');
  const volCv  = document.getElementById('vol-canvas');
  resizeCanvas(canvas);
  resizeCanvas(volCv);

  const ctx  = canvas.getContext('2d');
  const vctx = volCv.getContext('2d');
  const W = canvas.width, H = canvas.height;
  const VW = volCv.width, VH = volCv.height;

  ctx.clearRect(0, 0, W, H);
  vctx.clearRect(0, 0, VW, VH);

  const pad    = {l:55, r:16, t:16, b:20};
  const chartW = W - pad.l - pad.r;
  const chartH = H - pad.t - pad.b;

  // Price range
  const prices = candles.flatMap(c => [c.h, c.l]);
  const minP   = Math.min(...prices), maxP = Math.max(...prices);
  const pRange = maxP - minP || 1;
  const maxVol = Math.max(...candles.map(c => c.v || 0)) || 1;

  const toY  = p => pad.t + chartH - ((p - minP) / pRange) * chartH;
  const toVY = v => VH - (v / maxVol) * VH * 0.9;

  const n       = candles.length;
  const cw      = Math.max(Math.floor(chartW / n) - 1, 1);
  const candleW = Math.max(cw - 1, 1);

  // Grid lines + price labels
  ctx.strokeStyle = 'rgba(68,72,79,0.3)';
  ctx.lineWidth   = 0.5 * chartDPR;
  const steps = 5;
  for (let i = 0; i <= steps; i++) {
    const y = pad.t + (chartH / steps) * i;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke();
    const price = maxP - (pRange / steps) * i;
    ctx.fillStyle  = '#72757d';
    ctx.font       = (10 * chartDPR) + 'px Inter,sans-serif';
    ctx.textAlign  = 'right';
    ctx.fillText(price.toFixed(0), pad.l - 4, y + 3);
  }

  // Draw candles + volume
  candles.forEach((c, i) => {
    const x      = pad.l + i * (cw + 1);
    const isBull = c.c >= c.o;
    const warm   = c.phase === 'warmup';
    const body_c = warm ? (isBull ? WARMUP_BULL : WARMUP_BEAR) : (isBull ? LIVE_BULL : LIVE_BEAR);
    const wick_c = warm ? WICK_WARM : (isBull ? WICK_LIVE_B : WICK_LIVE_R);

    const yO = toY(c.o), yC = toY(c.c);
    const yH = toY(c.h), yL = toY(c.l);
    const bodyTop = Math.min(yO, yC);
    const bodyH   = Math.max(Math.abs(yO - yC), 1 * chartDPR);
    const cx      = x + candleW / 2;

    // Wick
    ctx.strokeStyle = wick_c;
    ctx.lineWidth   = Math.max(1, chartDPR * 0.8);
    ctx.beginPath(); ctx.moveTo(cx, yH); ctx.lineTo(cx, yL); ctx.stroke();

    // Body
    ctx.fillStyle = body_c;
    ctx.fillRect(x, bodyTop, candleW, bodyH);

    // Volume
    if (c.v) {
      const vx = pad.l + i * (VW - pad.l - pad.r) / n;
      const vy = toVY(c.v);
      vctx.fillStyle = warm
        ? 'rgba(75,85,99,0.4)'
        : (isBull ? 'rgba(129,236,255,0.25)' : 'rgba(255,110,132,0.25)');
      vctx.fillRect(vx, vy, Math.max((VW - pad.l - pad.r) / n - 1, 1), VH - vy);
    }
  });

  chartData = candles;
}

// Chart mouse tracking
document.getElementById('chart-canvas').addEventListener('mousemove', e => {
  if (!chartData.length) return;
  const canvas = document.getElementById('chart-canvas');
  const rect   = canvas.getBoundingClientRect();
  const x      = (e.clientX - rect.left) * chartDPR;
  const pad    = {l:55, r:16};
  const cw     = Math.max(Math.floor((canvas.width - pad.l - pad.r) / chartData.length) - 1, 1);
  const i      = Math.floor((x - pad.l) / (cw + 1));
  if (i < 0 || i >= chartData.length) { document.getElementById('tt').style.display='none'; return; }

  const c   = chartData[i];
  const ts  = new Date(c.ts).toLocaleTimeString('en-IN', {hour:'2-digit', minute:'2-digit'});
  const tt  = document.getElementById('tt');
  const phaseColor = c.phase === 'warmup' ? '#9ca3af' : '#81ecff';
  const phaseBg    = c.phase === 'warmup' ? '#374151' : 'rgba(129,236,255,0.2)';

  tt.style.display = 'block';
  tt.style.left    = (e.clientX + 14) + 'px';
  tt.style.top     = (e.clientY - 10) + 'px';
  tt.innerHTML =
    '<div style="color:#8b949e;margin-bottom:4px">' + ts +
    ' <span style="font-size:9px;padding:1px 5px;border-radius:8px;background:' + phaseBg +
    ';color:' + phaseColor + '">' + c.phase + '</span></div>' +
    '<div>O: <b>' + c.o.toFixed(2) + '</b>&nbsp;' +
    ' H: <b style="color:#81ecff">' + c.h.toFixed(2) + '</b>&nbsp;' +
    ' L: <b style="color:#ff6e84">' + c.l.toFixed(2) + '</b>&nbsp;' +
    ' C: <b>' + c.c.toFixed(2) + '</b></div>';

  document.getElementById('chart-crosshair').textContent =
    ts + '  O:' + c.o.toFixed(2) + '  H:' + c.h.toFixed(2) +
    '  L:' + c.l.toFixed(2) + '  C:' + c.c.toFixed(2);
});

document.getElementById('chart-canvas').addEventListener('mouseleave', () => {
  document.getElementById('tt').style.display = 'none';
  document.getElementById('chart-crosshair').textContent = 'Hover over chart';
});

// ── Strategy cards ────────────────────────────────────────────────────────────
function buildSparkline(data) {
  if (!data || data.length < 2) return '<svg viewBox="0 0 100 30"><line x1="0" y1="15" x2="100" y2="15" stroke="rgba(255,255,255,0.1)" stroke-width="1.5" vector-effect="non-scaling-stroke"/></svg>';
  const min = Math.min(...data), max = Math.max(...data), rng = max - min || 1;
  const pts = data.map((v, i) => ((i / (data.length - 1)) * 100) + ',' + (30 - ((v - min) / rng) * 26)).join(' ');
  const up  = data[data.length - 1] >= data[0];
  return '<svg viewBox="0 0 100 30"><polyline points="' + pts + '" fill="none" stroke="' + (up ? '#81ecff' : '#ff6e84') + '" stroke-width="2" vector-effect="non-scaling-stroke"/></svg>';
}

function buildStratCards(strats) {
  const el = document.getElementById('strat-cards');
  el.innerHTML = KEYS.map(k => {
    const s = strats[k];
    if (!s) return '<div class="neo rounded-xl p-4 border border-outline-variant/10"><div class="text-xs text-on-surface-variant">' + k + ' not loaded</div></div>';

    const st = s.status || 'flat';
    sparkData[k].push(s.session_pnl || 0);
    if (sparkData[k].length > 40) sparkData[k].shift();

    const pos  = s.open_trade;
    const risk = s.risk || {};
    const inPos = !!pos;
    let posHtml = '';

    if (inPos) {
      const uc      = pos.unrealised >= 0 ? 'text-primary' : 'text-error';
      const t1Hit   = risk.t1_hit;
      const trailing= risk.trailing_active;
      const atBE    = risk.sl_at_be;
      const favPct  = risk.favour_pct || 0;
      const barsHeld= risk.bars_held  || 0;
      const maxBars = risk.max_hold_bars || 10;
      const timeLeft= Math.max(0, maxBars - barsHeld);

      // Favour colour
      const favCol = favPct >= 60 ? '#81ecff' : favPct >= 30 ? '#ffd709' : '#72757d';

      // SL label
      let slLabel = 'SL';
      if (atBE && trailing) slLabel = 'SL (trailing)';
      else if (atBE)        slLabel = 'SL (BE)';

      // T1 row
      const t1Str = (risk.t1 && risk.t1 > 0)
        ? '<div class="flex justify-between items-center">' +
            '<span class="text-on-surface-variant flex items-center gap-1">T1' +
            (t1Hit ? ' <span class="text-[8px] bg-primary/20 text-primary px-1 rounded font-black">✓HIT</span>' : '') +
            '</span>' +
            '<span class="' + (t1Hit ? 'text-primary' : 'text-tertiary') + ' tabular font-bold">' + risk.t1.toFixed(2) + '</span>' +
          '</div>'
        : '';

      // Favour bar
      const favBar =
        '<div class="mt-1.5 mb-1">' +
          '<div class="flex justify-between text-[9px] text-on-surface-variant mb-0.5">' +
            '<span>Favour</span><span style="color:' + favCol + '" class="font-bold tabular">' + favPct.toFixed(0) + '%</span>' +
          '</div>' +
          '<div class="h-1 bg-surface-container-highest rounded-full overflow-hidden">' +
            '<div style="width:' + Math.min(favPct, 100) + '%;background:' + favCol + ';height:100%;border-radius:999px;transition:width .4s"></div>' +
          '</div>' +
        '</div>';

      // Time bar
      const timeBarPct = Math.min((barsHeld / maxBars) * 100, 100);
      const timeBarCol = timeBarPct > 80 ? '#ff6e84' : timeBarPct > 50 ? '#ffd709' : '#81ecff';
      const timeBar =
        '<div class="mt-0.5">' +
          '<div class="flex justify-between text-[9px] text-on-surface-variant mb-0.5">' +
            '<span>Hold time</span><span class="tabular">' + barsHeld + 'm / ' + maxBars + 'm</span>' +
          '</div>' +
          '<div class="h-0.5 bg-surface-container-highest rounded-full overflow-hidden">' +
            '<div style="width:' + timeBarPct + '%;background:' + timeBarCol + ';height:100%;border-radius:999px;transition:width .4s"></div>' +
          '</div>' +
        '</div>';

      posHtml =
        '<div class="mt-2 bg-surface/60 rounded-lg p-2.5 border border-outline-variant/10 text-[11px] space-y-1">' +
          '<div class="flex justify-between"><span class="text-on-surface-variant">Entry</span><span class="tabular font-bold">' + pos.entry_price.toFixed(2) + '</span></div>' +
          t1Str +
          '<div class="flex justify-between"><span class="text-on-surface-variant">' + slLabel + '</span><span class="text-error tabular font-bold">' + pos.sl.toFixed(2) + '</span></div>' +
          '<div class="flex justify-between"><span class="text-on-surface-variant">Target (T2)</span><span class="text-primary tabular font-bold">' + pos.target.toFixed(2) + '</span></div>' +
          favBar + timeBar +
          '<div class="flex justify-between border-t border-outline-variant/10 pt-1 mt-1"><span class="text-on-surface-variant">Unrealised</span><span class="' + uc + ' tabular font-bold">' + fmtINR(pos.unrealised) + '</span></div>' +
        '</div>' +
        '<button class="mt-btn mt-2 w-full py-1.5 rounded-lg text-[10px] font-black uppercase tracking-wider bg-error/15 text-error border border-error/30 hover:bg-error/30 transition-colors cursor-pointer"' +
          ' data-strat="' + k + '" data-dir="EXIT">' +
          '\u25a0 Exit Position' +
        '</button>';
    } else {
      // Manual entry buttons when flat
      const canTrade = st !== 'waiting';
      posHtml = canTrade
        ? '<div class="mt-2 grid grid-cols-2 gap-1.5">' +
            '<button class="mt-btn py-1.5 rounded-lg text-[10px] font-black uppercase tracking-wider bg-primary/10 text-primary border border-primary/20 hover:bg-primary/25 transition-colors cursor-pointer"' +
              ' data-strat="' + k + '" data-dir="LONG">\u25b2 Buy</button>' +
            '<button class="mt-btn py-1.5 rounded-lg text-[10px] font-black uppercase tracking-wider bg-error/10 text-error border border-error/20 hover:bg-error/25 transition-colors cursor-pointer"' +
              ' data-strat="' + k + '" data-dir="SHORT">\u25bc Sell</button>' +
          '</div>'
        : '<div class="mt-2 text-center text-[10px] text-on-surface-variant/40 py-1">Waiting for model...</div>';
    }

    return (
      '<div class="neo rounded-xl p-4 border border-outline-variant/10 fadein group">' +
        '<div class="flex justify-between items-start mb-2">' +
          '<span class="material-symbols-outlined ' + IC[k] + ' text-lg">' + ICONS[k] + '</span>' +
          '<span class="text-[9px] font-bold text-outline uppercase tracking-wider">' + TYPES[k] + '</span>' +
        '</div>' +
        '<div class="flex items-center justify-between mb-1">' +
          '<span class="font-headline font-bold text-sm group-hover:text-primary transition-colors">' + LABELS[k] + '</span>' +
          '<span class="text-[9px] font-black px-1.5 py-0.5 rounded-full border ' + scls(st) + '">' + st.toUpperCase() + '</span>' +
        '</div>' +
        '<p class="text-xl font-headline font-black tabular mb-2 ' + col(s.session_pnl) + '">' + fmtINR(s.session_pnl) + '</p>' +
        '<div class="h-7 w-full mb-2 opacity-70">' + buildSparkline(sparkData[k]) + '</div>' +
        '<div class="space-y-1 text-[11px] font-label border-t border-outline-variant/10 pt-2">' +
          '<div class="flex justify-between"><span class="text-on-surface-variant">Trades</span><span>' + s.n_trades + '</span></div>' +
          '<div class="flex justify-between"><span class="text-on-surface-variant">Win rate</span><span class="' + (s.win_rate >= 40 ? 'text-primary' : 'text-tertiary-fixed') + ' font-bold">' + s.win_rate + '%</span></div>' +
        '</div>' +
        posHtml +
      '</div>'
    );
  }).join('');
}

// ── Trade History ─────────────────────────────────────────────────────────────
function mergeT(strats) {
  allTrades = [];
  KEYS.forEach(k => {
    const s = strats[k];
    if (s && s.trades) s.trades.forEach(t => allTrades.push(Object.assign({}, t, {strat: k})));
  });
  allTrades.sort((a, b) => (b.time || '').localeCompare(a.time || ''));
}

function setTradeFilter(k) {
  tradeFilter = k;
  document.querySelectorAll('.tf-btn').forEach(b => {
    b.className = b.dataset.k === k
      ? 'tf-btn px-2.5 py-1 rounded-lg bg-primary/20 text-primary text-[10px] font-black uppercase'
      : 'tf-btn px-2.5 py-1 rounded-lg bg-surface-container-high text-on-surface-variant text-[10px] font-black uppercase';
  });
  renderTrades();
}

function renderTrades() {
  const tb   = document.getElementById('trades-body');
  const noel = document.getElementById('no-trades');
  const rows = tradeFilter === 'ALL' ? allTrades : allTrades.filter(t => t.strat === tradeFilter);
  if (!rows.length) { tb.innerHTML = ''; noel.style.display = 'block'; return; }
  noel.style.display = 'none';
  const di = d => d === 'LONG'
    ? '<span class="text-primary font-bold flex items-center gap-0.5"><span class="material-symbols-outlined text-xs">north_east</span>LONG</span>'
    : '<span class="text-error font-bold flex items-center gap-0.5"><span class="material-symbols-outlined text-xs">south_east</span>SHORT</span>';
  tb.innerHTML = rows.slice(0, 60).map(t =>
    '<tr class="hover:bg-white/5 transition-colors">' +
      '<td class="px-4 py-2.5"><span class="text-[9px] font-black px-1.5 py-0.5 rounded bg-surface-container-high text-primary">' + t.strat + '</span></td>' +
      '<td class="px-4 py-2.5 tabular text-on-surface-variant">' + t.time + '</td>' +
      '<td class="px-4 py-2.5">' + di(t.direction) + '</td>' +
      '<td class="px-4 py-2.5 tabular">' + t.entry + '</td>' +
      '<td class="px-4 py-2.5 tabular">' + (t.exit || '--') + '</td>' +
      '<td class="px-4 py-2.5 text-right"><span class="' + (t.pnl >= 0 ? 'bg-primary/10 text-primary' : 'bg-error/10 text-error') + ' px-2 py-0.5 rounded text-[10px] font-bold tabular">' + fmtINR(t.pnl) + '</span></td>' +
      '<td class="px-4 py-2.5 text-on-surface-variant text-[10px]">' + (t.reason || '--') + '</td>' +
    '</tr>'
  ).join('');
}

// ── Activity Feed ─────────────────────────────────────────────────────────────
function mergeL(strats) {
  allLogs = [];
  KEYS.forEach(k => {
    const s = strats[k];
    if (s && s.log) s.log.forEach(l => allLogs.push({l, k}));
  });
}

function setLogFilter(k) {
  logFilter = k;
  document.querySelectorAll('.lt-btn').forEach(b => {
    b.className = b.dataset.k === k
      ? 'lt-btn px-2.5 py-1 rounded-lg bg-primary/20 text-primary text-[10px] font-black uppercase'
      : 'lt-btn px-2.5 py-1 rounded-lg bg-surface-container-high text-on-surface-variant text-[10px] font-black uppercase';
  });
  renderLogs();
}

function renderLogs() {
  const box  = document.getElementById('log-feed');
  const rows = (logFilter === 'ALL' ? allLogs : allLogs.filter(x => x.k === logFilter))
    .slice().reverse().slice(0, 100);
  if (!rows.length) { box.innerHTML = '<div class="text-on-surface-variant/50 text-center py-8">No activity yet</div>'; return; }
  box.innerHTML = rows.map(({l, k}) => {
    let c = 'text-on-surface-variant/50';
    if (l.includes('▶') || l.includes('▲') || l.includes('▼')) c = 'text-primary font-bold';
    else if (l.includes('✅'))             c = 'text-primary';
    else if (l.includes('❌'))             c = 'text-error';
    else if (l.includes('🎯'))             c = 'text-tertiary-fixed font-bold';  // T1 hit
    else if (l.includes('↕'))              c = 'text-tertiary';             // trail SL
    else if (l.includes('👇'))             c = 'text-primary-container font-bold'; // manual
    return '<div class="py-0.5 leading-relaxed ' + c + '"><span class="text-[9px] font-black text-outline mr-1">[' + k + ']</span>' + l + '</div>';
  }).join('');
}

// ── Manual trading ────────────────────────────────────────────────────────────
async function manualTrade(strategy, direction) {
  try {
    const r = await fetch('/api/manual_trade', {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify({strategy, direction}),
    });
    const d = await r.json();
    // Show a brief on-screen toast
    showToast(d.message, d.ok ? '#81ecff' : '#ff6e84');
    if (d.ok) refresh();  // immediate refresh
  } catch(e) { showToast('Request failed', '#ff6e84'); }
}

function showToast(msg, color) {
  let el = document.getElementById('trade-toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'trade-toast';
    el.style.cssText = 'position:fixed;bottom:24px;left:50%;transform:translateX(-50%);' +
      'padding:10px 20px;border-radius:8px;font-family:Inter,sans-serif;font-size:12px;' +
      'font-weight:600;z-index:9999;pointer-events:none;transition:opacity .3s;';
    document.body.appendChild(el);
  }
  el.style.background = color === '#81ecff' ? 'rgba(129,236,255,0.15)' : 'rgba(255,110,132,0.15)';
  el.style.border = '1px solid ' + color;
  el.style.color  = color;
  el.textContent  = msg;
  el.style.opacity = '1';
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.style.opacity = '0'; }, 3500);
}

// ── Intelligence panel ────────────────────────────────────────────────────────
function updateIntel(intel) {
  // Engine returns key "intel"; shape: { regime, allow_trading, is_valid }
  // Extended shape (from intelligence engine full output) may also include:
  //   score, gate (alias for allow_trading), best, reason
  if (!intel) return;

  const regime = intel.regime || '--';
  const regimeColor = {
    TRENDING_UP:   'text-primary',
    TRENDING_DOWN: 'text-error',
    RANGING:       'text-tertiary',
    HIGH_VOL:      'text-tertiary-fixed',
    CHOPPY:        'text-on-surface-variant',
  }[regime] || 'text-on-surface';

  const regimeEl = document.getElementById('intel-regime');
  regimeEl.className   = 'font-headline font-bold text-sm ' + regimeColor;
  regimeEl.textContent = regime.replace(/_/g, ' ');

  // Confidence / score — may not be present in the simple engine summary
  const sc       = intel.score != null ? intel.score : null;
  const scoreEl  = document.getElementById('intel-score');
  if (sc != null) {
    scoreEl.textContent = (sc * 100).toFixed(0) + '%';
    scoreEl.className   = 'font-headline font-bold text-sm ' +
      (sc >= 0.6 ? 'text-primary' : sc >= 0.35 ? 'text-tertiary-fixed' : 'text-error');
  } else {
    scoreEl.textContent = '--';
    scoreEl.className   = 'font-headline font-bold text-sm text-on-surface-variant';
  }

  // Gate — engine returns allow_trading (bool); extended may return gate (bool)
  const gate   = intel.gate != null ? intel.gate : intel.allow_trading;
  const gateEl = document.getElementById('intel-gate');
  gateEl.textContent = gate ? 'PASS' : 'BLOCKED';
  gateEl.className   = 'font-headline font-bold text-sm ' + (gate ? 'text-primary' : 'text-error');

  // Best strategies — extended field
  document.getElementById('intel-best').textContent   = (intel.best || []).join(', ') || '--';
  document.getElementById('intel-reason').textContent = intel.reason || '';
}

// ── Main refresh ──────────────────────────────────────────────────────────────
async function refresh() {
  try {
    const d = await (await fetch('/api/state')).json();

    // Status
    const running = d.status === 'running';
    const dot = document.getElementById('status-dot');
    dot.className      = 'w-2 h-2 rounded-full ' + (running ? 'bg-primary' : 'bg-slate-500');
    dot.style.animation = running ? 'pulse 1.5s infinite' : '';
    const stEl = document.getElementById('status-text');
    stEl.textContent = running ? 'Bot Active' : 'Connecting...';
    stEl.className   = 'text-xs font-label ' + (running ? 'text-tertiary-fixed' : 'text-slate-500');
    document.getElementById('nav-ts').textContent = d.timestamp || '--';

    const badge = document.getElementById('live-badge');
    if (running) {
      badge.textContent = 'LIVE';
      badge.className   = 'text-[10px] font-black px-2 py-0.5 rounded-full bg-primary/20 text-primary';
    }

    // Price
    const pr = d.current_price;
    if (pr) {
      document.getElementById('hero-price').textContent = pr.toFixed(2);
      document.getElementById('nav-price').textContent  = pr.toFixed(2);
    }

    // P&L
    const tp   = d.total_pnl || 0;
    const pnlEl = document.getElementById('hero-pnl');
    pnlEl.textContent = fmtINR(tp);
    pnlEl.className   = 'text-3xl font-headline font-bold tabular ' + col(tp);
    const npEl = document.getElementById('nav-pnl');
    npEl.textContent = fmtINR(tp);
    npEl.className   = 'font-headline font-bold tabular text-lg leading-none ' + col(tp);

    const tt2 = d.total_trades || 0;
    document.getElementById('hero-trades').textContent =
      tt2 + ' trade' + (tt2 !== 1 ? 's' : '') + ' across 5 strategies';

    // OHLCV
    const o = d.session_ohlcv || {};
    if (o.open)                          document.getElementById('d-open').textContent  = o.open.toFixed(2);
    if (o.high)                          document.getElementById('d-high').textContent  = o.high.toFixed(2);
    if (o.low && o.low < 1e9)            document.getElementById('d-low').textContent   = o.low.toFixed(2);
    if (o.vwap)                          document.getElementById('d-vwap').textContent  = o.vwap.toFixed(2);
    if (o.orb_high)                      document.getElementById('d-orb-h').textContent = o.orb_high.toFixed(2);
    if (o.orb_low && o.orb_low < 1e9)   document.getElementById('d-orb-l').textContent = o.orb_low.toFixed(2);
    if (o.n_bars !== undefined)          document.getElementById('d-bars').textContent  = o.n_bars;

    // Warmup
    if (d.warmup) {
      document.getElementById('warmup-line').textContent = d.warmup.summary || '';
      document.getElementById('ov-detail').textContent   = d.warmup.detail  || '';
    }

    // Chart
    if (d.chart_1m && d.chart_1m.length > 0) drawChart(d.chart_1m);

    // Portfolio overview
    const strats = d.strategies || {};
    const active = Object.values(strats).filter(s => s.status !== 'waiting').length;
    document.getElementById('ov-active').textContent = active + ' / 5';
    document.getElementById('ov-bar').style.width    = (active / 5 * 100) + '%';
    document.getElementById('ov-cap').textContent    = fmtFull(d.total_capital || 0);
    const ovp = document.getElementById('ov-pnl');
    ovp.textContent = fmtINR(tp);
    ovp.className   = 'tabular font-bold ' + col(tp);
    document.getElementById('ov-n').textContent = tt2;

    let wins = 0, ntrades = 0;
    KEYS.forEach(k => {
      const s = strats[k];
      if (s) { ntrades += s.n_trades; wins += Math.round(s.n_trades * (s.win_rate || 0) / 100); }
    });
    document.getElementById('ov-wr').textContent = ntrades > 0 ? (wins / ntrades * 100).toFixed(1) + '%' : '--';
    document.getElementById('last-update').textContent = 'Updated: ' + d.timestamp;

    // Intelligence — engine exposes key "intel"
    updateIntel(d.intel || d.intelligence || null);

    // Strategy cards
    buildStratCards(strats);

    // Trades + logs
    mergeT(strats); renderTrades();
    mergeL(strats); renderLogs();

  } catch (e) { console.error(e); }
}

refresh();
setInterval(refresh, 3000);
window.addEventListener('resize', () => { if (chartData.length) drawChart(chartData); });

// Delegated click handler for manual trade buttons (avoids string escaping issues)
document.addEventListener('click', function(e) {
  const btn = e.target.closest('.mt-btn');
  if (!btn) return;
  const strat = btn.getAttribute('data-strat');
  const dir   = btn.getAttribute('data-dir');
  if (strat && dir) manualTrade(strat, dir);
});
</script>
</body>
</html>"""


def create_multi_app(engine) -> Flask:
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    app = Flask(__name__)

    @app.route("/")
    def index():
        return render_template_string(HTML)

    @app.route("/api/state")
    def state():
        return jsonify(engine.get_state())

    @app.route("/api/manual_trade", methods=["POST"])
    def manual_trade():
        from flask import request
        # The dashboard is served publicly without login — anyone with the URL
        # could otherwise place trades. Opt in with ALLOW_MANUAL_TRADES=1.
        if os.getenv("ALLOW_MANUAL_TRADES", "0") != "1":
            return jsonify({"ok": False,
                            "message": "Manual trading is disabled on this server"}), 403
        data      = request.get_json(force=True, silent=True) or {}
        s_key     = data.get("strategy", "")
        direction = data.get("direction", "")
        if not s_key or not direction:
            return jsonify({"ok": False, "message": "strategy and direction required"}), 400
        result = engine.manual_trade(s_key, direction)
        return jsonify(result)

    return app