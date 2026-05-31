"""
Event schema construction and emission.
Writes to JSONL file and optionally POSTs to API.
"""
import json
import uuid
import logging
import os
from datetime import datetime, timezone
from typing import Optional, Dict, Any

import requests

logger = logging.getLogger(__name__)

API_INGEST_URL = os.getenv("API_INGEST_URL", "http://localhost:8000/events/ingest")


def make_event(
    store_id: str,
    camera_id: str,
    visitor_id: str,
    event_type: str,
    timestamp: datetime,
    zone_id: Optional[str] = None,
    dwell_ms: int = 0,
    is_staff: bool = False,
    confidence: float = 1.0,
    queue_depth: Optional[int] = None,
    sku_zone: Optional[str] = None,
    session_seq: Optional[int] = None,
) -> Dict[str, Any]:
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": camera_id,
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": timestamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": round(confidence, 4),
        "metadata": {
            "queue_depth": queue_depth,
            "sku_zone": sku_zone,
            "session_seq": session_seq,
        },
    }


class EventEmitter:
    def __init__(self, output_path: str, api_url: Optional[str] = None, batch_size: int = 50):
        self.output_path = output_path
        self.api_url = api_url or API_INGEST_URL
        self.batch_size = batch_size
        self._buffer = []
        self._file = open(output_path, "a", encoding="utf-8")
        logger.info("EventEmitter → %s (api=%s)", output_path, self.api_url)

    def emit(self, event: Dict[str, Any]) -> None:
        self._file.write(json.dumps(event) + "\n")
        self._file.flush()
        self._buffer.append(event)
        if len(self._buffer) >= self.batch_size:
            self._flush_to_api()

    def _flush_to_api(self) -> None:
        if not self._buffer or not self.api_url:
            self._buffer.clear()
            return
        try:
            resp = requests.post(
                self.api_url,
                json={"events": self._buffer},
                timeout=10,
            )
            if resp.status_code not in (200, 201):
                logger.warning("API ingest returned %s: %s", resp.status_code, resp.text[:200])
        except Exception as exc:
            logger.warning("Failed to POST events to API: %s", exc)
        finally:
            self._buffer.clear()

    def close(self) -> None:
        self._flush_to_api()
        self._file.close()
        logger.info("EventEmitter closed")
