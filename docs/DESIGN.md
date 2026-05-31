# System Architecture — Store Intelligence API

## Overview

This system transforms raw CCTV footage into real-time retail analytics. It is composed of four
stages: a computer vision detection pipeline, a structured event stream, a real-time intelligence
API, and a live dashboard.

```
Raw CCTV Clips
     │
     ▼
┌─────────────────────────────────────────────────────┐
│  Detection Layer (pipeline/)                        │
│                                                     │
│  ┌──────────┐   ┌───────────┐   ┌───────────────┐  │
│  │ YOLOv8n  │ → │ ByteTrack │ → │ Zone Classify │  │
│  └──────────┘   └───────────┘   └───────────────┘  │
│       ↓               ↓               ↓             │
│  ┌──────────┐   ┌───────────┐   ┌───────────────┐  │
│  │ Staff    │   │  Re-ID    │   │ Event Schema  │  │
│  │ Detector │   │ Engine    │   │ Construction  │  │
│  └──────────┘   └───────────┘   └───────────────┘  │
└─────────────────────────────────────────────────────┘
     │
     ▼ JSONL events + POST /events/ingest
┌─────────────────────────────────────────────────────┐
│  Intelligence API (app/)                            │
│                                                     │
│  POST /events/ingest  (idempotent, batch 500)       │
│  GET  /stores/{id}/metrics                          │
│  GET  /stores/{id}/funnel                           │
│  GET  /stores/{id}/heatmap                          │
│  GET  /stores/{id}/anomalies                        │
│  GET  /health                                       │
│                                                     │
│  Storage: SQLite + SQLAlchemy async (aiosqlite)     │
└─────────────────────────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────────────────────┐
│  Live Dashboard (dashboard/)                        │
│  ─ web_dashboard.py   FastAPI + WebSocket + HTML    │
│  ─ terminal_dashboard.py   Rich terminal UI         │
└─────────────────────────────────────────────────────┘
```

---

## Detection Layer

### Model: YOLOv8n + ByteTrack

YOLOv8n (nano variant) runs at ~30 FPS on CPU, sufficient for 15 FPS CCTV input. The `model.track()`
call activates the integrated ByteTrack multi-object tracker, which assigns persistent `track_id`s
across frames without a separate tracking step.

**Frame sampling**: We process every N-th frame where N = fps / 7.5, giving ~7.5 effective detections
per second. This balances accuracy against compute on CPU-only deployments.

### Entry/Exit Detection

A virtual line is placed at `entry_line_y` (normalized y coordinate, configurable per camera in
`store_layout.json`). Line crossing is detected by tracking the bounding-box centroid's y-coordinate
across consecutive frames:
- `prev_cy < line_y and curr_cy >= line_y` → ENTRY
- `prev_cy >= line_y and curr_cy < line_y` → EXIT

This is more robust than zone-based entry detection because it handles partial occlusion at the
threshold: a person who is 50% occluded still has a centroid that crosses the line.

### Zone Classification

Zone polygons (or bounding boxes for simpler deployments) are loaded from `store_layout.json`.
The `shapely` library performs point-in-polygon tests for arbitrary zone shapes.
Fallback: axis-aligned bounding boxes when shapely is not available.

### Staff Detection

Two-stage classification:
1. **Primary**: HSV color histogram matching against known staff uniform colors (configurable navy
   blue range in `staff_detector.py`). If >25% of the person crop matches uniform colors, flagged
   as staff.
2. **Heuristic**: If a track visits 5+ distinct zones with 10+ zone events, it is classified as
   staff (customers typically visit 1-3 zones).

Staff events are emitted with `is_staff=true` and excluded from all customer-facing metrics.

### Person Re-ID

Appearance features are extracted as a concatenated HSV color histogram (18+8+8 bins = 34 dims,
normalized). Similarity is computed using Bhattacharyya distance (1 - distance gives similarity in
[0,1]). A threshold of 0.72 was chosen empirically — low enough to catch same-person re-entries
(same clothes, lighting changes) while avoiding false matches between similar-looking customers.

**Re-entry logic**: When a new track appears, its feature is compared against the gallery of
recently-exited visitors. A match above threshold with a visitor seen in the last 120 seconds
generates a `REENTRY` event and reuses the existing `visitor_id`.

### Group Entry Handling

YOLOv8 detects individual bounding boxes per person. Three people entering simultaneously each get
their own bounding box and their own ByteTrack `track_id`, generating 3 separate ENTRY events.
This is the correct behaviour — the challenge of group entry is a detection challenge (occlusion),
not a tracking challenge. If two people are fully occluded into one box, confidence degrades and
both are flagged at lower confidence.

