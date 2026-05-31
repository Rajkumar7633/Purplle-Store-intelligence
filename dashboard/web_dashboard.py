#!/usr/bin/env python3
"""
Live web dashboard — FastAPI + WebSocket + HTML.
Serves a single-page dashboard with real-time metrics, funnel, anomalies,
and a Brigade Road store heatmap overlay on the actual floor plan.

Usage:
    python dashboard/web_dashboard.py --api http://localhost:8000 --port 8080
"""
import argparse
import asyncio
import base64
import json
import logging
import os
from datetime import datetime
from pathlib import Path

import requests
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response

logger = logging.getLogger(__name__)
API_URL = os.getenv("API_URL", "http://localhost:8000")

app = FastAPI(title="Store Intelligence Dashboard")

# ── Floor plan image (embedded as base64 so there are no static-file deps) ──
_FLOORPLAN_B64 = ""
_FLOORPLAN_PATH = Path(__file__).parent.parent / "data" / "store_floorplan.png"
if _FLOORPLAN_PATH.exists():
    _FLOORPLAN_B64 = base64.b64encode(_FLOORPLAN_PATH.read_bytes()).decode()


# Zone layout matching data/sample_store_layout.json (bbox_pct = [x1, y1, x2, y2])
ZONE_LAYOUT = [
    {"zone_id": "SKINCARE",     "label": "Skincare",      "bbox": [0.06, 0.00, 0.75, 0.18]},
    {"zone_id": "ACCESSORIES",  "label": "Accessories",   "bbox": [0.75, 0.00, 0.86, 0.18]},
    {"zone_id": "FRAGRANCE",    "label": "Fragrance",     "bbox": [0.27, 0.22, 0.36, 0.73]},
    {"zone_id": "NAIL_CORNER",  "label": "Nail Corner",   "bbox": [0.36, 0.22, 0.44, 0.73]},
    {"zone_id": "MAKEUP",       "label": "Makeup",        "bbox": [0.06, 0.75, 0.57, 1.00]},
    {"zone_id": "HAIRCARE",     "label": "Haircare",      "bbox": [0.27, 0.75, 0.87, 1.00]},
    {"zone_id": "PMU",          "label": "PMU Studio",    "bbox": [0.88, 0.48, 0.97, 0.80]},
    {"zone_id": "BILLING",      "label": "Cash Counter",  "bbox": [0.76, 0.18, 0.88, 0.68]},
]
ZONE_LAYOUT_JS = json.dumps(ZONE_LAYOUT)

