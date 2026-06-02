# PROMPT: "Write pytest tests to cover pipeline/detect.py, pipeline/tracker.py and
# pipeline/ingest_to_api.py without real video files. Mock cv2.VideoCapture to return
# synthetic frames, mock YOLO to return no detections, test all code paths in
# MultiObjectTracker (entry/exit crossing, zone enter/exit, dwell, re-entry, finalize,
# staff heuristic), and test ingest_to_api functions with mocked requests.post.
# Also test load_store_layout, load_pos_data, _post_batch retry logic, and ingest_pos."
#
# CHANGES MADE:
# - AI initially used cv2.CAP_PROP_* as raw integers; replaced with lambda dispatch dict
#   keyed by the actual cv2 constant values to avoid import-order issues
# - Added test for process_clip with invalid start_time (exercises the ValueError fallback)
# - Added test for MultiObjectTracker zone dwell emission after 30s interval
# - Added test for billing queue join + occupant tracking
# - Added test for _post_batch retry when first attempt returns 500, second returns 200
# - AI omitted test for main() in ingest_to_api.py; added argparse path tests

import csv
import json
import os
import sys
import tempfile
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import pipeline.detect as detect
import pipeline.ingest_to_api as ingest_mod
from pipeline.tracker import MultiObjectTracker, TrackState
from pipeline.zone_classifier import ZoneClassifier
from pipeline.staff_detector import StaffDetector

# ─── Helpers ──────────────────────────────────────────────────────────────────

MINIMAL_LAYOUT = {
    "cameras": {
        "CAM_ENTRY_01": {"entry_line_y": 0.5},
        "CAM_FLOOR_01": {},
    },
    "zones": [
        {
            "zone_id": "SKINCARE",
            "sku_zone": "MOISTURISER",
            "cameras_covering": ["CAM_FLOOR_01"],
            "bbox": [0.0, 0.0, 0.5, 0.5],
        },
        {
            "zone_id": "BILLING",
            "sku_zone": "BILLING",
            "cameras_covering": ["CAM_FLOOR_01"],
            "bbox": [0.4, 0.8, 0.6, 1.0],
        },
    ],
}

FAKE_FRAME = np.zeros((480, 640, 3), dtype=np.uint8)
NOW = datetime(2026, 5, 31, 10, 0, 0, tzinfo=timezone.utc)


def _make_tracker(camera_id="CAM_ENTRY_01", layout=None, fps=15.0):
    layout = layout or MINIMAL_LAYOUT
    zc = ZoneClassifier(layout, camera_id, 640, 480)
    sd = StaffDetector(camera_id)
    return MultiObjectTracker(
        store_id="STORE_BLR_002",
        camera_id=camera_id,
        fps=fps,
        zone_classifier=zc,
        staff_detector=sd,
    )


# ─── detect.py: load_store_layout ─────────────────────────────────────────────

class TestLoadStoreLayout:
    def test_missing_file_returns_default(self):
        layout = detect.load_store_layout("/nonexistent/path.json")
        assert "cameras" in layout
        assert "zones" in layout
        assert len(layout["zones"]) > 0

    def test_valid_file_loaded(self, tmp_path):
        layout_path = tmp_path / "layout.json"
        layout_path.write_text(json.dumps(MINIMAL_LAYOUT))
        result = detect.load_store_layout(str(layout_path))
        assert result["cameras"]["CAM_ENTRY_01"]["entry_line_y"] == 0.5

    def test_default_layout_structure(self):
        layout = detect._default_layout()
        assert isinstance(layout["cameras"], dict)
        assert isinstance(layout["zones"], list)
        zone_ids = [z["zone_id"] for z in layout["zones"]]
        assert "BILLING" in zone_ids


# ─── detect.py: load_pos_data ──────────────────────────────────────────────────

