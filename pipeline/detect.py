#!/usr/bin/env python3
"""
Main detection + tracking script for Store Intelligence Pipeline.
Processes CCTV clips using YOLOv8 + ByteTrack, emits structured events.

Usage:
    python detect.py --clip path/to/clip.mp4 --store-id STORE_BLR_002 \
                     --camera-id CAM_ENTRY_01 --layout data/store_layout.json \
                     --output output/events.jsonl
"""
import argparse
import csv
import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional

import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("detect")

try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    logger.error("opencv-python-headless is required: pip install opencv-python-headless")
    CV2_AVAILABLE = False

try:
    from ultralytics import YOLO
    YOLO_AVAILABLE = True
except ImportError:
    logger.warning("ultralytics not available — running in mock/demo mode")
    YOLO_AVAILABLE = False

# Add pipeline dir to path
sys.path.insert(0, str(Path(__file__).parent))
from emit import EventEmitter
from tracker import MultiObjectTracker
from zone_classifier import ZoneClassifier
from staff_detector import StaffDetector


def load_store_layout(path: str) -> dict:
    if not os.path.exists(path):
        logger.warning("store_layout.json not found at %s — using defaults", path)
        return _default_layout()
    with open(path) as f:
        return json.load(f)


def _default_layout() -> dict:
    """Minimal layout used when store_layout.json is absent."""
    return {
        "cameras": {
            "CAM_ENTRY_01": {"entry_line_y": 0.5},
            "CAM_FLOOR_01": {},
            "CAM_BILLING_01": {},
        },
        "zones": [
            {"zone_id": "SKINCARE", "sku_zone": "SKINCARE", "cameras_covering": ["CAM_FLOOR_01"],
             "bbox": [0.0, 0.0, 0.5, 0.5]},
            {"zone_id": "HAIRCARE", "sku_zone": "HAIRCARE", "cameras_covering": ["CAM_FLOOR_01"],
             "bbox": [0.5, 0.0, 1.0, 0.5]},
            {"zone_id": "BILLING", "sku_zone": "BILLING", "cameras_covering": ["CAM_BILLING_01"],
             "bbox": [0.1, 0.1, 0.9, 0.9]},
        ],
    }


def load_pos_data(path: str) -> List[dict]:
    if not path or not os.path.exists(path):
        return []
    records = []
    try:
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                records.append(row)
    except Exception as exc:
        logger.warning("Could not load POS data: %s", exc)
    return records