DASHBOARD_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Purplle Store Intelligence — Brigade Road</title>
<style>
  :root {{
    --bg:#0d1117; --card:#161b22; --accent:#a855f7; --green:#3fb950;
    --yellow:#d29922; --red:#f85149; --text:#e6edf3; --dim:#8b949e;
    --purplle:#7c3aed;
  }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ background:var(--bg); color:var(--text); font-family:'Segoe UI',sans-serif; padding:20px; }}
  h1 {{ color:var(--accent); font-size:1.35rem; margin-bottom:2px; }}
  .subtitle {{ color:var(--dim); font-size:0.82rem; margin-bottom:18px; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:14px; margin-bottom:20px; }}
  .card {{ background:var(--card); border-radius:8px; padding:18px; border:1px solid #30363d; }}
  .card .label {{ font-size:0.72rem; color:var(--dim); text-transform:uppercase; letter-spacing:.08em; }}
  .card .value {{ font-size:1.9rem; font-weight:700; margin-top:4px; color:var(--green); }}
  .card .value.warn {{ color:var(--yellow); }}
  .card .value.danger {{ color:var(--red); }}
  .section {{ background:var(--card); border-radius:8px; padding:18px; border:1px solid #30363d; margin-bottom:18px; }}
  .section h2 {{ font-size:0.85rem; color:var(--dim); margin-bottom:12px; text-transform:uppercase; letter-spacing:.06em; }}
  .funnel-bar {{ margin-bottom:8px; }}
  .funnel-label {{ display:flex; justify-content:space-between; font-size:0.78rem; margin-bottom:3px; }}
  .bar-track {{ background:#21262d; border-radius:4px; height:18px; }}
  .bar-fill {{ height:100%; border-radius:4px; background:var(--accent); transition:width .5s ease; }}
  .anomaly {{ display:flex; align-items:flex-start; gap:8px; margin-bottom:9px; font-size:0.8rem; }}
  .badge {{ padding:2px 8px; border-radius:12px; font-size:0.68rem; font-weight:700; flex-shrink:0; }}
  .badge.INFO {{ background:#1f3a5f; color:var(--accent); }}
  .badge.WARN {{ background:#3b2f00; color:var(--yellow); }}
  .badge.CRITICAL {{ background:#3d0a0a; color:var(--red); }}
  .status-dot {{ width:8px; height:8px; border-radius:50%; background:var(--green); display:inline-block; margin-right:6px; }}
  .status-dot.stale {{ background:var(--red); animation:blink 1s infinite; }}
  @keyframes blink {{ 50%{{opacity:0}} }}
  /* Floor plan */
  #map-wrap {{ position:relative; width:100%; max-width:960px; margin:0 auto; }}
  #map-wrap img {{ width:100%; display:block; border-radius:6px; }}
  #zone-overlay {{ position:absolute; top:0; left:0; width:100%; height:100%; }}
  .zone-box {{ position:absolute; border:2px solid rgba(168,85,247,0.7); border-radius:4px;
               background:rgba(168,85,247,0.12); cursor:pointer; transition:background .3s; }}
  .zone-box:hover {{ background:rgba(168,85,247,0.35); }}
  .zone-box.heat-high {{ border-color:rgba(248,81,73,0.85); background:rgba(248,81,73,0.22); }}
  .zone-box.heat-mid  {{ border-color:rgba(210,153,34,0.85); background:rgba(210,153,34,0.20); }}
  .zone-box.heat-low  {{ border-color:rgba(63,185,80,0.70); background:rgba(63,185,80,0.12); }}
  .zone-tag {{ position:absolute; bottom:3px; left:4px; font-size:0.58rem; color:#e6edf3;
               background:rgba(0,0,0,0.55); padding:1px 4px; border-radius:3px; pointer-events:none; white-space:nowrap; }}
  .score-badge {{ position:absolute; top:3px; right:4px; font-size:0.6rem; font-weight:700;
                  background:rgba(0,0,0,0.6); padding:1px 4px; border-radius:3px; pointer-events:none; }}
  .legend {{ display:flex; gap:14px; margin-top:8px; font-size:0.72rem; color:var(--dim); }}
  .legend-dot {{ width:10px; height:10px; border-radius:2px; display:inline-block; margin-right:4px; vertical-align:middle; }}
  footer {{ color:var(--dim); font-size:0.72rem; margin-top:16px; }}
</style>
</head>
<body>
<h1>&#9679; Purplle Store Intelligence — Brigade Road, Bangalore</h1>
<div class="subtitle" id="subtitle">Connecting...</div>

<div class="grid" id="metrics-grid">
  <div class="card"><div class="label">Unique Visitors</div><div class="value" id="unique-visitors">—</div></div>
  <div class="card"><div class="label">Conversion Rate</div><div class="value" id="conversion-rate">—</div></div>
  <div class="card"><div class="label">Queue Depth</div><div class="value" id="queue-depth">—</div></div>
  <div class="card"><div class="label">Abandonment Rate</div><div class="value" id="abandonment-rate">—</div></div>
  <div class="card"><div class="label">Total Entries</div><div class="value" id="total-entries">—</div></div>
  <div class="card"><div class="label">Total Exits</div><div class="value" id="total-exits">—</div></div>
</div>

<div class="section">
  <h2>Store Heatmap — Brigade Road Floor Plan</h2>
  <div id="map-wrap">
    <img id="floorplan" src="data:image/png;base64,{_FLOORPLAN_B64}" alt="Store Floor Plan">
    <div id="zone-overlay"></div>
  </div>
  <div class="legend">
    <span><span class="legend-dot" style="background:rgba(248,81,73,0.7)"></span>High traffic</span>
    <span><span class="legend-dot" style="background:rgba(210,153,34,0.7)"></span>Medium traffic</span>
    <span><span class="legend-dot" style="background:rgba(63,185,80,0.6)"></span>Low traffic</span>
    <span><span class="legend-dot" style="background:rgba(168,85,247,0.3)"></span>No data</span>
  </div>
</div>

<div class="section">
  <h2>Conversion Funnel</h2>
  <div id="funnel-stages"></div>
</div>

<div class="section">
  <h2>Active Anomalies</h2>
  <div id="anomaly-list"><span style="color:var(--dim)">No active anomalies</span></div>
</div>

<footer id="footer">Waiting for data...</footer>

<script>
const ZONES = {ZONE_LAYOUT_JS};
const scoreMap = {{}};

function heatClass(score) {{
  if (score == null) return '';
  if (score >= 66) return 'heat-high';
  if (score >= 33) return 'heat-mid';
  return 'heat-low';
}}

function buildZoneOverlay() {{
  const overlay = document.getElementById('zone-overlay');
  const img = document.getElementById('floorplan');
  overlay.innerHTML = '';
  const W = img.offsetWidth, H = img.offsetHeight;
  if (!W || !H) return;

  ZONES.forEach(z => {{
    const [x1, y1, x2, y2] = z.bbox;
    const div = document.createElement('div');
    div.className = 'zone-box ' + heatClass(scoreMap[z.zone_id]);
    div.style.left   = (x1 * 100) + '%';
    div.style.top    = (y1 * 100) + '%';
    div.style.width  = ((x2 - x1) * 100) + '%';
    div.style.height = ((y2 - y1) * 100) + '%';
    div.title = z.label + (scoreMap[z.zone_id] != null ? ' — score: ' + scoreMap[z.zone_id].toFixed(0) : '');

    const tag = document.createElement('span');
    tag.className = 'zone-tag';
    tag.textContent = z.label;
    div.appendChild(tag);

    if (scoreMap[z.zone_id] != null) {{
      const badge = document.createElement('span');
      badge.className = 'score-badge';
      badge.style.color = heatClass(scoreMap[z.zone_id]) === 'heat-high' ? '#f85149' :
                          heatClass(scoreMap[z.zone_id]) === 'heat-mid'  ? '#d29922' : '#3fb950';
      badge.textContent = scoreMap[z.zone_id].toFixed(0);
      div.appendChild(badge);
    }}
    overlay.appendChild(div);
  }});
}}

window.addEventListener('resize', buildZoneOverlay);
document.getElementById('floorplan').addEventListener('load', buildZoneOverlay);

const wsProtocol = location.protocol === 'https:' ? 'wss' : 'ws';
const ws = new WebSocket(`${{wsProtocol}}://${{location.host}}/ws/dashboard`);

ws.onmessage = (e) => {{
  const data = JSON.parse(e.data);
  const {{ metrics, funnel, anomalies, heatmap, store_id, ts }} = data;

  document.getElementById('subtitle').innerHTML =
    `<span class="status-dot"></span>Purplle — Brigade Road &nbsp;|&nbsp; ${{ts}}`;

  if (metrics) {{
    document.getElementById('unique-visitors').textContent = metrics.unique_visitors ?? '—';
    const conv = metrics.conversion_rate;
    const convEl = document.getElementById('conversion-rate');
    convEl.textContent = conv != null ? (conv * 100).toFixed(1) + '%' : '—';
    convEl.className = 'value' + (conv < 0.05 ? ' warn' : '');

    const qd = metrics.current_queue_depth;
    const qdEl = document.getElementById('queue-depth');
    qdEl.textContent = qd ?? '—';
    qdEl.className = 'value' + (qd >= 5 ? ' danger' : qd >= 3 ? ' warn' : '');

    const ab = metrics.abandonment_rate;
    const abEl = document.getElementById('abandonment-rate');
    abEl.textContent = ab != null ? (ab * 100).toFixed(1) + '%' : '—';
    abEl.className = 'value' + (ab > 0.3 ? ' warn' : '');

    document.getElementById('total-entries').textContent = metrics.total_entries ?? '—';
    document.getElementById('total-exits').textContent   = metrics.total_exits   ?? '—';
  }}

  if (heatmap && heatmap.zones) {{
    heatmap.zones.forEach(z => {{ scoreMap[z.zone_id] = z.normalized_score; }});
    buildZoneOverlay();
  }}

  if (funnel && funnel.stages && funnel.stages.length) {{
    const maxCount = Math.max(...funnel.stages.map(s => s.count), 1);
    document.getElementById('funnel-stages').innerHTML = funnel.stages.map(s => `
      <div class="funnel-bar">
        <div class="funnel-label">
          <span>${{s.stage}}</span>
          <span>${{s.count}} visitors ${{s.drop_off_pct > 0 ? '(-' + s.drop_off_pct.toFixed(1) + '%)' : ''}}</span>
        </div>
        <div class="bar-track"><div class="bar-fill" style="width:${{(s.count / maxCount * 100).toFixed(1)}}%"></div></div>
      </div>`).join('');
  }}

  if (anomalies && anomalies.length > 0) {{
    document.getElementById('anomaly-list').innerHTML = anomalies.slice(0, 5).map(a => `
      <div class="anomaly">
        <span class="badge ${{a.severity}}">${{a.severity}}</span>
        <span>${{a.description}}</span>
      </div>`).join('');
  }} else {{
    document.getElementById('anomaly-list').innerHTML = '<span style="color:var(--dim)">No active anomalies</span>';
  }}

  document.getElementById('footer').textContent = `Auto-refresh: 5s | ${{ts}}`;
}};

ws.onclose = () => {{
  document.getElementById('subtitle').innerHTML =
    '<span class="status-dot stale"></span>Disconnected — reconnecting...';
  setTimeout(() => location.reload(), 3000);
}};
</script>
</body></html>
"""


@app.get("/", response_class=HTMLResponse)
async def dashboard_page():
    return DASHBOARD_HTML


@app.websocket("/ws/dashboard")
async def dashboard_ws(websocket: WebSocket, store_id: str = "STORE_BLR_002"):
    await websocket.accept()
    try:
        while True:
            payload = _fetch_all(store_id)
            await websocket.send_text(json.dumps(payload))
            await asyncio.sleep(5)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("WebSocket error: %s", exc)


def _fetch_all(store_id: str) -> dict:
    def safe_get(path):
        try:
            r = requests.get(f"{API_URL}{path}", timeout=5)
            return r.json() if r.status_code == 200 else {}
        except Exception:
            return {}

    return {
        "store_id": store_id,
        "ts": datetime.now().strftime("%H:%M:%S"),
        "metrics":   safe_get(f"/stores/{store_id}/metrics"),
        "funnel":    safe_get(f"/stores/{store_id}/funnel"),
        "heatmap":   safe_get(f"/stores/{store_id}/heatmap"),
        "anomalies": safe_get(f"/stores/{store_id}/anomalies").get("active_anomalies", []),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--api",   default=os.getenv("API_URL", "http://localhost:8000"))
    parser.add_argument("--port",  type=int, default=8080)
    parser.add_argument("--store", default="STORE_BLR_002")
    args = parser.parse_args()
    API_URL = args.api
    uvicorn.run(app, host="0.0.0.0", port=args.port)
