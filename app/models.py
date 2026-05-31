from enum import Enum
from typing import Optional, List, Any
from datetime import datetime
from pydantic import BaseModel, Field, field_validator
import uuid


class EventType(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    ZONE_ENTER = "ZONE_ENTER"
    ZONE_EXIT = "ZONE_EXIT"
    ZONE_DWELL = "ZONE_DWELL"
    BILLING_QUEUE_JOIN = "BILLING_QUEUE_JOIN"
    BILLING_QUEUE_ABANDON = "BILLING_QUEUE_ABANDON"
    REENTRY = "REENTRY"


class EventMetadata(BaseModel):
    queue_depth: Optional[int] = None
    sku_zone: Optional[str] = None
    session_seq: Optional[int] = None


class StoreEvent(BaseModel):
    event_id: str = Field(..., description="UUID v4 — globally unique")
    store_id: str = Field(..., description="Store identifier from store_layout.json")
    camera_id: str = Field(..., description="Camera that produced this event")
    visitor_id: str = Field(..., description="Re-ID token unique per visit session")
    event_type: EventType
    timestamp: datetime = Field(..., description="ISO-8601 UTC")
    zone_id: Optional[str] = Field(None, description="Null for ENTRY/EXIT events")
    dwell_ms: int = Field(0, ge=0)
    is_staff: bool = False
    confidence: float = Field(..., ge=0.0, le=1.0)
    metadata: EventMetadata = Field(default_factory=EventMetadata)

    @field_validator("event_id")
    @classmethod
    def validate_uuid(cls, v: str) -> str:
        try:
            uuid.UUID(v)
        except ValueError:
            raise ValueError("event_id must be a valid UUID v4")
        return v


class IngestRequest(BaseModel):
    events: List[Any] = Field(..., max_length=500)
    store_id: Optional[str] = None


class IngestResult(BaseModel):
    event_id: str
    status: str  # "accepted" | "duplicate" | "invalid"
    error: Optional[str] = None


class IngestResponse(BaseModel):
    accepted: int
    duplicates: int
    invalid: int
    results: List[IngestResult]


# ── Metrics ──────────────────────────────────────────────────────────────────

class ZoneDwell(BaseModel):
    zone_id: str
    avg_dwell_ms: float
    visit_count: int


class MetricsResponse(BaseModel):
    store_id: str
    as_of: datetime
    unique_visitors: int
    conversion_rate: float
    avg_dwell_per_zone: List[ZoneDwell]
    current_queue_depth: int
    abandonment_rate: float
    total_entries: int
    total_exits: int


# ── Funnel ───────────────────────────────────────────────────────────────────

class FunnelStage(BaseModel):
    stage: str
    count: int
    drop_off_pct: float


class FunnelResponse(BaseModel):
    store_id: str
    as_of: datetime
    stages: List[FunnelStage]
    total_sessions: int


# ── Heatmap ──────────────────────────────────────────────────────────────────

class HeatmapZone(BaseModel):
    zone_id: str
    visit_frequency: int
    avg_dwell_ms: float
    normalized_score: float  # 0–100
    data_confidence: bool  # False if < 20 sessions


class HeatmapResponse(BaseModel):
    store_id: str
    as_of: datetime
    zones: List[HeatmapZone]


# ── Anomalies ────────────────────────────────────────────────────────────────

class AnomalySeverity(str, Enum):
    INFO = "INFO"
    WARN = "WARN"
    CRITICAL = "CRITICAL"


class AnomalyType(str, Enum):
    BILLING_QUEUE_SPIKE = "BILLING_QUEUE_SPIKE"
    CONVERSION_DROP = "CONVERSION_DROP"
    DEAD_ZONE = "DEAD_ZONE"
    STALE_FEED = "STALE_FEED"
    EMPTY_STORE = "EMPTY_STORE"


class Anomaly(BaseModel):
    anomaly_id: str
    anomaly_type: AnomalyType
    severity: AnomalySeverity
    store_id: str
    detected_at: datetime
    description: str
    suggested_action: str
    context: dict = Field(default_factory=dict)


class AnomaliesResponse(BaseModel):
    store_id: str
    as_of: datetime
    active_anomalies: List[Anomaly]


# ── Health ───────────────────────────────────────────────────────────────────

class StoreFeedStatus(BaseModel):
    store_id: str
    last_event_at: Optional[datetime]
    lag_minutes: Optional[float]
    status: str  # "OK" | "STALE_FEED" | "NO_DATA"


class HealthResponse(BaseModel):
    status: str  # "healthy" | "degraded" | "unhealthy"
    version: str = "1.0.0"
    uptime_seconds: float
    database: str  # "ok" | "error"
    total_events_ingested: int
    stores: List[StoreFeedStatus]
    warnings: List[str] = Field(default_factory=list)
