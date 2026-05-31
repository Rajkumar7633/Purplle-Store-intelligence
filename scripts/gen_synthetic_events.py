"""Generate synthetic ENTRY/EXIT/BILLING events to supplement real CCTV detections."""
import json
import uuid
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

OUT_DIR = Path("/output")
STORE_ID = "STORE_BLR_002"
BASE_TIME = datetime(2026, 3, 3, 9, 0, 0, tzinfo=timezone.utc)

# Collect real visitor IDs from floor cameras
real_visitors = set()
for cam in ["CAM_FLOOR_01", "CAM_FLOOR_02", "CAM_FLOOR_03"]:
    f = OUT_DIR / f"events_{cam}.jsonl"
    if f.exists():
        for line in f.read_text().splitlines():
            ev = json.loads(line)
            vid = ev.get("visitor_id", "")
            if vid and not ev.get("is_staff", False):
                real_visitors.add(vid)

real_visitors = sorted(real_visitors)
print(f"Found {len(real_visitors)} unique non-staff visitors from floor cameras")

events = []

def make_event(event_type, camera_id, visitor_id, ts_offset_min, **kwargs):
    ts = BASE_TIME + timedelta(minutes=ts_offset_min)
    ev = {
        "event_id": str(uuid.uuid4()),
        "store_id": STORE_ID,
        "camera_id": camera_id,
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "zone_id": kwargs.get("zone_id", None),
        "dwell_ms": kwargs.get("dwell_ms", 0),
        "is_staff": kwargs.get("is_staff", False),
        "confidence": round(random.uniform(0.60, 0.92), 4),
        "metadata": {
            "queue_depth": kwargs.get("queue_depth", None),
            "sku_zone": kwargs.get("sku_zone", None),
            "session_seq": kwargs.get("session_seq", 1),
        }
    }
    return ev

# 1. Generate STORE_ENTER for all real visitors at staggered times
for i, vid in enumerate(real_visitors):
    offset = random.uniform(0, 30)
    events.append(make_event("ENTRY", "CAM_ENTRY_01", vid, offset))

# 2. Generate extra walk-in visitors (10 more) who may not reach floor cameras
extra_visitors = [f"VIS_{uuid.uuid4().hex[:6]}" for _ in range(10)]
for i, vid in enumerate(extra_visitors):
    offset = random.uniform(5, 60)
    events.append(make_event("ENTRY", "CAM_ENTRY_01", vid, offset))

# 3. EXIT for about 60% of all visitors
all_visitors = real_visitors + extra_visitors
exiting = random.sample(all_visitors, k=int(len(all_visitors) * 0.6))
for vid in exiting:
    offset = random.uniform(30, 90)
    events.append(make_event("EXIT", "CAM_ENTRY_01", vid, offset))

# 4. BILLING events — take 40% of real visitors through billing queue
billing_visitors = random.sample(real_visitors, k=max(1, int(len(real_visitors) * 0.4)))
queue_depth = 0
for i, vid in enumerate(billing_visitors):
    join_offset = 25 + i * 3 + random.uniform(0, 2)
    queue_depth = min(queue_depth + 1, 8)
    events.append(make_event("BILLING_QUEUE_JOIN", "CAM_BILLING_01", vid,
                             join_offset, zone_id="BILLING", queue_depth=queue_depth,
                             sku_zone="BILLING"))
    leave_offset = join_offset + random.uniform(4, 12)
    queue_depth = max(0, queue_depth - 1)
    events.append(make_event("BILLING_QUEUE_ABANDON", "CAM_BILLING_01", vid,
                             leave_offset, zone_id="BILLING", queue_depth=queue_depth,
                             sku_zone="BILLING"))

# 5. One spike event — simulate queue_depth >= 5
spike_visitors = [f"VIS_{uuid.uuid4().hex[:6]}" for _ in range(6)]
for i, vid in enumerate(spike_visitors):
    events.append(make_event("BILLING_QUEUE_JOIN", "CAM_BILLING_01", vid,
                             50 + i * 0.5, zone_id="BILLING", queue_depth=i+1,
                             sku_zone="BILLING"))

# Sort by timestamp
events.sort(key=lambda e: e["timestamp"])

out_file = OUT_DIR / "events_synthetic.jsonl"
with out_file.open("w") as f:
    for ev in events:
        f.write(json.dumps(ev) + "\n")

print(f"Written {len(events)} synthetic events → {out_file}")
