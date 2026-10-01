#!/usr/bin/env python3
"""
Crypto Agent Team — Web Dashboard

Run:
    python3 server.py               # starts on http://localhost:8080
    python3 server.py --port 9000   # custom port
    python3 server.py --interval 3  # auto-scan every 3h (0 disables, default 6h)

Open on your phone: http://<your-local-ip>:8080
Find your IP: ifconfig | grep "inet " | grep -v 127.0.0.1

For access from outside your home Wi-Fi (free, no account):
    cloudflared tunnel --url http://localhost:8080
    → prints a public https://<random>.trycloudflare.com URL
"""
import argparse
import glob
import http.server
import json
import os
import socketserver
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
JST = timezone(timedelta(hours=9), "JST")  # Japan Standard Time for scan timestamps

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = os.path.join(BASE_DIR, "data", "reports")
SIGNALS_FILE = os.path.join(BASE_DIR, "data", "signals.json")

# ── Scan state ──────────────────────────────────────────────────────────
scan_lock = threading.Lock()
scan_running = False
scan_last_run = None
scan_last_error = None
scan_interval = 6.0  # hours between auto-scans (0 = off)


def run_scan_background():
    global scan_running, scan_last_run, scan_last_error
    with scan_lock:
        if scan_running:
            return {"status": "already_running"}
        scan_running = True
        scan_last_error = None

    def _worker():
        global scan_running, scan_last_run, scan_last_error
        try:
            result = subprocess.run(
                [sys.executable, os.path.join(BASE_DIR, "run_pipeline.py")],
                capture_output=True, text=True, timeout=120, cwd=BASE_DIR,
            )
            scan_last_run = datetime.now(JST).isoformat()
            if result.returncode != 0:
                scan_last_error = result.stderr[-500:] if result.stderr else "unknown error"
        except Exception as e:
            scan_last_error = str(e)
        finally:
            scan_running = False

    threading.Thread(target=_worker, daemon=True).start()
    return {"status": "started"}


def get_latest_report():
    files = sorted(glob.glob(os.path.join(REPORTS_DIR, "report_*.md")))
    if not files:
        return None
    with open(files[-1], "r") as f:
        return f.read()


def get_signals():
    if os.path.exists(SIGNALS_FILE):
        with open(SIGNALS_FILE, "r") as f:
            return json.load(f)
    return []


def parse_report_cards(md):
    """Parse the markdown report into structured cards for the frontend."""
    cards = []
    lines = md.split("\n")
    current = None
    for line in lines:
        line = line.strip()
        if line.startswith("## "):
            if current:
                cards.append(current)
            title = line[3:].strip()
            action = "WATCH"
            if "BUY" in title:
                action = "BUY"
            elif "AVOID" in title:
                action = "AVOID"
            coin = title.split("—")[0].strip() if "—" in title else title
            coin = coin.lstrip("🟢🟡🔴 ").strip()
            current = {"coin": coin, "action": action, "details": [], "raw": ""}
        elif current and line.startswith("- **"):
            current["details"].append(line[2:].strip())
            current["raw"] += line[2:] + "\n"
        elif current and line.startswith("- "):
            current["details"].append(line[2:].strip())
            current["raw"] += line[2:] + "\n"
    if current:
        cards.append(current)
    return cards


