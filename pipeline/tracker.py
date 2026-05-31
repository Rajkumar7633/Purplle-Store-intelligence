"""
Multi-object tracker with:
  - Entry/Exit detection via virtual line crossing
  - Re-entry detection via Re-ID (same visitor re-enters after EXIT)
  - Staff exclusion flag
  - Zone dwell tracking (emit ZONE_DWELL every 30s)
  - Group entry: each tracked bounding box = 1 person (handled by YOLO/ByteTrack)
  - Queue depth tracking for BILLING zone
"""
import hashlib
import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

import numpy as np

from pipeline.emit import make_event
from pipeline.zone_classifier import ZoneClassifier
from pipeline.staff_detector import StaffDetector
from pipeline.reid import ReIDEngine

logger = logging.getLogger(__name__)

DWELL_EMIT_INTERVAL_SEC = 30.0


class TrackState:
    def __init__(self, track_id: int, visitor_id: str, first_seen: float):
        self.track_id = track_id
        self.visitor_id = visitor_id
        self.first_seen = first_seen
        self.last_seen = first_seen
        self.is_staff = False
        self.staff_confidence = 0.5
        self.has_entered = False
        self.has_exited = False
        self.current_zone: Optional[str] = None
        self.zone_enter_time: Optional[float] = None
        self.last_dwell_emit: float = 0.0
        self.zone_visit_counts: Dict[str, int] = {}
        self.session_seq = 0
        # Track y-position history for line crossing
        self.prev_cy: Optional[float] = None
        self.feature: Optional[np.ndarray] = None
        self.detection_count = 0
        self.confidence_history: List[float] = []

    def avg_confidence(self) -> float:
        if not self.confidence_history:
            return 0.5
        return sum(self.confidence_history[-10:]) / min(len(self.confidence_history), 10)

    def make_visitor_id(track_id: int, store_id: str) -> str:
        h = hashlib.md5(f"{store_id}_{track_id}_{time.time()}".encode()).hexdigest()[:6]
        return f"VIS_{h}"