---

## Event Stream Schema

Events follow a fixed schema (see `app/models.py`):
- `event_id`: UUIDv4 — used for idempotent deduplication
- `visitor_id`: Re-ID token, format `VIS_xxxxxx`
- `event_type`: One of ENTRY | EXIT | ZONE_ENTER | ZONE_EXIT | ZONE_DWELL | BILLING_QUEUE_JOIN | BILLING_QUEUE_ABANDON | REENTRY
- `is_staff`: Classification flag — never filtered at emission, always exposed for API-layer filtering
- `confidence`: Raw detection confidence — low-confidence events are emitted with their real score, never suppressed

---

## Intelligence API

### Storage: SQLite + SQLAlchemy async

SQLite was chosen for zero-configuration deployment. The async driver (`aiosqlite`) allows FastAPI to
handle concurrent requests without blocking. The schema uses compound indexes on `(store_id, timestamp)`
for efficient time-range queries. Migration to PostgreSQL requires only changing `DATABASE_URL`.

### Metrics Computation

All metrics are computed in real time from the event store on every request. There is no metrics cache:
- `unique_visitors`: COUNT DISTINCT visitor_id where event_type=ENTRY and is_staff=False
- `conversion_rate`: Visitors in billing zone within 5 minutes before a POS transaction / total visitors
- `queue_depth`: Most recent BILLING_QUEUE_JOIN.queue_depth
- `abandonment_rate`: BILLING_QUEUE_ABANDON count / BILLING_QUEUE_JOIN count

### Conversion Correlation

The POS data provides timestamps but no customer_id. Correlation is time-window based: a visitor who
was in the billing zone in the 5-minute window before a POS transaction is counted as converted.
This is conservative — it may undercount if billing dwell time exceeds 5 minutes — but avoids the
opposite error of overcounting by correlating the wrong visitor.

### Funnel Logic

The funnel unit is a session (unique `visitor_id`), not raw events. Re-entries are handled by querying
for distinct `visitor_id`s at each stage — the same visitor_id appearing in multiple ENTRY events
(initial + REENTRY) still counts as 1 session.

### Anomaly Detection

Five anomaly checks run concurrently via `asyncio.gather`:
1. `BILLING_QUEUE_SPIKE`: Latest queue_depth >= configurable threshold (default 5)
2. `CONVERSION_DROP`: Today's conversion rate is >20% below 7-day average
3. `DEAD_ZONE`: Zone active today but no visits in last 30 minutes
4. `STALE_FEED`: No events received for >10 minutes
5. `EMPTY_STORE`: Zero customer entries after store has been open >1 hour

---

## AI-Assisted Decisions

### 1. ByteTrack vs DeepSORT for Multi-Object Tracking

I asked Claude: "Compare ByteTrack and DeepSORT for retail CCTV tracking with partial occlusion.
Which handles crowded scenes better?"

Claude's analysis: ByteTrack outperforms DeepSORT in crowded, occluded scenes because it uses
IoU-based matching for high-confidence detections while maintaining low-confidence "tentative"
tracklets (tracked without Re-ID appearance features). This is more robust to occlusion than
DeepSORT which can lose tracks entirely under occlusion.

**Decision**: Used ByteTrack via ultralytics integration. Agreed with AI suggestion. The built-in
integration (`model.track()`) also reduced implementation complexity significantly.

### 2. Conversion Rate Correlation Approach

I asked Claude: "Given POS data with timestamps but no customer_id, what's the most accurate
method to correlate visitors with purchases? Options: time window, zone proximity, probabilistic."

Claude suggested a Bayesian approach using zone-visit probability distributions. I considered this
but rejected it for the MVP because: (1) it requires a calibration period of historical data we
don't have, (2) it's harder to explain to non-technical stakeholders, (3) the simpler time-window
approach is transparent and auditable.

**Decision**: Time-window correlation (5 minutes before POS tx). Overrode AI's more complex suggestion.

### 3. Storage Engine Selection

Claude recommended PostgreSQL for production workloads. I chose SQLite for the submission because:
(1) it enables `docker compose up` with zero external dependencies, (2) the data volumes in this
challenge (1 store, 20-minute clips) fit comfortably in SQLite, (3) SQLAlchemy's async abstraction
makes the migration path to PostgreSQL a single config change.

**Decision**: SQLite for submission, architecture ready for PostgreSQL. Agreed with AI that PostgreSQL
is the right production choice; made a pragmatic trade-off for submission constraints.
