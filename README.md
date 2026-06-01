# Store Intelligence — Purplle Tech Challenge 2026

End-to-end retail analytics system: raw CCTV → detection pipeline → event stream → live API + dashboard.

---

## Quick Start (5 commands)

```bash
git clone <your-repo-url> store-intelligence && cd store-intelligence
cp .env.example .env
docker compose up --build -d
# Wait ~10 seconds for API to start
curl http://localhost:8000/health
```

Open the live dashboard: http://localhost:8080

---

## Deploy on Render

This repo is ready for Render deployment using `render.yaml` and the existing `Dockerfile`.

1. Push your code to GitHub on `main`.
2. Open https://dashboard.render.com/new/web-service.
3. Connect your GitHub repo and select branch `main`.
4. Choose `Docker` as the environment.
5. Render will use `render.yaml` to create two services:
   - `store-intelligence-api` → FastAPI backend
   - `store-intelligence-dashboard` → live dashboard web UI

After deployment, the public service URLs should be:

- `https://store-intelligence-api-i4yz.onrender.com`
- `https://store-intelligence-dashboard-cj5c.onrender.com`

If you only want the API, disable the dashboard service or remove the second service from `render.yaml`.

> Important: for the dashboard service use `python3`, not `python`, in the `Docker Command` / start command.

---

## Running the Detection Pipeline Against Your Clips

### Option A — Run locally (recommended for faster iteration)

```bash
# 1. Install pipeline dependencies
pip install -r requirements.pipeline.txt

# 2. Process a single clip
python pipeline/detect.py \
  --clip clips/STORE_BLR_002/entry_camera.mp4 \
  --store-id STORE_BLR_002 \
  --camera-id CAM_ENTRY_01 \
  --layout data/store_layout.json \
  --output output/events.jsonl \
  --start-time 2026-03-03T09:00:00Z \
  --api-url http://localhost:8000

# 3. Process ALL clips (entry + floor + billing) and auto-ingest
./pipeline/run.sh ./clips STORE_BLR_002 http://localhost:8000

# 4. Ingest POS transactions
python pipeline/ingest_to_api.py \
  --pos data/pos_transactions.csv \
  --api http://localhost:8000
```

### Option B — Run pipeline in Docker

```bash
docker build -f Dockerfile.pipeline -t store-pipeline .
docker run --rm \
  -v $(pwd)/clips:/clips \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/output:/app/output \
  store-pipeline \
  --clip /clips/STORE_BLR_002/entry.mp4 \
  --store-id STORE_BLR_002 \
  --camera-id CAM_ENTRY_01 \
  --layout /app/data/store_layout.json \
  --output /app/output/events.jsonl \
  --api-url http://host.docker.internal:8000
```

---

## API Endpoints

| Endpoint                     | Description                                        |
| ---------------------------- | -------------------------------------------------- |
| `POST /events/ingest`        | Ingest up to 500 events (idempotent)               |
| `GET /stores/{id}/metrics`   | Unique visitors, conversion rate, dwell, queue     |
| `GET /stores/{id}/funnel`    | Entry → Zone → Billing → Purchase with drop-off %  |
| `GET /stores/{id}/heatmap`   | Zone frequency + dwell, normalised 0-100           |
| `GET /stores/{id}/anomalies` | Queue spike, conversion drop, dead zones           |
| `GET /health`                | DB status, per-store feed lag, STALE_FEED warnings |
| `POST /pos/ingest`           | Ingest POS transactions for conversion correlation |

### Example: Check metrics

```bash
curl http://localhost:8000/stores/STORE_BLR_002/metrics | python -m json.tool
```

### Example: Ingest events

```bash
curl -X POST http://localhost:8000/events/ingest \
  -H "Content-Type: application/json" \
  -d @output/events.jsonl  # see ingest_to_api.py for batch format
```

---

## Store Layout Configuration

Edit `data/store_layout.json` to define:

- Camera entry line positions (`entry_line_y`)
- Zone names, bounding boxes, and SKU zone labels
- Camera-to-zone coverage mapping

---

## Running Tests

```bash
# Install test dependencies
pip install -r requirements.txt

# Run all tests with coverage
pytest tests/ -v --cov=app --cov-report=term-missing --cov-fail-under=70

# Run specific test file
pytest tests/test_metrics.py -v
```

---

## Live Dashboard

- **Web UI**: http://localhost:8080 (auto-refreshes every 5 seconds via WebSocket)
- **Terminal**: `python dashboard/terminal_dashboard.py --store STORE_BLR_002`

---

## Architecture

See `docs/DESIGN.md` for full architecture description.
See `docs/CHOICES.md` for engineering decision rationale.

---

## Project Structure

```
store-intelligence/
├── pipeline/
│   ├── detect.py          # YOLOv8 + ByteTrack detection
│   ├── tracker.py         # Re-ID, entry/exit, zone tracking
│   ├── zone_classifier.py # Polygon/bbox zone detection
│   ├── staff_detector.py  # Uniform color classification
│   ├── reid.py            # Appearance feature Re-ID
│   ├── emit.py            # Event schema + JSONL + API emission
│   ├── ingest_to_api.py   # Bulk ingest JSONL → API
│   └── run.sh             # One-command full pipeline run
├── app/
│   ├── main.py            # FastAPI entrypoint + all routes
│   ├── models.py          # Pydantic event + response schemas
│   ├── database.py        # SQLAlchemy async + ORM models
│   ├── ingestion.py       # Dedup + ingest logic
│   ├── metrics.py         # Real-time metrics computation
│   ├── funnel.py          # Session-based funnel
│   ├── heatmap.py         # Zone heatmap
│   ├── anomalies.py       # Anomaly detection
│   ├── health.py          # Health endpoint
│   ├── config.py          # Settings management
│   └── logging_config.py  # Structured logging middleware
├── dashboard/
│   ├── web_dashboard.py   # FastAPI WebSocket web UI
│   └── terminal_dashboard.py  # Rich terminal dashboard
├── tests/
│   ├── conftest.py        # Shared fixtures + factories
│   ├── test_ingestion.py  # Ingest tests (idempotency, dedup, batch)
│   ├── test_metrics.py    # Metrics tests (staff exclusion, conversion)
│   ├── test_funnel.py     # Funnel tests (session logic, re-entry)
│   ├── test_anomalies.py  # Anomaly detection tests
│   └── test_pipeline.py   # Pipeline unit tests (schema, reid, zones)
├── docs/
│   ├── DESIGN.md          # Architecture + AI-Assisted Decisions
│   └── CHOICES.md         # 3 key decisions with full reasoning
├── data/
│   └── sample_store_layout.json
├── docker-compose.yml
├── Dockerfile
├── Dockerfile.pipeline
├── requirements.txt
├── requirements.pipeline.txt
└── README.md
```

---

## North Star Metric

**Offline Store Conversion Rate** = Visitors who completed a purchase ÷ Total unique visitors

Every component either improves accuracy of this number (detection) or makes it actionable (API).