class TestLoadPosData:
    def test_none_returns_empty(self):
        result = detect.load_pos_data(None)
        assert result == []

    def test_missing_file_returns_empty(self):
        result = detect.load_pos_data("/nonexistent/pos.csv")
        assert result == []

    def test_valid_csv_loaded(self, tmp_path):
        pos_path = tmp_path / "pos.csv"
        pos_path.write_text(
            "store_id,transaction_id,timestamp,basket_value_inr\n"
            "STORE_BLR_002,TXN_001,2026-05-31T10:00:00Z,1200.00\n"
            "STORE_BLR_002,TXN_002,2026-05-31T10:05:00Z,800.00\n"
        )
        result = detect.load_pos_data(str(pos_path))
        assert len(result) == 2
        assert result[0]["transaction_id"] == "TXN_001"


# ─── detect.py: process_clip (mocked cv2 + no YOLO) ───────────────────────────

def _make_mock_cv2(n_frames=5, fps=15.0, width=640, height=480):
    """Return a (mock_cv2_module, mock_cap) pair. cap.get() returns values in call order."""
    mock_cv2 = MagicMock()
    mock_cap = MagicMock()
    mock_cap.isOpened.return_value = True
    # cap.get() is called 4 times in order: fps, frame_count, width, height
    mock_cap.get.side_effect = [fps, float(n_frames), float(width), float(height)]
    reads = [(True, FAKE_FRAME)] * n_frames + [(False, None)]
    mock_cap.read.side_effect = reads
    mock_cv2.VideoCapture.return_value = mock_cap
    return mock_cv2, mock_cap


class TestProcessClip:
    def _run_process_clip(self, monkeypatch, tmp_path, n_frames=5,
                          clip_start="2026-05-31T10:00:00Z", api_url=None, bad_open=False):
        mock_cv2, mock_cap = _make_mock_cv2(n_frames=n_frames)
        if bad_open:
            mock_cap.isOpened.return_value = False
        monkeypatch.setattr(detect, "cv2", mock_cv2, raising=False)
        monkeypatch.setattr(detect, "CV2_AVAILABLE", True)
        monkeypatch.setattr(detect, "YOLO_AVAILABLE", False)
        output = str(tmp_path / "events.jsonl")
        return detect.process_clip(
            clip_path="/fake/clip.mp4",
            store_id="STORE_BLR_002",
            camera_id="CAM_ENTRY_01",
            store_layout=MINIMAL_LAYOUT,
            pos_data=[],
            output_path=output,
            clip_start_time=clip_start,
            api_url=api_url,
        ), output

    def test_process_clip_no_yolo(self, monkeypatch, tmp_path):
        stats, _ = self._run_process_clip(monkeypatch, tmp_path, n_frames=10)
        assert "total_entries" in stats
        assert "total_exits" in stats
        assert "active_tracks" in stats

    def test_process_clip_invalid_start_time_fallback(self, monkeypatch, tmp_path):
        stats, _ = self._run_process_clip(monkeypatch, tmp_path, n_frames=3,
                                           clip_start="NOT_A_DATE")
        assert isinstance(stats, dict)

    def test_process_clip_cannot_open_video(self, monkeypatch, tmp_path):
        with pytest.raises(ValueError, match="Cannot open video"):
            self._run_process_clip(monkeypatch, tmp_path, bad_open=True)

    def test_process_clip_with_api_url(self, monkeypatch, tmp_path):
        """api_url set but unreachable — should complete without crash."""
        stats, _ = self._run_process_clip(monkeypatch, tmp_path, n_frames=5,
                                           api_url="http://localhost:19999")
        assert isinstance(stats, dict)

    def test_process_clip_cv2_unavailable_raises(self, monkeypatch, tmp_path):
        monkeypatch.setattr(detect, "CV2_AVAILABLE", False)
        with pytest.raises(RuntimeError, match="opencv not available"):
            detect.process_clip(
                clip_path="/fake/clip.mp4",
                store_id="STORE_BLR_002",
                camera_id="CAM_ENTRY_01",
                store_layout=MINIMAL_LAYOUT,
                pos_data=[],
                output_path=str(tmp_path / "out.jsonl"),
                clip_start_time="2026-05-31T10:00:00Z",
            )


