#!/usr/bin/env python3
"""
Validates a JSONL events file against the required schema.
Run this on your pipeline output before submission.

Usage:
    python scripts/validate_events.py output/events.jsonl
"""
import json
import sys
import uuid
from datetime import datetime


REQUIRED_FIELDS = [
    "event_id", "store_id", "camera_id", "visitor_id",
    "event_type", "timestamp", "dwell_ms", "is_staff", "confidence", "metadata",
]
VALID_EVENT_TYPES = {
    "ENTRY", "EXIT", "ZONE_ENTER", "ZONE_EXIT", "ZONE_DWELL",
    "BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON", "REENTRY",
}


def validate(path: str) -> bool:
    errors = []
    event_ids = set()
    counts = {}

    with open(path) as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"Line {i}: Invalid JSON — {exc}")
                continue

            # Required fields
            for field in REQUIRED_FIELDS:
                if field not in e:
                    errors.append(f"Line {i}: Missing field '{field}'")

            # event_id must be UUID
            try:
                uuid.UUID(e.get("event_id", ""))
            except ValueError:
                errors.append(f"Line {i}: event_id '{e.get('event_id')}' is not a valid UUID")

            # Duplicate event_id
            eid = e.get("event_id")
            if eid in event_ids:
                errors.append(f"Line {i}: Duplicate event_id '{eid}'")
            event_ids.add(eid)

            # event_type must be valid
            et = e.get("event_type")
            if et not in VALID_EVENT_TYPES:
                errors.append(f"Line {i}: Invalid event_type '{et}'")
            counts[et] = counts.get(et, 0) + 1

            # timestamp must be parseable ISO-8601
            ts = e.get("timestamp", "")
            try:
                datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                errors.append(f"Line {i}: Invalid timestamp '{ts}'")

            # confidence must be [0, 1]
            conf = e.get("confidence")
            if conf is not None and not (0.0 <= conf <= 1.0):
                errors.append(f"Line {i}: confidence {conf} out of range [0,1]")

            # is_staff must be bool
            if not isinstance(e.get("is_staff"), bool):
                errors.append(f"Line {i}: is_staff must be a boolean")

            # metadata must be a dict
            if not isinstance(e.get("metadata"), dict):
                errors.append(f"Line {i}: metadata must be an object")

    print(f"\nValidation report for: {path}")
    print(f"Total events: {len(event_ids)}")
    print(f"Event type distribution: {json.dumps(counts, indent=2)}")

    if errors:
        print(f"\n❌ {len(errors)} validation errors:")
        for err in errors[:20]:
            print(f"  • {err}")
        if len(errors) > 20:
            print(f"  ... and {len(errors) - 20} more")
        return False
    else:
        print(f"\n✅ All {len(event_ids)} events are valid")
        return True


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "output/events.jsonl"
    ok = validate(path)
    sys.exit(0 if ok else 1)
