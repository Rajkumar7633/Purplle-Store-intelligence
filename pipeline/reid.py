"""
Person Re-ID using color histogram similarity.
Keeps a feature cache per track for cross-camera deduplication and re-entry detection.
OSNet / torchreid can be swapped in when GPU is available.
"""
import logging
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False


class AppearanceFeature:
    def __init__(self, histogram: np.ndarray, timestamp: float):
        self.histogram = histogram
        self.timestamp = timestamp


class ReIDEngine:
    def __init__(self, similarity_threshold: float = 0.75, max_age_seconds: float = 120.0):
        self.similarity_threshold = similarity_threshold
        self.max_age_seconds = max_age_seconds
        # visitor_id → list of AppearanceFeature
        self._gallery: Dict[str, List[AppearanceFeature]] = {}

    def extract_features(self, crop: Optional[np.ndarray]) -> Optional[np.ndarray]:
        """Extract color histogram (HSV) as appearance feature vector."""
        if crop is None or not CV2_AVAILABLE:
            return None
        if crop.size == 0 or crop.shape[0] < 10 or crop.shape[1] < 5:
            return None
        try:
            hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
            # 3-channel histogram: H(18 bins) + S(8 bins) + V(8 bins)
            hist_h = cv2.calcHist([hsv], [0], None, [18], [0, 180])
            hist_s = cv2.calcHist([hsv], [1], None, [8], [0, 256])
            hist_v = cv2.calcHist([hsv], [2], None, [8], [0, 256])
            hist = np.concatenate([hist_h, hist_s, hist_v]).flatten()
            cv2.normalize(hist, hist)
            return hist
        except Exception:
            return None

    def register(self, visitor_id: str, feature: np.ndarray, timestamp: float) -> None:
        if visitor_id not in self._gallery:
            self._gallery[visitor_id] = []
        self._gallery[visitor_id].append(AppearanceFeature(feature, timestamp))
        # Keep only last 5 appearances per visitor
        self._gallery[visitor_id] = self._gallery[visitor_id][-5:]

    def find_match(
        self,
        query_feature: np.ndarray,
        current_time: float,
        exclude_ids: Optional[set] = None,
    ) -> Optional[Tuple[str, float]]:
        """
        Returns (visitor_id, similarity_score) for the best match above threshold,
        or None if no match found.
        """
        if query_feature is None:
            return None

        best_id: Optional[str] = None
        best_score = 0.0

        for visitor_id, features in self._gallery.items():
            if exclude_ids and visitor_id in exclude_ids:
                continue
            for feat in features:
                # Skip very old features
                if current_time - feat.timestamp > self.max_age_seconds:
                    continue
                sim = self._bhattacharyya_similarity(query_feature, feat.histogram)
                if sim > best_score:
                    best_score = sim
                    best_id = visitor_id

        if best_id and best_score >= self.similarity_threshold:
            return best_id, best_score
        return None

    @staticmethod
    def _bhattacharyya_similarity(h1: np.ndarray, h2: np.ndarray) -> float:
        if h1 is None or h2 is None:
            return 0.0
        if not CV2_AVAILABLE:
            # Fallback: cosine similarity
            norm1 = np.linalg.norm(h1)
            norm2 = np.linalg.norm(h2)
            if norm1 == 0 or norm2 == 0:
                return 0.0
            return float(np.dot(h1, h2) / (norm1 * norm2))
        try:
            dist = cv2.compareHist(h1.astype(np.float32), h2.astype(np.float32), cv2.HISTCMP_BHATTACHARYYA)
            return max(0.0, 1.0 - dist)
        except Exception:
            return 0.0

    def prune_old_entries(self, current_time: float) -> None:
        to_delete = []
        for visitor_id, features in self._gallery.items():
            features[:] = [f for f in features if current_time - f.timestamp <= self.max_age_seconds]
            if not features:
                to_delete.append(visitor_id)
        for vid in to_delete:
            del self._gallery[vid]