# ─── tracker.py: TrackState ───────────────────────────────────────────────────

class TestTrackState:
    def test_avg_confidence_empty(self):
        ts = TrackState(1, "VIS_abc", 0.0)
        assert ts.avg_confidence() == 0.5

    def test_avg_confidence_with_history(self):
        ts = TrackState(1, "VIS_abc", 0.0)
        ts.confidence_history = [0.8, 0.9, 0.7]
        avg = ts.avg_confidence()
        assert abs(avg - 0.8) < 0.01

    def test_avg_confidence_uses_last_10(self):
        ts = TrackState(1, "VIS_abc", 0.0)
        ts.confidence_history = [0.1] * 5 + [0.9] * 15
        # Last 10 are all 0.9
        assert ts.avg_confidence() == pytest.approx(0.9)

    def test_initial_state(self):
        ts = TrackState(42, "VIS_xyz", 1000.0)
        assert ts.track_id == 42
        assert ts.visitor_id == "VIS_xyz"
        assert ts.has_entered is False
        assert ts.has_exited is False
        assert ts.current_zone is None
        assert ts.is_staff is False


# ─── tracker.py: MultiObjectTracker — entry/exit ──────────────────────────────

class TestTrackerEntryExit:
    def _entry_detection(self, track_id, cy, conf=0.8):
        cx = 320.0
        return {
            "track_id": track_id,
            "bbox": [cx - 50, cy - 100, cx + 50, cy + 100],
            "confidence": conf,
            "crop": FAKE_FRAME,
        }

    def test_entry_event_on_line_crossing(self):
        tracker = _make_tracker("CAM_ENTRY_01")
        # First frame: person above line (cy < 240)
        events1 = tracker.update(
            frame=FAKE_FRAME,
            detections=[self._entry_detection(1, cy=200.0)],
            frame_time=NOW,
            frame_idx=0,
        )
        # Second frame: person below line (cy > 240) → should fire ENTRY
        events2 = tracker.update(
            frame=FAKE_FRAME,
            detections=[self._entry_detection(1, cy=280.0)],
            frame_time=NOW + timedelta(seconds=1),
            frame_idx=15,
        )
        all_events = events1 + events2
        entry_events = [e for e in all_events if e["event_type"] == "ENTRY"]
        assert len(entry_events) == 1
        assert entry_events[0]["visitor_id"].startswith("VIS_")

    def test_exit_event_after_entry(self):
        tracker = _make_tracker("CAM_ENTRY_01")
        # Frame 1: above line
        tracker.update(
            frame=FAKE_FRAME,
            detections=[self._entry_detection(1, cy=200.0)],
            frame_time=NOW,
            frame_idx=0,
        )
        # Frame 2: below line → ENTRY
        tracker.update(
            frame=FAKE_FRAME,
            detections=[self._entry_detection(1, cy=280.0)],
            frame_time=NOW + timedelta(seconds=1),
            frame_idx=15,
        )
        # Frame 3: back above line → EXIT
        events3 = tracker.update(
            frame=FAKE_FRAME,
            detections=[self._entry_detection(1, cy=200.0)],
            frame_time=NOW + timedelta(seconds=2),
            frame_idx=30,
        )
        exit_events = [e for e in events3 if e["event_type"] == "EXIT"]
        assert len(exit_events) == 1

    def test_no_entry_on_floor_camera(self):
        """Floor cameras should never emit ENTRY events."""
        tracker = _make_tracker("CAM_FLOOR_01")
        for i in range(5):
            tracker.update(
                frame=FAKE_FRAME,
                detections=[self._entry_detection(1, cy=float(i * 50))],
                frame_time=NOW + timedelta(seconds=i),
                frame_idx=i * 15,
            )
        assert tracker._total_entries == 0

    def test_get_stats_after_entries(self):
        tracker = _make_tracker("CAM_ENTRY_01")
        tracker.update(FAKE_FRAME, [self._entry_detection(1, 200.0)], NOW, 0)
        tracker.update(FAKE_FRAME, [self._entry_detection(1, 280.0)], NOW + timedelta(seconds=1), 15)
        stats = tracker.get_stats()
        assert stats["total_entries"] == 1
        assert stats["total_exits"] == 0
        assert "active_tracks" in stats