def process_clip(
    clip_path: str,
    store_id: str,
    camera_id: str,
    store_layout: dict,
    pos_data: List[dict],
    output_path: str,
    clip_start_time: str,
    model_path: str = "yolov8n.pt",
    confidence_threshold: float = 0.35,
    device: str = "cpu",
    api_url: Optional[str] = None,
) -> dict:
    if not CV2_AVAILABLE:
        raise RuntimeError("opencv not available")

    cap = cv2.VideoCapture(clip_path)
    if not cap.isOpened():
        raise ValueError(f"Cannot open video: {clip_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    logger.info("Video: %dx%d @ %.1ffps, %d frames", width, height, fps, total_frames)

    # Parse clip start time
    try:
        clip_start_dt = datetime.fromisoformat(clip_start_time.replace("Z", "+00:00"))
    except ValueError:
        clip_start_dt = datetime.now(timezone.utc)
        logger.warning("Invalid clip_start_time, using now")

    # Initialize components
    model = None
    if YOLO_AVAILABLE:
        try:
            model = YOLO(model_path)
            logger.info("Loaded YOLO model: %s", model_path)
        except Exception as exc:
            logger.warning("Failed to load YOLO model: %s", exc)

    zone_classifier = ZoneClassifier(
        store_layout=store_layout,
        camera_id=camera_id,
        frame_width=width,
        frame_height=height,
    )
    staff_detector = StaffDetector(camera_id=camera_id)
    tracker = MultiObjectTracker(
        store_id=store_id,
        camera_id=camera_id,
        fps=fps,
        zone_classifier=zone_classifier,
        staff_detector=staff_detector,
        pos_data=pos_data,
    )
    emitter = EventEmitter(output_path=output_path, api_url=api_url)

    frame_idx = 0
    # Process every Nth frame to balance accuracy vs speed (2 = every other frame)
    process_every = max(1, int(fps / 7.5))  # ~7.5 effective fps

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        if frame_idx % process_every != 0:
            frame_idx += 1
            continue

        frame_time = clip_start_dt + timedelta(seconds=frame_idx / fps)

        # ── Run YOLOv8 + ByteTrack ────────────────────────────────────────
        detections = []
        if model is not None:
            try:
                results = model.track(
                    frame,
                    classes=[0],  # person only
                    conf=confidence_threshold,
                    device=device,
                    tracker="bytetrack.yaml",
                    persist=True,
                    verbose=False,
                )
                for result in results:
                    boxes = result.boxes
                    if boxes is None:
                        continue
                    for box in boxes:
                        x1, y1, x2, y2 = box.xyxy[0].tolist()
                        conf = float(box.conf[0])
                        track_id = int(box.id[0]) if box.id is not None else frame_idx * 1000 + len(detections)

                        x1i, y1i, x2i, y2i = int(x1), int(y1), int(x2), int(y2)
                        crop = frame[max(0, y1i):y2i, max(0, x1i):x2i]
                        if crop.size == 0:
                            crop = None

                        detections.append({
                            "track_id": track_id,
                            "bbox": [x1, y1, x2, y2],
                            "confidence": conf,
                            "crop": crop,
                        })
            except Exception as exc:
                logger.debug("Detection error frame %d: %s", frame_idx, exc)

        # Update tracker → get events
        events = tracker.update(
            frame=frame,
            detections=detections,
            frame_time=frame_time,
            frame_idx=frame_idx,
        )
        for event in events:
            emitter.emit(event)

        frame_idx += 1

        if frame_idx % (int(fps) * 60) == 0:
            mins = frame_idx / fps / 60
            logger.info("%.1f min processed | entries=%d exits=%d",
                        mins, tracker._total_entries, tracker._total_exits)

    # Close open sessions
    final_time = clip_start_dt + timedelta(seconds=total_frames / fps)
    final_events = tracker.finalize(frame_time=final_time)
    for event in final_events:
        emitter.emit(event)

    cap.release()
    emitter.close()

    stats = tracker.get_stats()
    logger.info("Done: %s", stats)
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Store Intelligence Detection Pipeline")
    parser.add_argument("--clip", required=True, help="Path to CCTV clip")
    parser.add_argument("--store-id", required=True, help="Store identifier (e.g. STORE_BLR_002)")
    parser.add_argument("--camera-id", required=True, help="Camera ID (e.g. CAM_ENTRY_01)")
    parser.add_argument("--layout", default="data/store_layout.json", help="store_layout.json path")
    parser.add_argument("--pos", default=None, help="pos_transactions.csv path")
    parser.add_argument("--output", required=True, help="Output JSONL path for events")
    parser.add_argument("--start-time", default=None,
                        help="Clip start time ISO-8601 (e.g. 2026-03-03T09:00:00Z)")
    parser.add_argument("--model", default="yolov8n.pt", help="YOLO model path")
    parser.add_argument("--conf", type=float, default=0.35, help="Detection confidence threshold")
    parser.add_argument("--device", default="cpu", help="Device: cpu / cuda / mps")
    parser.add_argument("--api-url", default=os.getenv("API_INGEST_URL"),
                        help="API ingest URL (optional — also writes to --output)")
    args = parser.parse_args()

    # Ensure output dir exists
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)

    start_time = args.start_time or datetime.now(timezone.utc).isoformat()
    store_layout = load_store_layout(args.layout)
    pos_data = load_pos_data(args.pos) if args.pos else []

    stats = process_clip(
        clip_path=args.clip,
        store_id=args.store_id,
        camera_id=args.camera_id,
        store_layout=store_layout,
        pos_data=pos_data,
        output_path=args.output,
        clip_start_time=start_time,
        model_path=args.model,
        confidence_threshold=args.conf,
        device=args.device,
        api_url=args.api_url,
    )
    print(json.dumps({"status": "complete", **stats}))


if __name__ == "__main__":
    main()
