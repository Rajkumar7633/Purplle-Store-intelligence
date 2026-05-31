"""
Re-date all events from 2026-03-03 to today (2026-05-31).
Preserves relative offsets (time-of-day) within the store day.
Also generates a fresh events_all.jsonl.
"""
import json, sys
from datetime import datetime, timezone
from pathlib import Path

OUT_DIR = Path("/output")
TODAY = "2026-05-31"  # target date

def redate_line(line: str) -> str:
    ev = json.loads(line)
    ts = ev["timestamp"]  # e.g. "2026-03-03T09:02:00Z"
    # Replace date part, keep time part
    time_part = ts[10:]  # "T09:02:00Z"
    ev["timestamp"] = TODAY + time_part
    return json.dumps(ev)

all_events = []
sources = [
    "events_CAM_FLOOR_01.jsonl",
    "events_CAM_FLOOR_02.jsonl",
    "events_CAM_FLOOR_03.jsonl",
    "events_synthetic.jsonl",
]

for src in sources:
    f = OUT_DIR / src
    if not f.exists():
        print(f"  SKIP: {src} not found")
        continue
    lines = [l for l in f.read_text().splitlines() if l.strip()]
    redated = [redate_line(l) for l in lines]
    all_events.extend(redated)
    print(f"  {src}: {len(redated)} events re-dated")

# Sort by timestamp
all_events.sort(key=lambda l: json.loads(l)["timestamp"])

out = OUT_DIR / "events_all.jsonl"
out.write_text("\n".join(all_events) + "\n")
print(f"\nTotal: {len(all_events)} events -> {out}")