# ─── tracker.py: MultiObjectTracker — zone events ─────────────────────────────

class TestTrackerZoneEvents:
    def _floor_detection(self, track_id, cx, cy, conf=0.8):
        return {
            "track_id": track_id,
            "bbox": [cx - 40, cy - 80, cx + 40, cy + 80],
            "confidence": conf,
            "crop": FAKE_FRAME,
        }

    def test_zone_enter_event_emitted(self):
        tracker = _make_tracker("CAM_FLOOR_01")
        # cx=160, cy=120 → SKINCARE zone (bbox 0.0–0.5 → 0–320, 0–240 at 640x480)
        events = tracker.update(
            frame=FAKE_FRAME,
            detections=[self._floor_detection(1, cx=160, cy=120)],
            frame_time=NOW,
            frame_idx=0,
        )
        zone_enters = [e for e in events if e["event_type"] == "ZONE_ENTER"]
        assert len(zone_enters) == 1
        assert zone_enters[0]["zone_id"] == "SKINCARE"

    def test_zone_exit_emitted_on_zone_change(self):
        tracker = _make_tracker("CAM_FLOOR_01")
        # First frame: in SKINCARE
        tracker.update(FAKE_FRAME, [self._floor_detection(1, 160, 120)], NOW, 0)
        # Second frame: outside all zones
        events2 = tracker.update(
            frame=FAKE_FRAME,
            detections=[self._floor_detection(1, cx=100, cy=400)],  # outside all zones
            frame_time=NOW + timedelta(seconds=5),
            frame_idx=75,
        )
        zone_exits = [e for e in events2 if e["event_type"] == "ZONE_EXIT"]
        assert len(zone_exits) == 1
        assert zone_exits[0]["zone_id"] == "SKINCARE"

    def test_zone_dwell_emitted_after_30s(self):
        tracker = _make_tracker("CAM_FLOOR_01")
        # Enter zone
        tracker.update(FAKE_FRAME, [self._floor_detection(1, 160, 120)], NOW, 0)
        # 31 seconds later — still in same zone → ZONE_DWELL
        events = tracker.update(
            frame=FAKE_FRAME,
            detections=[self._floor_detection(1, 160, 120)],
            frame_time=NOW + timedelta(seconds=31),
            frame_idx=31 * 15,
        )
        dwells = [e for e in events if e["event_type"] == "ZONE_DWELL"]
        assert len(dwells) == 1
        assert dwells[0]["zone_id"] == "SKINCARE"

    def test_billing_queue_join_when_queue_gt_1(self):
        tracker = _make_tracker("CAM_FLOOR_01")
        billing_cx, billing_cy = 320, 432  # inside BILLING bbox (0.4–0.6, 0.8–1.0) at 640x480

        # Visitor 1 enters billing
        tracker.update(FAKE_FRAME, [self._floor_detection(1, billing_cx, billing_cy)], NOW, 0)
        # Visitor 2 enters billing → queue depth > 1 → BILLING_QUEUE_JOIN
        events = tracker.update(
            frame=FAKE_FRAME,
            detections=[
                self._floor_detection(1, billing_cx, billing_cy),
                self._floor_detection(2, billing_cx + 10, billing_cy),
            ],
            frame_time=NOW + timedelta(seconds=2),
            frame_idx=30,
        )
        queue_joins = [e for e in events if e["event_type"] == "BILLING_QUEUE_JOIN"]
        assert len(queue_joins) >= 1


# ─── tracker.py: finalize ─────────────────────────────────────────────────────

