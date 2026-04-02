#!/usr/bin/env python3
"""
NemoClaw Live UI Server
========================
Serves a terminal-style browser dashboard that streams the orchestrator's
live output via WebSocket. Runs on port 8080.

Usage:
    python ui_server.py              # Start the UI server
    python orchestrator.py           # In another terminal — logs stream to UI

The orchestrator broadcasts events to the UI via a shared event queue.
"""

import asyncio
import json
import os
import subprocess
import sys
import signal
import threading
from pathlib import Path
from datetime import datetime

try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.responses import HTMLResponse, FileResponse
    import uvicorn
except ImportError:
    print("Installing dependencies...")
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                           "fastapi", "uvicorn[standard]", "websockets",
                           "--break-system-packages", "-q"])
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.responses import HTMLResponse, FileResponse
    import uvicorn

app = FastAPI(title="NemoClaw Autonomous Software Factory")

BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "output"
LOGS_DIR = BASE_DIR / "logs"

# Connected WebSocket clients
ws_clients: set = set()

# Pipeline process handle
pipeline_process = None

# Event log buffer (in-memory, for clients that connect mid-run)
event_buffer: list = []


# ── Dashboard HTML ────────────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NemoClaw — Autonomous Software Factory Powered by HP ZGX Nano</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Space+Grotesk:wght@400;500;700&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg-primary: #0a0e1a;
    --bg-card: #111827;
    --bg-terminal: #080c14;
    --border: #1e293b;
    --text-primary: #e2e8f0;
    --text-dim: #64748b;
    --text-muted: #475569;
    --accent-blue: #00d4ff;
    --accent-cyan: #00ffcc;
    --accent-amber: #ffaa00;
    --accent-red: #ff3b3b;
    --accent-green: #34d399;
    --accent-purple: #a78bfa;
  }

  body {
    background: var(--bg-primary);
    color: var(--text-primary);
    font-family: 'Space Grotesk', system-ui, sans-serif;
    min-height: 100vh;
    overflow: hidden;
  }

  /* ── Layout ── */
  .app {
    display: grid;
    grid-template-rows: auto 1fr auto;
    height: 100vh;
  }

  /* ── Header ── */
  .header {
    padding: 20px 32px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    background: linear-gradient(180deg, rgba(0,212,255,0.03) 0%, transparent 100%);
  }

  .header-left {
    display: flex;
    align-items: center;
    gap: 16px;
  }

  .logo {
    display: flex;
    align-items: center;
    gap: 10px;
  }

  .logo-icon {
    width: 36px;
    height: 36px;
    background: linear-gradient(135deg, var(--accent-blue), var(--accent-cyan));
    border-radius: 8px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 18px;
    font-weight: 700;
    color: var(--bg-primary);
    font-family: 'JetBrains Mono', monospace;
  }

  .logo-text {
    font-size: 18px;
    font-weight: 700;
    letter-spacing: -0.02em;
  }

  .logo-text span {
    color: var(--accent-blue);
  }

  .header-badge {
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    padding: 4px 10px;
    border-radius: 4px;
    background: rgba(0,212,255,0.1);
    color: var(--accent-blue);
    border: 1px solid rgba(0,212,255,0.2);
  }

  .header-right {
    display: flex;
    align-items: center;
    gap: 16px;
  }

  .connection-status {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    color: var(--text-dim);
    font-family: 'JetBrains Mono', monospace;
  }

  .status-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--accent-red);
  }

  .status-dot.connected {
    background: var(--accent-green);
    box-shadow: 0 0 8px rgba(52,211,153,0.5);
  }

  .btn-run {
    font-family: 'JetBrains Mono', monospace;
    font-size: 13px;
    font-weight: 500;
    padding: 8px 20px;
    border: 1px solid var(--accent-cyan);
    background: rgba(0,255,204,0.08);
    color: var(--accent-cyan);
    border-radius: 6px;
    cursor: pointer;
    transition: all 0.2s;
    letter-spacing: 0.02em;
  }

  .btn-run:hover {
    background: rgba(0,255,204,0.15);
    box-shadow: 0 0 20px rgba(0,255,204,0.1);
  }

  .btn-run:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  .btn-run.running {
    border-color: var(--accent-amber);
    color: var(--accent-amber);
    background: rgba(255,170,0,0.08);
  }

  /* ── Main content ── */
  .main {
    display: grid;
    grid-template-columns: 220px 1fr;
    overflow: hidden;
  }

  /* ── Pipeline sidebar ── */
  .pipeline {
    border-right: 1px solid var(--border);
    padding: 24px 16px;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .pipeline-title {
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    color: var(--text-muted);
    margin-bottom: 12px;
    padding: 0 8px;
  }

  .stage {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 12px 12px;
    border-radius: 8px;
    transition: all 0.3s;
    position: relative;
  }

  .stage.active {
    background: rgba(0,212,255,0.06);
    border: 1px solid rgba(0,212,255,0.15);
  }

  .stage.complete {
    opacity: 0.6;
  }

  .stage-number {
    width: 28px;
    height: 28px;
    border-radius: 6px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-family: 'JetBrains Mono', monospace;
    font-size: 12px;
    font-weight: 700;
    background: var(--bg-card);
    border: 1px solid var(--border);
    color: var(--text-dim);
    flex-shrink: 0;
  }

  .stage.active .stage-number {
    background: rgba(0,212,255,0.15);
    border-color: var(--accent-blue);
    color: var(--accent-blue);
    box-shadow: 0 0 12px rgba(0,212,255,0.2);
  }

  .stage.complete .stage-number {
    background: rgba(52,211,153,0.15);
    border-color: var(--accent-green);
    color: var(--accent-green);
  }

  .stage.failed .stage-number {
    background: rgba(255,59,59,0.15);
    border-color: var(--accent-red);
    color: var(--accent-red);
  }

  .stage-info {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  .stage-name {
    font-size: 13px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .stage-detail {
    font-size: 10px;
    font-family: 'JetBrains Mono', monospace;
    color: var(--text-muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .stage.active .stage-detail {
    color: var(--accent-blue);
  }

  /* Connector lines between stages */
  .stage-connector {
    width: 1px;
    height: 12px;
    background: var(--border);
    margin-left: 26px;
  }

  /* Pipeline stats at bottom */
  .pipeline-stats {
    margin-top: auto;
    padding: 16px 8px;
    border-top: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .stat-row {
    display: flex;
    justify-content: space-between;
    font-size: 11px;
    font-family: 'JetBrains Mono', monospace;
  }

  .stat-label { color: var(--text-muted); }
  .stat-value { color: var(--text-dim); }

  /* ── Terminal ── */
  .terminal-container {
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  .terminal-header {
    padding: 8px 16px;
    background: var(--bg-card);
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  .terminal-tabs {
    display: flex;
    gap: 2px;
  }

  .terminal-tab {
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    padding: 4px 12px;
    border-radius: 4px;
    color: var(--text-muted);
    cursor: pointer;
    border: none;
    background: none;
    transition: all 0.2s;
  }

  .terminal-tab.active {
    background: rgba(0,212,255,0.1);
    color: var(--accent-blue);
  }

  .terminal-actions {
    display: flex;
    gap: 8px;
  }

  .terminal-action {
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px;
    padding: 3px 8px;
    border-radius: 3px;
    color: var(--text-muted);
    cursor: pointer;
    border: 1px solid var(--border);
    background: none;
    transition: all 0.2s;
  }

  .terminal-action:hover {
    border-color: var(--text-dim);
    color: var(--text-dim);
  }

  .terminal {
    flex: 1;
    overflow-y: auto;
    padding: 16px;
    background: var(--bg-terminal);
    font-family: 'JetBrains Mono', monospace;
    font-size: 12.5px;
    line-height: 1.7;
  }

  .terminal::-webkit-scrollbar { width: 6px; }
  .terminal::-webkit-scrollbar-track { background: transparent; }
  .terminal::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

  .log-line {
    white-space: pre-wrap;
    word-break: break-all;
    animation: fadeIn 0.15s ease;
  }

  @keyframes fadeIn {
    from { opacity: 0; transform: translateY(2px); }
    to { opacity: 1; transform: translateY(0); }
  }

  .log-time { color: var(--text-muted); }
  .log-stage { font-weight: 700; }

  .log-stage.architect { color: var(--accent-purple); }
  .log-stage.coder { color: var(--accent-cyan); }
  .log-stage.reviewer { color: var(--accent-amber); }
  .log-stage.analyst { color: var(--accent-blue); }
  .log-stage.orchestrator { color: var(--text-dim); }
  .log-stage.validator { color: var(--accent-green); }
  .log-stage.fallback { color: var(--accent-red); }

  .log-message { color: var(--text-primary); }
  .log-message.error { color: var(--accent-red); }
  .log-message.warning { color: var(--accent-amber); }
  .log-message.success { color: var(--accent-green); }

  /* Stage banner lines */
  .log-banner {
    color: var(--accent-blue);
    font-weight: 700;
    padding: 8px 0 4px;
    border-top: 1px solid rgba(0,212,255,0.1);
    margin-top: 8px;
  }

  /* Pipeline complete banner */
  .log-complete {
    color: var(--accent-green);
    font-weight: 700;
    padding: 12px 0;
    border-top: 1px solid rgba(52,211,153,0.2);
    margin-top: 12px;
  }

  /* Dashboard ready link */
  .dashboard-link {
    display: inline-block;
    margin-top: 12px;
    padding: 10px 24px;
    background: rgba(0,255,204,0.1);
    border: 1px solid var(--accent-cyan);
    border-radius: 6px;
    color: var(--accent-cyan);
    text-decoration: none;
    font-weight: 600;
    font-size: 13px;
    transition: all 0.2s;
  }

  .dashboard-link:hover {
    background: rgba(0,255,204,0.2);
    box-shadow: 0 0 20px rgba(0,255,204,0.15);
  }

  /* ── Footer ── */
  .footer {
    padding: 8px 32px;
    border-top: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px;
    color: var(--text-muted);
  }

  .footer-left {
    display: flex;
    align-items: center;
    gap: 16px;
  }

  .footer-tag {
    padding: 2px 8px;
    background: rgba(0,212,255,0.06);
    border: 1px solid rgba(0,212,255,0.1);
    border-radius: 3px;
    color: var(--accent-blue);
  }

  /* ── Pulse animation ── */
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
  }

  .pulsing {
    animation: pulse 1.5s ease-in-out infinite;
  }

  /* ── Cursor blink ── */
  .cursor {
    display: inline-block;
    width: 7px;
    height: 15px;
    background: var(--accent-blue);
    animation: blink 1s step-end infinite;
    vertical-align: text-bottom;
    margin-left: 2px;
  }

  @keyframes blink {
    50% { opacity: 0; }
  }
</style>
</head>
<body>
<div class="app">

  <!-- Header -->
  <div class="header">
    <div class="header-left">
      <div class="logo">
        <div class="logo-icon">🦞</div>
        <div class="logo-text"><span>Nemo</span>Claw</div>
      </div>
      <div class="header-badge">AUTONOMOUS SOFTWARE FACTORY POWERED BY HP ZGX NANO</div>
    </div>
    <div class="header-right">
      <div class="connection-status">
        <div class="status-dot" id="ws-status"></div>
        <span id="ws-label">disconnected</span>
      </div>
      <button class="btn-run" id="btn-run" onclick="startPipeline()">▶ RUN PIPELINE</button>
    </div>
  </div>

  <!-- Main -->
  <div class="main">

    <!-- Pipeline sidebar -->
    <div class="pipeline">
      <div class="pipeline-title">Pipeline Stages</div>

      <div class="stage" id="stage-1">
        <div class="stage-number">1</div>
        <div class="stage-info">
          <div class="stage-name">Architect</div>
          <div class="stage-detail">waiting</div>
        </div>
      </div>
      <div class="stage-connector"></div>

      <div class="stage" id="stage-2">
        <div class="stage-number">2</div>
        <div class="stage-info">
          <div class="stage-name">Coder</div>
          <div class="stage-detail">waiting</div>
        </div>
      </div>
      <div class="stage-connector"></div>

      <div class="stage" id="stage-3">
        <div class="stage-number">3</div>
        <div class="stage-info">
          <div class="stage-name">Reviewer</div>
          <div class="stage-detail">waiting</div>
        </div>
      </div>
      <div class="stage-connector"></div>

      <div class="stage" id="stage-4">
        <div class="stage-number">4</div>
        <div class="stage-info">
          <div class="stage-name">Analyst</div>
          <div class="stage-detail">waiting</div>
        </div>
      </div>

      <div class="pipeline-stats">
        <div class="stat-row">
          <span class="stat-label">elapsed</span>
          <span class="stat-value" id="elapsed">--:--</span>
        </div>
        <div class="stat-row">
          <span class="stat-label">model</span>
          <span class="stat-value">Qwen3-Coder-30B</span>
        </div>
        <div class="stat-row">
          <span class="stat-label">hardware</span>
          <span class="stat-value">HP ZGX Nano</span>
        </div>
        <div class="stat-row">
          <span class="stat-label">sandboxes</span>
          <span class="stat-value">4 × NemoClaw</span>
        </div>
      </div>
    </div>

    <!-- Terminal -->
    <div class="terminal-container">
      <div class="terminal-header">
        <div class="terminal-tabs">
          <button class="terminal-tab active">Live Output</button>
        </div>
        <div class="terminal-actions">
          <button class="terminal-action" onclick="clearTerminal()">clear</button>
          <button class="terminal-action" onclick="scrollToBottom()">↓ bottom</button>
        </div>
      </div>
      <div class="terminal" id="terminal">
        <div class="log-line">
          <span class="log-time">[--:--:--]</span>
          <span class="log-stage orchestrator"> [SYSTEM]</span>
          <span class="log-message"> NemoClaw Autonomous Software Factory — ready</span>
        </div>
        <div class="log-line">
          <span class="log-time">[--:--:--]</span>
          <span class="log-stage orchestrator"> [SYSTEM]</span>
          <span class="log-message"> Press RUN PIPELINE to begin</span>
        </div>
        <span class="cursor"></span>
      </div>
    </div>

  </div>

  <!-- Footer -->
  <div class="footer">
    <div class="footer-left">
      <span class="footer-tag">ON-PREMISES</span>
      <span class="footer-tag">ZERO CLOUD</span>
      <span class="footer-tag">SANDBOX ISOLATED</span>
    </div>
    <span>HP ZGX Nano AI Station × NVIDIA NemoClaw</span>
  </div>

</div>

<script>
  const terminal = document.getElementById('terminal');
  const wsStatus = document.getElementById('ws-status');
  const wsLabel = document.getElementById('ws-label');
  const btnRun = document.getElementById('btn-run');
  const elapsedEl = document.getElementById('elapsed');

  let ws = null;
  let pipelineRunning = false;
  let startTime = null;
  let elapsedInterval = null;
  let currentStage = 0;

  const stageMap = {
    'architect': 1,
    'coder': 2,
    'reviewer': 3,
    'analyst': 4,
    'validator': 2  // validator is part of coder stage
  };

  function connectWS() {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${protocol}//${location.host}/ws/stream`);

    ws.onopen = () => {
      wsStatus.classList.add('connected');
      wsLabel.textContent = 'connected';
    };

    ws.onclose = () => {
      wsStatus.classList.remove('connected');
      wsLabel.textContent = 'disconnected';
      setTimeout(connectWS, 3000);
    };

    ws.onmessage = (e) => {
      try {
        const event = JSON.parse(e.data);
        handleEvent(event);
      } catch (err) {
        console.error('WS parse error:', err);
      }
    };
  }

  function handleEvent(event) {
    switch (event.type) {
      case 'log':
        appendLog(event);
        break;
      case 'stage':
        showBanner(event.title);
        updatePipelineStage(event.title);
        break;
      case 'pipeline_start':
        pipelineRunning = true;
        btnRun.textContent = '● RUNNING';
        btnRun.classList.add('running');
        btnRun.disabled = true;
        startTime = Date.now();
        startElapsedTimer();
        break;
      case 'pipeline_complete':
        pipelineRunning = false;
        btnRun.textContent = '▶ RUN PIPELINE';
        btnRun.classList.remove('running');
        btnRun.disabled = false;
        stopElapsedTimer();
        showComplete(event);
        // Mark final stage complete
        for (let i = 1; i <= 4; i++) {
          const el = document.getElementById(`stage-${i}`);
          if (!el.classList.contains('failed')) {
            el.classList.remove('active');
            el.classList.add('complete');
            el.querySelector('.stage-detail').textContent = 'done';
          }
        }
        break;
      case 'agent_start':
        const stageNum = stageMap[event.agent];
        if (stageNum) {
          updateStageStatus(stageNum, 'active', 'processing...');
        }
        break;
      case 'agent_complete':
        break;
      case 'fallback':
        appendLog({stage: 'fallback', message: event.reason, level: 'warning'});
        break;
    }
  }

  function appendLog(event) {
    const cursor = terminal.querySelector('.cursor');
    if (cursor) cursor.remove();

    const line = document.createElement('div');
    line.className = 'log-line';

    const ts = event.timestamp
      ? new Date(event.timestamp).toLocaleTimeString('en-US', {hour12: false})
      : new Date().toLocaleTimeString('en-US', {hour12: false});

    const stage = event.stage || 'system';
    const levelClass = event.level === 'error' ? ' error'
                     : event.level === 'warning' ? ' warning'
                     : event.message && event.message.includes('✓') ? ' success' : '';

    line.innerHTML =
      `<span class="log-time">[${ts}]</span>` +
      `<span class="log-stage ${stage}"> [${stage.toUpperCase()}]</span>` +
      `<span class="log-message${levelClass}"> ${escapeHtml(event.message)}</span>`;

    terminal.appendChild(line);

    // Re-add cursor
    const newCursor = document.createElement('span');
    newCursor.className = 'cursor';
    terminal.appendChild(newCursor);

    scrollToBottom();
  }

  function showBanner(title) {
    const cursor = terminal.querySelector('.cursor');
    if (cursor) cursor.remove();

    const line = document.createElement('div');
    line.className = 'log-line log-banner';
    line.textContent = `━━━ ${title} ━━━`;
    terminal.appendChild(line);

    const newCursor = document.createElement('span');
    newCursor.className = 'cursor';
    terminal.appendChild(newCursor);

    scrollToBottom();
  }

  function showComplete(event) {
    const cursor = terminal.querySelector('.cursor');
    if (cursor) cursor.remove();

    const div = document.createElement('div');
    div.className = 'log-line log-complete';
    const fb = event.fallback_used ? ' (fallback used)' : '';
    div.textContent = `✓ PIPELINE COMPLETE — ${event.elapsed_seconds}s${fb}`;
    terminal.appendChild(div);

    if (!event.fallback_used) {
      const link = document.createElement('a');
      link.className = 'dashboard-link';
      link.href = '/dashboard';
      link.target = '_blank';
      link.textContent = '→ OPEN GENERATED DASHBOARD';
      terminal.appendChild(link);
    }

    scrollToBottom();
  }

  function updatePipelineStage(title) {
    // Parse stage number from title like "STAGE 2 / 4 — CODER"
    const match = title.match(/STAGE\\s+(\\d+)/i);
    if (!match) return;
    const num = parseInt(match[1]);

    // Mark previous stages complete
    for (let i = 1; i < num; i++) {
      updateStageStatus(i, 'complete', 'done');
    }
    // Mark current stage active
    updateStageStatus(num, 'active', 'running...');
  }

  function updateStageStatus(num, status, detail) {
    const el = document.getElementById(`stage-${num}`);
    if (!el) return;
    el.classList.remove('active', 'complete', 'failed');
    el.classList.add(status);
    el.querySelector('.stage-detail').textContent = detail;
  }

  function startElapsedTimer() {
    elapsedInterval = setInterval(() => {
      if (!startTime) return;
      const elapsed = Math.floor((Date.now() - startTime) / 1000);
      const m = Math.floor(elapsed / 60).toString().padStart(2, '0');
      const s = (elapsed % 60).toString().padStart(2, '0');
      elapsedEl.textContent = `${m}:${s}`;
    }, 1000);
  }

  function stopElapsedTimer() {
    if (elapsedInterval) clearInterval(elapsedInterval);
  }

  function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  function clearTerminal() {
    terminal.innerHTML = '<span class="cursor"></span>';
  }

  function scrollToBottom() {
    terminal.scrollTop = terminal.scrollHeight;
  }

  async function startPipeline() {
    if (pipelineRunning) return;
    try {
      const res = await fetch('/api/start', { method: 'POST' });
      if (!res.ok) {
        appendLog({stage: 'system', message: 'Failed to start pipeline', level: 'error'});
      }
    } catch (err) {
      appendLog({stage: 'system', message: `Error: ${err.message}`, level: 'error'});
    }
  }

  // Connect on load
  connectWS();
</script>
</body>
</html>"""


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root():
    return DASHBOARD_HTML


@app.get("/dashboard")
async def serve_dashboard():
    """Serve the generated dashboard HTML file."""
    dashboard_path = OUTPUT_DIR / "dashboard.html"
    if dashboard_path.exists():
        return FileResponse(str(dashboard_path), media_type="text/html")
    return HTMLResponse("<h1>Dashboard not yet generated</h1>", status_code=404)


@app.websocket("/ws/stream")
async def websocket_stream(websocket: WebSocket):
    await websocket.accept()
    queue = asyncio.Queue()
    ws_clients.add(queue)

    # Send buffered events to catch up
    for event in event_buffer:
        await websocket.send_text(json.dumps(event))

    try:
        while True:
            event = await queue.get()
            await websocket.send_text(json.dumps(event))
    except WebSocketDisconnect:
        pass
    finally:
        ws_clients.discard(queue)


@app.post("/api/start")
async def start_pipeline():
    """Start the orchestrator pipeline in a subprocess."""
    global pipeline_process

    if pipeline_process and pipeline_process.poll() is None:
        return {"status": "already_running"}

    # Clear event buffer for new run
    event_buffer.clear()

    # Start orchestrator as subprocess, capture its stdout/stderr
    # PYTHONUNBUFFERED ensures output flows immediately instead of being buffered
    orchestrator_path = BASE_DIR / "orchestrator.py"
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    pipeline_process = subprocess.Popen(
        [sys.executable, "-u", str(orchestrator_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=str(BASE_DIR),
        env=env
    )

    # Stream output in background thread
    def stream_output():
        for line in pipeline_process.stdout:
            line = line.rstrip()
            if not line:
                continue

            # Parse log lines: [HH:MM:SS] [STAGE] message
            import re
            match = re.match(r'\[(\d{2}:\d{2}:\d{2})\]\s+\[(\w+)\]\s+(.*)', line)

            if match:
                ts_str, stage, message = match.groups()
                level = "info"
                if "✗" in message or "error" in message.lower() or "failed" in message.lower():
                    level = "error"
                elif "warning" in message.lower() or "REVISE" in message:
                    level = "warning"
                elif "✓" in message:
                    level = "info"

                event = {
                    "type": "log",
                    "timestamp": datetime.now().isoformat(),
                    "stage": stage.lower(),
                    "message": message,
                    "level": level
                }
            elif "STAGE" in line and "—" in line:
                # Banner line
                event = {
                    "type": "stage",
                    "timestamp": datetime.now().isoformat(),
                    "title": line.strip().strip("─ ")
                }
            elif "PIPELINE COMPLETE" in line:
                event = {
                    "type": "pipeline_complete",
                    "timestamp": datetime.now().isoformat(),
                    "elapsed_seconds": 0,
                    "fallback_used": False
                }
            elif "═" in line or not line.strip():
                continue
            else:
                event = {
                    "type": "log",
                    "timestamp": datetime.now().isoformat(),
                    "stage": "system",
                    "message": line,
                    "level": "info"
                }

            event_buffer.append(event)
            for q in list(ws_clients):
                try:
                    q.put_nowait(event)
                except Exception:
                    pass

        # Pipeline finished
        exit_code = pipeline_process.wait()
        final_event = {
            "type": "pipeline_complete",
            "timestamp": datetime.now().isoformat(),
            "elapsed_seconds": 0,
            "fallback_used": exit_code != 0,
            "exit_code": exit_code
        }
        event_buffer.append(final_event)
        for q in list(ws_clients):
            try:
                q.put_nowait(final_event)
            except Exception:
                pass

    threading.Thread(target=stream_output, daemon=True).start()

    return {"status": "started"}


@app.get("/api/status")
async def get_status():
    """Return current pipeline status."""
    running = pipeline_process is not None and pipeline_process.poll() is None
    return {
        "running": running,
        "events": len(event_buffer),
        "dashboard_ready": (OUTPUT_DIR / "dashboard.html").exists()
    }


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import socket
    hostname = socket.gethostname()
    # Get all IPs for this machine
    try:
        ips = socket.getaddrinfo(hostname, None, socket.AF_INET)
        local_ips = list(set(addr[4][0] for addr in ips if not addr[4][0].startswith("127.")))
    except Exception:
        local_ips = []

    print("NemoClaw Live UI")
    print(f"  Local:    http://localhost:8888")
    for ip in local_ips:
        print(f"  Network:  http://{ip}:8888")
    print(f"  Hostname: http://{hostname}:8888")
    print()
    uvicorn.run(app, host="0.0.0.0", port=8888, log_level="warning")