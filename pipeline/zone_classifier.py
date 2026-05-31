"""
Zone classifier: maps bounding-box centroid to named zone using polygon intersection.
Zone polygons are defined in store_layout.json as normalized [0,1] coordinates.
"""
import logging
from typing import Optional, Dict, List, Tuple

logger = logging.getLogger(__name__)

try:
    from shapely.geometry import Point, Polygon
    SHAPELY_AVAILABLE = True
except ImportError:
    SHAPELY_AVAILABLE = False
    logger.warning("shapely not available — using bounding-box zone classification")


class ZoneClassifier:
    def __init__(
        self,
        store_layout: dict,
        camera_id: str,
        frame_width: int,
        frame_height: int,
    ):
        self.camera_id = camera_id
        self.frame_width = frame_width
        self.frame_height = frame_height
        self.zones: List[Dict] = []
        self._entry_line_y: Optional[float] = None  # normalized y for entry/exit threshold

        self._parse_layout(store_layout)

    def _parse_layout(self, layout: dict) -> None:
        cameras = layout.get("cameras", {})
        cam_config = cameras.get(self.camera_id, {})

        # Entry camera: define entry/exit threshold line at ~middle of frame
        if "entry" in self.camera_id.lower():
            self._entry_line_y = cam_config.get("entry_line_y", 0.5)

        zones_cfg = layout.get("zones", [])
        for zone in zones_cfg:
            cameras_covering = zone.get("cameras_covering", [])
            if self.camera_id in cameras_covering or not cameras_covering:
                polygon_pts = zone.get("polygon", None)
                if polygon_pts and SHAPELY_AVAILABLE:
                    # Convert normalized coords to pixel coords
                    pixel_pts = [
                        (x * self.frame_width, y * self.frame_height)
                        for x, y in polygon_pts
                    ]
                    self.zones.append({
                        "zone_id": zone["zone_id"],
                        "sku_zone": zone.get("sku_zone", zone["zone_id"]),
                        "polygon": Polygon(pixel_pts),
                        "bbox": zone.get("bbox", None),
                    })
                elif "bbox" in zone or "bbox_pct" in zone:
                    # Fallback: axis-aligned bounding box [x1_norm, y1_norm, x2_norm, y2_norm]
                    # Accept both "bbox" and "bbox_pct" keys (store_layout.json uses bbox_pct)
                    bb = zone.get("bbox") or zone.get("bbox_pct")
                    self.zones.append({
                        "zone_id": zone["zone_id"],
                        "sku_zone": zone.get("sku_zone", zone["zone_id"]),
                        "polygon": None,
                        "bbox": (
                            bb[0] * self.frame_width,
                            bb[1] * self.frame_height,
                            bb[2] * self.frame_width,
                            bb[3] * self.frame_height,
                        ),
                    })

        logger.info("ZoneClassifier: %d zones loaded for camera %s", len(self.zones), self.camera_id)

    def classify(self, cx: float, cy: float) -> Optional[str]:
        """Return zone_id for centroid (cx, cy) in pixel space, or None."""
        for zone in self.zones:
            if zone["polygon"] is not None and SHAPELY_AVAILABLE:
                if zone["polygon"].contains(Point(cx, cy)):
                    return zone["zone_id"]
            elif zone["bbox"] is not None:
                x1, y1, x2, y2 = zone["bbox"]
                if x1 <= cx <= x2 and y1 <= cy <= y2:
                    return zone["zone_id"]
        return None

    def get_sku_zone(self, zone_id: str) -> Optional[str]:
        for zone in self.zones:
            if zone["zone_id"] == zone_id:
                return zone.get("sku_zone")
        return None

    def is_entry_direction(self, prev_cy: float, curr_cy: float) -> bool:
        """True if movement crosses entry threshold inward (top→bottom by default)."""
        if self._entry_line_y is None:
            return False
        line_y = self._entry_line_y * self.frame_height
        return prev_cy < line_y <= curr_cy

    def is_exit_direction(self, prev_cy: float, curr_cy: float) -> bool:
        """True if movement crosses entry threshold outward (bottom→top by default)."""
        if self._entry_line_y is None:
            return False
        line_y = self._entry_line_y * self.frame_height
        return prev_cy >= line_y > curr_cy

    def get_entry_line_y_px(self) -> Optional[float]:
        if self._entry_line_y is None:
            return None
        return self._entry_line_y * self.frame_height