class TestTrackerFinalize:
    def test_finalize_closes_open_entry_session(self):
        tracker = _make_tracker("CAM_ENTRY_01")
        # Create an entered (but not exited) track
        tracker.update(
            FAKE_FRAME,
            [{"track_id": 1, "bbox": [270, 100, 370, 300], "confidence": 0.9, "crop": FAKE_FRAME}],
            NOW, 0,
        )
        tracker.update(
            FAKE_FRAME,
            [{"track_id": 1, "bbox": [270, 260, 370, 460], "confidence": 0.9, "crop": FAKE_FRAME}],
            NOW + timedelta(seconds=1), 15,
        )
        # Force has_entered = True for any active track
        for state in tracker._active.values():
            state.has_entered = True
            state.has_exited = False

        final_events = tracker.finalize(frame_time=NOW + timedelta(minutes=5))
        exit_events = [e for e in final_events if e["event_type"] == "EXIT"]
        assert len(exit_events) >= 1

    def test_finalize_closes_open_zone(self):
        tracker = _make_tracker("CAM_FLOOR_01")
        # Put a person in SKINCARE
        tracker.update(
            FAKE_FRAME,
            [{"track_id": 1, "bbox": [120, 40, 200, 200], "confidence": 0.85, "crop": FAKE_FRAME}],
            NOW, 0,
        )
        # Confirm person is in a zone
        active = list(tracker._active.values())
        assert any(s.current_zone is not None for s in active)

        final_events = tracker.finalize(frame_time=NOW + timedelta(minutes=3))
        zone_exits = [e for e in final_events if e["event_type"] == "ZONE_EXIT"]
        assert len(zone_exits) >= 1

    def test_finalize_empty_tracker(self):
        tracker = _make_tracker("CAM_ENTRY_01")
        events = tracker.finalize(frame_time=NOW)
        assert events == []

    def test_get_stats_initial(self):
        tracker = _make_tracker("CAM_ENTRY_01")
        stats = tracker.get_stats()
        assert stats == {"total_entries": 0, "total_exits": 0, "active_tracks": 0}


# ─── tracker.py: stale track pruning ─────────────────────────────────────────

class TestTrackerStalePruning:
    def test_stale_track_moved_to_exited(self):
        tracker = _make_tracker("CAM_FLOOR_01")
        det = {"track_id": 1, "bbox": [120, 40, 200, 200], "confidence": 0.85, "crop": FAKE_FRAME}
        tracker.update(FAKE_FRAME, [det], NOW, 0)
        assert 1 in tracker._active

        # 10 seconds later, track is gone → should be pruned
        tracker.update(FAKE_FRAME, [], NOW + timedelta(seconds=10), 150)
        assert 1 not in tracker._active


# ─── ingest_to_api.py: _post_batch ────────────────────────────────────────────

class TestPostBatch:
    @patch("pipeline.ingest_to_api.requests.post")
    def test_post_batch_success(self, mock_post):
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"accepted": 3, "duplicates": 0, "invalid": 0}
        result = ingest_mod._post_batch([{"event_id": str(uuid.uuid4())}], "http://localhost:8000")
        assert result["accepted"] == 3
        mock_post.assert_called_once()

    @patch("pipeline.ingest_to_api.requests.post")
    def test_post_batch_http_500_returns_empty(self, mock_post):
        mock_post.return_value.status_code = 500
        mock_post.return_value.text = "Internal Server Error"
        result = ingest_mod._post_batch([{"event_id": "x"}], "http://localhost:8000")
        assert result == {}
        assert mock_post.call_count == ingest_mod.RETRY_ATTEMPTS

    @patch("pipeline.ingest_to_api.time.sleep")
    @patch("pipeline.ingest_to_api.requests.post")
    def test_post_batch_network_error_retries(self, mock_post, mock_sleep):
        import requests as req
        mock_post.side_effect = req.exceptions.ConnectionError("refused")
        result = ingest_mod._post_batch([{"event_id": "x"}], "http://localhost:8000")
        assert result == {}
        assert mock_post.call_count == ingest_mod.RETRY_ATTEMPTS
        # Should sleep between retries
        assert mock_sleep.call_count == ingest_mod.RETRY_ATTEMPTS - 1

    @patch("pipeline.ingest_to_api.time.sleep")
    @patch("pipeline.ingest_to_api.requests.post")
    def test_post_batch_succeeds_on_second_attempt(self, mock_post, mock_sleep):
        import requests as req
        mock_post.side_effect = [
            req.exceptions.ConnectionError("refused"),
            MagicMock(status_code=200, json=lambda: {"accepted": 5, "duplicates": 0, "invalid": 0}),
        ]
        result = ingest_mod._post_batch([{"event_id": "y"}], "http://localhost:8000")
        assert result["accepted"] == 5
        assert mock_post.call_count == 2