class MultiObjectTracker:
    def __init__(
        self,
        store_id: str,
        camera_id: str,
        fps: float,
        zone_classifier: ZoneClassifier,
        staff_detector: StaffDetector,
        pos_data: Optional[List] = None,
        reentry_window_sec: float = 120.0,
    ):
        self.store_id = store_id
        self.camera_id = camera_id
        self.fps = fps
        self.zone_classifier = zone_classifier
        self.staff_detector = staff_detector
        self.pos_data = pos_data or []
        self.reentry_window_sec = reentry_window_sec

        self._active: Dict[int, TrackState] = {}   # track_id → TrackState
        self._exited: Dict[str, TrackState] = {}   # visitor_id → last TrackState (for re-entry)
        self._reid = ReIDEngine(similarity_threshold=0.72, max_age_seconds=reentry_window_sec)
        self._billing_occupants: Set[str] = set()  # visitor_ids currently in billing zone
        self._total_entries = 0
        self._total_exits = 0

        try:
            from ultralytics import YOLO
            self._byte_tracker = None  # ByteTrack is built into ultralytics
        except ImportError:
            pass

    def update(
        self,
        frame: np.ndarray,
        detections: List[dict],
        frame_time: datetime,
        frame_idx: int,
    ) -> List[dict]:
        """
        Process detections for one frame.
        Returns list of events to emit.
        """
        events = []
        ts_sec = frame_time.timestamp()

        # Use ByteTrack via ultralytics (detections already come with track IDs if used properly)
        # Here we receive pre-tracked detections with bbox + confidence + optional crop
        for det in detections:
            track_id = det.get("track_id")
            if track_id is None:
                continue

            bbox = det["bbox"]
            conf = det["confidence"]
            crop = det.get("crop")

            cx = (bbox[0] + bbox[2]) / 2
            cy = (bbox[1] + bbox[3]) / 2

            # ── Get or create track state ──────────────────────────────
            if track_id not in self._active:
                # Check re-entry via Re-ID
                feature = self._reid.extract_features(crop)
                existing_visitor = None
                if feature is not None:
                    match = self._reid.find_match(
                        feature,
                        ts_sec,
                        exclude_ids={s.visitor_id for s in self._active.values()},
                    )
                    if match:
                        existing_visitor, reid_score = match
                        logger.debug("Re-ID match: %s (score=%.2f)", existing_visitor, reid_score)

                if existing_visitor and existing_visitor in self._exited:
                    # This is a re-entry
                    old_state = self._exited.pop(existing_visitor)
                    state = TrackState(track_id, existing_visitor, ts_sec)
                    state.is_staff = old_state.is_staff
                    state.zone_visit_counts = old_state.zone_visit_counts.copy()
                    state.session_seq = old_state.session_seq
                    state.feature = feature
                    self._active[track_id] = state

                    events.append(make_event(
                        store_id=self.store_id,
                        camera_id=self.camera_id,
                        visitor_id=existing_visitor,
                        event_type="REENTRY",
                        timestamp=frame_time,
                        confidence=conf,
                        is_staff=state.is_staff,
                        session_seq=state.session_seq,
                    ))
                else:
                    vis_id = f"VIS_{hashlib.md5(f'{self.store_id}_{track_id}_{ts_sec}'.encode()).hexdigest()[:6]}"
                    state = TrackState(track_id, vis_id, ts_sec)
                    state.feature = feature
                    self._active[track_id] = state

            state = self._active[track_id]
            state.last_seen = ts_sec
            state.confidence_history.append(conf)
            state.detection_count += 1

            # ── Staff detection (update every 30 frames) ──────────────
            if state.detection_count % 30 == 1:
                is_staff, staff_conf = self.staff_detector.is_staff(crop)
                if staff_conf > state.staff_confidence:
                    state.is_staff = is_staff
                    state.staff_confidence = staff_conf

            # ── Entry / Exit detection (line crossing) ─────────────────
            if state.prev_cy is not None and "entry" in self.camera_id.lower():
                if (not state.has_entered
                        and self.zone_classifier.is_entry_direction(state.prev_cy, cy)):
                    state.has_entered = True
                    state.session_seq += 1
                    self._total_entries += 1
                    events.append(make_event(
                        store_id=self.store_id,
                        camera_id=self.camera_id,
                        visitor_id=state.visitor_id,
                        event_type="ENTRY",
                        timestamp=frame_time,
                        confidence=conf,
                        is_staff=state.is_staff,
                        session_seq=state.session_seq,
                    ))
                    # Register in Re-ID gallery
                    if state.feature is not None:
                        self._reid.register(state.visitor_id, state.feature, ts_sec)

                elif (state.has_entered and not state.has_exited
                        and self.zone_classifier.is_exit_direction(state.prev_cy, cy)):
                    state.has_exited = True
                    self._total_exits += 1
                    events.append(make_event(
                        store_id=self.store_id,
                        camera_id=self.camera_id,
                        visitor_id=state.visitor_id,
                        event_type="EXIT",
                        timestamp=frame_time,
                        confidence=conf,
                        is_staff=state.is_staff,
                        session_seq=state.session_seq,
                    ))

            state.prev_cy = cy

            # ── Zone classification (non-entry cameras) ────────────────
            if "entry" not in self.camera_id.lower():
                zone = self.zone_classifier.classify(cx, cy)

                if zone != state.current_zone:
                    # Zone exit
                    if state.current_zone is not None:
                        dwell_ms = int((ts_sec - (state.zone_enter_time or ts_sec)) * 1000)
                        events.append(make_event(
                            store_id=self.store_id,
                            camera_id=self.camera_id,
                            visitor_id=state.visitor_id,
                            event_type="ZONE_EXIT",
                            timestamp=frame_time,
                            zone_id=state.current_zone,
                            dwell_ms=dwell_ms,
                            confidence=conf,
                            is_staff=state.is_staff,
                            sku_zone=self.zone_classifier.get_sku_zone(state.current_zone),
                            session_seq=state.session_seq,
                        ))
                        if state.current_zone.lower() in ("billing", "billing_area"):
                            self._billing_occupants.discard(state.visitor_id)

                    # Zone enter
                    if zone is not None:
                        state.zone_visit_counts[zone] = state.zone_visit_counts.get(zone, 0) + 1
                        state.session_seq += 1

                        queue_depth = None
                        event_type = "ZONE_ENTER"

                        if zone.lower() in ("billing", "billing_area"):
                            self._billing_occupants.add(state.visitor_id)
                            queue_depth = len(self._billing_occupants)
                            if queue_depth > 1:
                                event_type = "BILLING_QUEUE_JOIN"

                        events.append(make_event(
                            store_id=self.store_id,
                            camera_id=self.camera_id,
                            visitor_id=state.visitor_id,
                            event_type=event_type,
                            timestamp=frame_time,
                            zone_id=zone,
                            confidence=conf,
                            is_staff=state.is_staff,
                            queue_depth=queue_depth,
                            sku_zone=self.zone_classifier.get_sku_zone(zone),
                            session_seq=state.session_seq,
                        ))

                    state.current_zone = zone
                    state.zone_enter_time = ts_sec
                    state.last_dwell_emit = ts_sec

                # Zone dwell (emit every DWELL_EMIT_INTERVAL_SEC of continuous dwell)
                elif (zone is not None
                        and ts_sec - state.last_dwell_emit >= DWELL_EMIT_INTERVAL_SEC):
                    dwell_ms = int((ts_sec - state.last_dwell_emit) * 1000)
                    state.session_seq += 1
                    events.append(make_event(
                        store_id=self.store_id,
                        camera_id=self.camera_id,
                        visitor_id=state.visitor_id,
                        event_type="ZONE_DWELL",
                        timestamp=frame_time,
                        zone_id=zone,
                        dwell_ms=dwell_ms,
                        confidence=conf,
                        is_staff=state.is_staff,
                        sku_zone=self.zone_classifier.get_sku_zone(zone),
                        session_seq=state.session_seq,
                    ))
                    state.last_dwell_emit = ts_sec

        # ── Check billing queue abandonment ───────────────────────────
        abandonment_events = self._check_billing_abandonment(frame_time)
        events.extend(abandonment_events)

        # ── Prune stale tracks ────────────────────────────────────────
        stale_ids = [
            tid for tid, s in self._active.items()
            if ts_sec - s.last_seen > 5.0  # 5 second gap = lost track
        ]
        for tid in stale_ids:
            state = self._active.pop(tid)
            self._exited[state.visitor_id] = state
            # Re-register with latest feature for re-entry detection
            if state.feature is not None:
                self._reid.register(state.visitor_id, state.feature, ts_sec)

        return events

    def _check_billing_abandonment(self, frame_time: datetime) -> List[dict]:
        """
        Detect billing queue abandonment: visitor was in billing but exited
        without a corresponding POS transaction (handled via zone exit + no POS match).
        This is a simplified check; full POS correlation is done post-processing.
        """
        return []  # Full correlation done in API layer via BILLING_QUEUE_ABANDON events

    def finalize(self, frame_time: datetime) -> List[dict]:
        """Close all open sessions at end of clip."""
        events = []
        ts_sec = frame_time.timestamp()

        for track_id, state in list(self._active.items()):
            if state.has_entered and not state.has_exited:
                events.append(make_event(
                    store_id=self.store_id,
                    camera_id=self.camera_id,
                    visitor_id=state.visitor_id,
                    event_type="EXIT",
                    timestamp=frame_time,
                    confidence=state.avg_confidence(),
                    is_staff=state.is_staff,
                    session_seq=state.session_seq,
                ))
            # Close open zone
            if state.current_zone is not None:
                dwell_ms = int((ts_sec - (state.zone_enter_time or ts_sec)) * 1000)
                events.append(make_event(
                    store_id=self.store_id,
                    camera_id=self.camera_id,
                    visitor_id=state.visitor_id,
                    event_type="ZONE_EXIT",
                    timestamp=frame_time,
                    zone_id=state.current_zone,
                    dwell_ms=dwell_ms,
                    confidence=state.avg_confidence(),
                    is_staff=state.is_staff,
                    session_seq=state.session_seq,
                ))

        self._active.clear()
        return events

    def get_stats(self) -> dict:
        return {
            "total_entries": self._total_entries,
            "total_exits": self._total_exits,
            "active_tracks": len(self._active),
        }


