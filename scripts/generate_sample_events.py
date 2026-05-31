#!/usr/bin/env python3
"""
Generates realistic sample events for testing and API validation.
Simulates a 20-minute store session with realistic visitor behavior.

Usage:
    python scripts/generate_sample_events.py --store STORE_BLR_002 --output output/sample_events.jsonl
"""
import argparse
import json
import random
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path


ZONES = ["SKINCARE", "HAIRCARE", "FRAGRANCE", "BODYCARE", "BILLING"]
STORE_ID = "STORE_BLR_002"
CAMERAS = {
    "entry": "CAM_ENTRY_01",
    "floor": "CAM_FLOOR_01",
    "billing": "CAM_BILLING_01",
}


def make_event(store_id, camera_id, visitor_id, event_type, ts, zone_id=None,
               dwell_ms=0, is_staff=False, conf=None, queue_depth=None,
               sku_zone=None, session_seq=1):
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": camera_id,
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": conf if conf is not None else round(random.uniform(0.7, 0.97), 3),
        "metadata": {
            "queue_depth": queue_depth,
            "sku_zone": sku_zone or zone_id,
            "session_seq": session_seq,
        }
    }


def simulate_visitor(vis_id, start_ts, store_id, is_staff=False, reentry=False):
    events = []
    t = start_ts
    seq = 0

    # Entry
    seq += 1
    events.append(make_event(store_id, CAMERAS["entry"], vis_id, "ENTRY", t,
                             is_staff=is_staff, session_seq=seq))
    t += timedelta(seconds=random.randint(5, 15))

    # If staff: visit many zones
    num_zones = random.randint(4, 6) if is_staff else random.randint(1, 3)
    zones_to_visit = random.sample(ZONES[:-1], min(num_zones, 4))

    for zone in zones_to_visit:
        seq += 1
        events.append(make_event(store_id, CAMERAS["floor"], vis_id, "ZONE_ENTER", t,
                                 zone_id=zone, is_staff=is_staff, session_seq=seq))
        dwell = random.randint(20000, 120000)  # 20s - 2min
        t += timedelta(milliseconds=dwell)

        # Emit ZONE_DWELL if > 30s
        if dwell > 30000:
            seq += 1
            events.append(make_event(store_id, CAMERAS["floor"], vis_id, "ZONE_DWELL",
                                     t - timedelta(milliseconds=dwell // 2),
                                     zone_id=zone, dwell_ms=dwell, is_staff=is_staff,
                                     session_seq=seq))

        seq += 1
        events.append(make_event(store_id, CAMERAS["floor"], vis_id, "ZONE_EXIT", t,
                                 zone_id=zone, dwell_ms=dwell, is_staff=is_staff, session_seq=seq))

    # 40% chance to go to billing (customers only)
    if not is_staff and random.random() < 0.4:
        queue_depth = random.randint(1, 4)
        seq += 1
        event_type = "BILLING_QUEUE_JOIN" if queue_depth > 1 else "ZONE_ENTER"
        events.append(make_event(store_id, CAMERAS["billing"], vis_id, event_type, t,
                                 zone_id="BILLING", queue_depth=queue_depth, session_seq=seq))
        dwell = random.randint(30000, 180000)
        t += timedelta(milliseconds=dwell)

        # 70% of billing visitors complete purchase, 30% abandon
        if random.random() < 0.3:
            seq += 1
            events.append(make_event(store_id, CAMERAS["billing"], vis_id,
                                     "BILLING_QUEUE_ABANDON", t,
                                     zone_id="BILLING", session_seq=seq))

    # Exit
    t += timedelta(seconds=random.randint(5, 30))
    seq += 1
    events.append(make_event(store_id, CAMERAS["entry"], vis_id, "EXIT", t,
                             is_staff=is_staff, session_seq=seq))

    return events, t


def generate(store_id: str, start_time: datetime, duration_minutes: int = 20) -> list:
    all_events = []
    t = start_time

    # Add 2 staff members
    for i in range(2):
        vis_id = f"VIS_staff{i:02d}"
        events, _ = simulate_visitor(vis_id, t + timedelta(minutes=i * 2), store_id, is_staff=True)
        all_events.extend(events)

    # Simulate 30-50 customer sessions
    num_visitors = random.randint(30, 50)
    end_time = start_time + timedelta(minutes=duration_minutes)

    for i in range(num_visitors):
        vis_id = f"VIS_{uuid.uuid4().hex[:6]}"
        visitor_start = start_time + timedelta(seconds=random.randint(0, duration_minutes * 60 - 120))
        events, _ = simulate_visitor(vis_id, visitor_start, store_id, is_staff=False)
        all_events.extend(events)

    # Add 3 re-entry cases
    for i in range(3):
        vis_id = f"VIS_reentry{i:02d}"
        first_t = start_time + timedelta(minutes=2 + i * 3)
        events1, exit_t = simulate_visitor(vis_id, first_t, store_id)
        all_events.extend(events1)

        # Re-entry 3-5 minutes later
        reentry_t = exit_t + timedelta(minutes=random.randint(3, 5))
        all_events.append(make_event(store_id, CAMERAS["entry"], vis_id, "REENTRY", reentry_t,
                                     session_seq=20))
        events2, _ = simulate_visitor(vis_id, reentry_t, store_id)
        all_events.extend(events2)

    # Sort by timestamp
    all_events.sort(key=lambda e: e["timestamp"])
    return all_events


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", default="STORE_BLR_002")
    parser.add_argument("--output", default="output/sample_events.jsonl")
    parser.add_argument("--start", default="2026-03-03T09:00:00Z")
    parser.add_argument("--duration", type=int, default=20)
    args = parser.parse_args()

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    start = datetime.fromisoformat(args.start.replace("Z", "+00:00"))
    events = generate(args.store, start, args.duration)

    with open(args.output, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")

    print(f"Generated {len(events)} events → {args.output}")


if __name__ == "__main__":
    main()