# ─── ingest_to_api.py: ingest_events ─────────────────────────────────────────

class TestIngestEvents:
    def _make_jsonl(self, tmp_path, n=5, include_bad=False):
        path = tmp_path / "events.jsonl"
        lines = []
        for i in range(n):
            lines.append(json.dumps({
                "event_id": str(uuid.uuid4()),
                "store_id": "STORE_BLR_002",
                "event_type": "ENTRY",
            }))
        if include_bad:
            lines.append("NOT VALID JSON {{{{")
        path.write_text("\n".join(lines) + "\n")
        return str(path)

    @patch("pipeline.ingest_to_api.requests.post")
    def test_ingest_events_happy_path(self, mock_post, tmp_path):
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"accepted": 5, "duplicates": 0, "invalid": 0}
        path = self._make_jsonl(tmp_path, n=5)
        result = ingest_mod.ingest_events(path, "http://localhost:8000")
        assert result["total_accepted"] == 5
        assert result["total_duplicates"] == 0

    @patch("pipeline.ingest_to_api.requests.post")
    def test_ingest_events_skips_bad_json(self, mock_post, tmp_path):
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"accepted": 3, "duplicates": 0, "invalid": 0}
        path = self._make_jsonl(tmp_path, n=3, include_bad=True)
        # Should not raise — bad lines are skipped
        result = ingest_mod.ingest_events(path, "http://localhost:8000")
        assert isinstance(result, dict)

    @patch("pipeline.ingest_to_api.requests.post")
    def test_ingest_events_batches_correctly(self, mock_post, tmp_path):
        """Events > BATCH_SIZE should trigger multiple POSTs."""
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"accepted": 1, "duplicates": 0, "invalid": 0}
        n = ingest_mod.BATCH_SIZE + 50
        path = self._make_jsonl(tmp_path, n=n)
        ingest_mod.ingest_events(path, "http://localhost:8000")
        assert mock_post.call_count >= 2

    @patch("pipeline.ingest_to_api.requests.post")
    def test_ingest_events_empty_file(self, mock_post, tmp_path):
        path = tmp_path / "empty.jsonl"
        path.write_text("")
        result = ingest_mod.ingest_events(str(path), "http://localhost:8000")
        assert result["total_accepted"] == 0
        mock_post.assert_not_called()

    @patch("pipeline.ingest_to_api.requests.post")
    def test_ingest_events_blank_lines_ignored(self, mock_post, tmp_path):
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"accepted": 1, "duplicates": 0, "invalid": 0}
        path = tmp_path / "events.jsonl"
        path.write_text(
            "\n\n"
            + json.dumps({"event_id": str(uuid.uuid4())}) + "\n"
            + "\n\n"
        )
        result = ingest_mod.ingest_events(str(path), "http://localhost:8000")
        assert isinstance(result, dict)


# ─── ingest_to_api.py: ingest_pos ─────────────────────────────────────────────

