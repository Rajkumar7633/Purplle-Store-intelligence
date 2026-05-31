"""
Staff detector: classifies a person crop as staff or customer.
Uses color histogram analysis to detect store uniform colors.
Falls back to zone-presence heuristics (staff detected in all zones uniformly).
"""
import logging
from typing import Optional, Tuple, List

import numpy as np

logger = logging.getLogger(__name__)

try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False


# Default staff uniform HSV color ranges (configurable via store_layout.json)
# Apex Retail default: navy blue uniform
DEFAULT_STAFF_COLORS = [
    {"name": "navy_blue", "lower": (100, 50, 20), "upper": (130, 255, 150)},
    {"name": "dark_blue", "lower": (105, 80, 30), "upper": (125, 255, 120)},
]


class StaffDetector:
    def __init__(
        self,
        camera_id: str,
        staff_colors: Optional[List[dict]] = None,
        color_threshold: float = 0.25,
    ):
        self.camera_id = camera_id
        self.staff_colors = staff_colors or DEFAULT_STAFF_COLORS
        self.color_threshold = color_threshold  # fraction of pixels that must match uniform

    def is_staff(self, crop: Optional[np.ndarray]) -> Tuple[bool, float]:
        """
        Returns (is_staff, confidence).
        Uses HSV color histogram matching against known uniform colors.
        """
        if crop is None or not CV2_AVAILABLE:
            return False, 0.5

        if crop.size == 0 or crop.shape[0] < 20 or crop.shape[1] < 10:
            return False, 0.5

        try:
            hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        except Exception:
            return False, 0.5

        total_pixels = hsv.shape[0] * hsv.shape[1]
        max_uniform_ratio = 0.0

        for color_range in self.staff_colors:
            lower = np.array(color_range["lower"], dtype=np.uint8)
            upper = np.array(color_range["upper"], dtype=np.uint8)
            mask = cv2.inRange(hsv, lower, upper)
            uniform_pixels = np.count_nonzero(mask)
            ratio = uniform_pixels / total_pixels
            max_uniform_ratio = max(max_uniform_ratio, ratio)

        is_staff_flag = max_uniform_ratio >= self.color_threshold
        # Confidence is stronger when more uniform pixels match
        confidence = min(0.95, 0.5 + max_uniform_ratio)

        return is_staff_flag, confidence

    def classify_from_history(self, zone_visit_counts: dict) -> bool:
        """
        Heuristic: staff visit ALL zones roughly uniformly.
        A customer tends to visit 1-3 zones.
        """
        if not zone_visit_counts:
            return False
        num_zones_visited = len(zone_visit_counts)
        total_visits = sum(zone_visit_counts.values())
        return num_zones_visited >= 5 and total_visits >= 10