# ── HTML Dashboard ──────────────────────────────────────────────────────
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Crypto Agent Team</title>
<style>
  :root {
    --bg: #0d1117; --surface: #161b22; --surface2: #21262d;
    --border: #30363d; --text: #e6edf3; --muted: #8b949e;
    --green: #3fb950; --yellow: #d29922; --red: #f85149;
    --accent: #58a6ff;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    background: var(--bg); color: var(--text); line-height: 1.5;
    padding: 16px; max-width: 600px; margin: 0 auto;
    -webkit-font-smoothing: antialiased;
  }
  header { text-align: center; padding: 20px 0 12px; }
  header h1 { font-size: 1.4rem; font-weight: 600; }
  header p { color: var(--muted); font-size: 0.85rem; margin-top: 4px; }

  .btn {
    display: block; width: 100%; padding: 14px; border: none; border-radius: 10px;
    font-size: 1rem; font-weight: 600; cursor: pointer; transition: all 0.15s;
    margin: 12px 0;
  }
  .btn-primary { background: var(--accent); color: #000; }
  .btn-primary:hover { opacity: 0.9; }
  .btn-primary:disabled { opacity: 0.4; cursor: not-allowed; }
  .btn-secondary { background: var(--surface2); color: var(--text); border: 1px solid var(--border); }

  .status-bar {
    display: flex; justify-content: space-between; align-items: center;
    padding: 10px 14px; background: var(--surface); border-radius: 8px;
    margin-bottom: 16px; font-size: 0.82rem; color: var(--muted);
  }
  .status-dot {
    width: 8px; height: 8px; border-radius: 50%; display: inline-block;
    margin-right: 6px; vertical-align: middle;
  }
  .dot-idle { background: var(--muted); }
  .dot-running { background: var(--yellow); animation: pulse 1s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }

  .summary-row {
    display: flex; gap: 8px; margin-bottom: 16px;
  }
  .summary-chip {
    flex: 1; text-align: center; padding: 12px 8px; border-radius: 10px;
    font-weight: 700; font-size: 1.1rem;
  }
  .chip-buy { background: rgba(63,185,80,0.15); color: var(--green); }
  .chip-watch { background: rgba(210,153,34,0.15); color: var(--yellow); }
  .chip-avoid { background: rgba(248,81,73,0.15); color: var(--red); }

  .card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; padding: 14px; margin-bottom: 10px;
    transition: border-color 0.15s;
  }
  .card:hover { border-color: var(--accent); }
  .card-header {
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 8px;
  }
  .coin-name { font-weight: 700; font-size: 1.05rem; }
  .action-badge {
    padding: 3px 10px; border-radius: 20px; font-size: 0.75rem;
    font-weight: 700; text-transform: uppercase;
  }
  .badge-buy { background: var(--green); color: #000; }
  .badge-watch { background: var(--yellow); color: #000; }
  .badge-avoid { background: var(--red); color: #fff; }
  .card-details { color: var(--muted); font-size: 0.85rem; }
  .card-details li { margin-bottom: 3px; list-style: none; }
  .card-details li::before { content: "· "; color: var(--accent); }

  .empty { text-align: center; padding: 40px 20px; color: var(--muted); }

  .spinner {
    display: inline-block; width: 16px; height: 16px; border: 2px solid var(--border);
    border-top-color: var(--accent); border-radius: 50%;
    animation: spin 0.6s linear infinite; vertical-align: middle; margin-right: 6px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>

<header>
  <h1>⚡ Crypto Agent Team</h1>
  <p id="lastRun">Loading...</p>
  <p id="autoInfo" style="color:var(--muted);font-size:0.78rem;margin-top:2px"></p>
</header>

<div class="status-bar">
  <span><span class="status-dot dot-idle" id="statusDot"></span><span id="statusText">Idle</span></span>
  <span id="signalCount"></span>
</div>

<button class="btn btn-primary" id="runBtn" onclick="startScan()">
  ▶ Run Full Scan
</button>

<div class="summary-row" id="summaryRow" style="display:none">
  <div class="summary-chip chip-buy" id="buyCount">0<br><small>BUY</small></div>
  <div class="summary-chip chip-watch" id="watchCount">0<br><small>WATCH</small></div>
  <div class="summary-chip chip-avoid" id="avoidCount">0<br><small>AVOID</small></div>
</div>

<div id="cards"><div class="empty">No report yet — tap Run Full Scan</div></div>

<script>
let pollTimer = null;

// Format any timestamp in Japan Standard Time. Timestamps without a timezone
// offset (older scans, generated on UTC CI runners) are treated as UTC.
function toJSTDate(ts) {
  if (typeof ts === 'string' && /T\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(ts)) ts += 'Z';
  return new Date(ts);
}
function formatJST(ts, opts) {
  const d = toJSTDate(ts);
  if (isNaN(d)) return '—';
  return d.toLocaleString('en-GB', Object.assign({
    timeZone: 'Asia/Tokyo', year: 'numeric', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit', hour12: false
  }, opts || {})) + ' JST';
}
function timeAgo(ts) {
  const mins = Math.max(0, Math.floor((Date.now() - toJSTDate(ts).getTime()) / 60000));
  if (isNaN(mins)) return '';
  if (mins < 1) return 'just now';
  if (mins < 60) return mins + ' min ago';
  const h = Math.floor(mins / 60), m = mins % 60;
  if (h < 24) return h + 'h ' + (m ? m + 'm ' : '') + 'ago';
  const d = Math.floor(h / 24);
  return d + (d === 1 ? ' day ago' : ' days ago');
}
let lastScanTs = null;
function renderLastScan() {
  const el = document.getElementById('lastRun');
  if (!el || !lastScanTs) return;
  el.textContent = 'Last scan: ' + formatJST(lastScanTs) + ' · ' + timeAgo(lastScanTs);
}
setInterval(renderLastScan, 30000);

async function startScan() {
  const btn = document.getElementById('runBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Scanning...';
  document.getElementById('statusDot').className = 'status-dot dot-running';
  document.getElementById('statusText').textContent = 'Scanning';

  try {
    await fetch('/api/run', { method: 'POST' });
    pollStatus();
  } catch(e) {
    btn.disabled = false;
    btn.textContent = '▶ Run Full Scan';
    document.getElementById('statusDot').className = 'status-dot dot-idle';
    document.getElementById('statusText').textContent = 'Error starting';
  }
}

function pollStatus() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    try {
      const r = await fetch('/api/status');
      const s = await r.json();
      if (!s.running) {
        clearInterval(pollTimer);
        pollTimer = null;
        document.getElementById('runBtn').disabled = false;
        document.getElementById('runBtn').textContent = '▶ Run Full Scan';
        document.getElementById('statusDot').className = 'status-dot dot-idle';
        document.getElementById('statusText').textContent = 'Idle';
        loadReport();
      }
    } catch(e) { /* retry */ }
  }, 2000);
}

async function loadReport() {
  try {
    const r = await fetch('/api/report');
    const data = await r.json();
    if (!data.report) {
      document.getElementById('cards').innerHTML = '<div class="empty">No report yet</div>';
      return;
    }
    lastScanTs = data.timestamp;
    renderLastScan();

    const cards = data.cards || [];
    let buy=0, watch=0, avoid=0;
    cards.forEach(c => {
      if (c.action==='BUY') buy++;
      else if (c.action==='WATCH') watch++;
      else avoid++;
    });

    if (cards.length) {
      document.getElementById('summaryRow').style.display = 'flex';
      document.getElementById('buyCount').innerHTML = buy + '<br><small>BUY</small>';
      document.getElementById('watchCount').innerHTML = watch + '<br><small>WATCH</small>';
      document.getElementById('avoidCount').innerHTML = avoid + '<br><small>AVOID</small>';
    }

    let html = '';
    cards.forEach(c => {
      const badgeClass = c.action === 'BUY' ? 'badge-buy' : c.action === 'AVOID' ? 'badge-avoid' : 'badge-watch';
      const details = (c.details||[]).map(d => '<li>' + d + '</li>').join('');
      html += '<div class="card">' +
        '<div class="card-header">' +
          '<span class="coin-name">' + c.coin + '</span>' +
          '<span class="action-badge ' + badgeClass + '">' + c.action + '</span>' +
        '</div>' +
        '<ul class="card-details">' + details + '</ul>' +
      '</div>';
    });
    document.getElementById('cards').innerHTML = html || '<div class="empty">No results</div>';
  } catch(e) {
    document.getElementById('cards').innerHTML = '<div class="empty">Failed to load report</div>';
  }
}

async function loadSignals() {
  try {
    const r = await fetch('/api/signals');
    const data = await r.json();
    document.getElementById('signalCount').textContent = data.count + ' signals';
  } catch(e) {}
  try {
    const s = await (await fetch('/api/status')).json();
    const iv = s.interval || 0;
    document.getElementById('autoInfo').textContent =
      iv > 0 ? '🤖 Auto-scan every ' + iv + 'h' : '🤖 Auto-scan off';
  } catch(e) {}
}

// Load on page open
loadReport();
loadSignals();
</script>
</body>
</html>"""


# ── HTTP Handler ────────────────────────────────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress request logging

    def _json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html, status=200):
        body = html.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            self._html(DASHBOARD_HTML)
        elif self.path == "/api/status":
            self._json({"running": scan_running, "last_run": scan_last_run,
                        "last_error": scan_last_error, "interval": scan_interval})
        elif self.path == "/api/report":
            md = get_latest_report()
            if md:
                ts = datetime.now(JST).isoformat()
                # Try to extract timestamp from filename
                files = sorted(glob.glob(os.path.join(REPORTS_DIR, "report_*.md")))
                if files:
                    fname = os.path.basename(files[-1]).replace("report_", "").replace(".md", "")
                    try:
                        ts = datetime.strptime(fname, "%Y%m%d_%H%M%S").replace(tzinfo=JST).isoformat()
                    except Exception:
                        pass
                self._json({"report": md, "cards": parse_report_cards(md), "timestamp": ts})
            else:
                self._json({"report": None, "cards": [], "timestamp": None})
        elif self.path == "/api/signals":
            signals = get_signals()
            self._json({"signals": signals, "count": len(signals)})
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/api/run":
            result = run_scan_background()
            self._json(result)
        else:
            self.send_error(404)


# ── Main ────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Crypto Agent Team — Web Dashboard")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--interval", type=float, default=6,
                    help="hours between automatic scans (0 disables). Default 6.")
    args = ap.parse_args()

    os.makedirs(REPORTS_DIR, exist_ok=True)

    global scan_interval
    scan_interval = args.interval

    # Auto-scan scheduler
    if args.interval > 0:
        def _loop():
            while True:
                time.sleep(args.interval * 3600)
                print(f"   ⏰ Auto-scan every {args.interval:g}h — running pipeline...")
                run_scan_background()

        threading.Thread(target=_loop, daemon=True).start()
        print(f"   ⏰ Auto-scan scheduled every {args.interval:g} hours (--interval to change, 0 = off)")

    with socketserver.TCPServer((args.host, args.port), Handler) as httpd:
        httpd.allow_reuse_address = True
        print(f"⚡ Crypto Agent Team — dashboard running")
        print(f"   Local:   http://localhost:{args.port}")

        # Find local IP for phone access
        try:
            import socket as _s
            s = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
            print(f"   Phone:   http://{local_ip}:{args.port}")
        except Exception:
            pass

        print(f"   Press Ctrl+C to stop\n")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")


if __name__ == "__main__":
    main()