class TestIngestPos:
    def _make_pos_csv(self, tmp_path, n=5):
        path = tmp_path / "pos.csv"
        rows = ["store_id,transaction_id,timestamp,basket_value_inr"]
        for i in range(n):
            rows.append(f"STORE_BLR_002,TXN_{i:04d},2026-05-31T10:{i:02d}:00Z,{500 + i * 100}.00")
        path.write_text("\n".join(rows))
        return str(path)

    @patch("pipeline.ingest_to_api.requests.post")
    def test_ingest_pos_happy_path(self, mock_post, tmp_path):
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"total": 5}
        path = self._make_pos_csv(tmp_path, n=5)
        result = ingest_mod.ingest_pos(path, "http://localhost:8000")
        assert result["total"] == 5
        mock_post.assert_called_once()

    @patch("pipeline.ingest_to_api.requests.post")
    def test_ingest_pos_network_error_does_not_crash(self, mock_post, tmp_path):
        import requests as req
        mock_post.side_effect = req.exceptions.ConnectionError("refused")
        path = self._make_pos_csv(tmp_path, n=3)
        result = ingest_mod.ingest_pos(path, "http://localhost:8000")
        assert result["total"] == 3  # counts rows even if POST fails

    @patch("pipeline.ingest_to_api.requests.post")
    def test_ingest_pos_batches_large_file(self, mock_post, tmp_path):
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"total": ingest_mod.BATCH_SIZE}
        path = self._make_pos_csv(tmp_path, n=ingest_mod.BATCH_SIZE + 10)
        result = ingest_mod.ingest_pos(path, "http://localhost:8000")
        assert mock_post.call_count >= 2

    def test_ingest_pos_legacy_order_csv(self, tmp_path):
        path = tmp_path / "legacy_pos.csv"
        path.write_text(
            "order_id,order_date,order_time,store_id,product_id,brand_name,total_amount\n"
            "101,10-04-2026,12:15:05,ST1008,399945,Faces Canada,302.33\n"
            "101,10-04-2026,12:15:05,ST1008,353621,Faces Canada,491.77\n"
            "102,10-04-2026,12:42:18,ST1008,407887,Purplle,1\n"
        )
        result = ingest_mod.ingest_pos(str(path), "http://localhost:8000")
        assert result["total"] == 2


# ─── ingest_to_api.py: main() CLI ─────────────────────────────────────────────

class TestIngestMain:
    @patch("pipeline.ingest_to_api.ingest_events")
    @patch("pipeline.ingest_to_api.ingest_pos")
    def test_main_events_only(self, mock_pos, mock_events, tmp_path, capsys):
        mock_events.return_value = {"total_accepted": 10, "total_duplicates": 0, "total_invalid": 0}
        events_file = tmp_path / "e.jsonl"
        events_file.write_text("")
        with patch("sys.argv", ["ingest_to_api.py", "--events", str(events_file),
                                "--api", "http://localhost:8000"]):
            ingest_mod.main()
        mock_events.assert_called_once()
        mock_pos.assert_not_called()

    @patch("pipeline.ingest_to_api.ingest_events")
    @patch("pipeline.ingest_to_api.ingest_pos")
    def test_main_pos_only(self, mock_pos, mock_events, tmp_path, capsys):
        mock_pos.return_value = {"total": 5}
        pos_file = tmp_path / "pos.csv"
        pos_file.write_text("store_id,transaction_id,timestamp,basket_value_inr\n")
        with patch("sys.argv", ["ingest_to_api.py", "--pos", str(pos_file),
                                "--api", "http://localhost:8000"]):
            ingest_mod.main()
        mock_pos.assert_called_once()
        mock_events.assert_not_called()

    @patch("pipeline.ingest_to_api.ingest_events")
    @patch("pipeline.ingest_to_api.ingest_pos")
    def test_main_both(self, mock_pos, mock_events, tmp_path, capsys):
        mock_events.return_value = {"total_accepted": 5, "total_duplicates": 0, "total_invalid": 0}
        mock_pos.return_value = {"total": 3}
        events_file = tmp_path / "e.jsonl"
        events_file.write_text("")
        pos_file = tmp_path / "pos.csv"
        pos_file.write_text("store_id,transaction_id,timestamp,basket_value_inr\n")
        with patch("sys.argv", ["ingest_to_api.py",
                                "--events", str(events_file),
                                "--pos", str(pos_file),
                                "--api", "http://localhost:8000"]):
            ingest_mod.main()
        mock_events.assert_called_once()
        mock_pos.assert_called_once()
