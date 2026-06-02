#!/usr/bin/env python3
"""Convert a raw sample_events JSONL file into the Store Intelligence event schema.

Usage:
    python scripts/convert_sample_events.py \
      "../sample_eventsbe42122.jsonl" \
      output/sample_events_converted.jsonl
"""
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

LEGACY_EVENT_TYPE_MAP = {
    "entry": "ENTRY",
    "exit": "EXIT",
    "zone_entered": "ZONE_ENTER",
    "zone_exited": "ZONE_EXIT",
    "queue_completed": "BILLING_QUEUE_JOIN",
    "queue_abandoned": "BILLING_QUEUE_ABANDON",
}


def parse_timestamp(entry: Dict[str, Any]) -> Optional[str]:
    if entry.get("event_timestamp"):
        ts = entry["event_timestamp"]
        try:
            return datetime.fromisoformat(ts).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            return None
    for key in ("event_time", "queue_join_ts", "queue_exit_ts", "zone_hotspot_x"):
        if entry.get(key):
            ts = entry[key]
            try:
                return datetime.fromisoformat(ts).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                continue
    return None


def make_event(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    event_type = raw.get("event_type")
    mapped = LEGACY_EVENT_TYPE_MAP.get(event_type)
    if not mapped:
        return None

    store_id = raw.get("store_code") or raw.get("store_id") or raw.get("store_code")
    if store_id and store_id.startswith("ST"):
        store_id = store_id.replace("ST", "STORE_")

    if not store_id:
        store_id = "STORE_UNKNOWN"

    timestamp = parse_timestamp(raw)
    if not timestamp:
        return None

    visitor_id = raw.get("id_token") or raw.get("track_id") or f"VIS_{raw.get('group_id', uuid.uuid4().hex[:6])}"
    zone_id = raw.get("zone_id") or raw.get("zone_name")
    confidence = 0.75 if raw.get("is_staff") is None else 0.95
    is_staff = bool(raw.get("is_staff", False))

    metadata = {
        "queue_depth": raw.get("queue_position_at_join") if event_type in ("queue_completed", "queue_abandoned") else None,
        "sku_zone": zone_id,
        "session_seq": raw.get("session_seq") or 0,
    }

    dwell_ms = 0
    if event_type == "zone_exited" and raw.get("wait_seconds") is not None:
        dwell_ms = int(float(raw.get("wait_seconds")) * 1000)

    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": raw.get("camera_id", "UNKNOWN"),
        "visitor_id": str(visitor_id),
        "event_type": mapped,
        "timestamp": timestamp,
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": round(float(raw.get("confidence", 0.75)), 4),
        "metadata": metadata,
    }


def convert(input_path: Path, output_path: Path) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with input_path.open("r", encoding="utf-8") as inf, output_path.open("w", encoding="utf-8") as outf:
        for line in inf:
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            event = make_event(raw)
            if event is None:
                continue
            outf.write(json.dumps(event) + "\n")
            count += 1
    return count


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python scripts/convert_sample_events.py INPUT_JSONL OUTPUT_JSONL")
        sys.exit(1)
    inp = Path(sys.argv[1])
    out = Path(sys.argv[2])
    total = convert(inp, out)
    print(f"Converted {total} events from {inp} to {out}")
