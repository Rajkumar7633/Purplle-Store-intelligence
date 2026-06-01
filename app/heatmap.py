"""
Zone heatmap: visit frequency + avg dwell, normalised 0-100.
data_confidence = False when fewer than 20 sessions in window.
"""
import logging
from datetime import datetime, timezone
from typing import List

from sqlalchemy import select, func, and_, distinct
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import EventORM
from app.models import HeatmapResponse, HeatmapZone
from app.time_utils import get_recent_event_window

logger = logging.getLogger(__name__)
MIN_SESSIONS_FOR_CONFIDENCE = 20


async def get_store_heatmap(store_id: str, db: AsyncSession) -> HeatmapResponse:
    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    since = await get_recent_event_window(store_id, db, now)
    if since != today_start:
        logger.warning(
            "No events today for %s, falling back to earliest available historical data",
            store_id,
        )

    # Total unique customer sessions for the selected window
    total_sessions_q = await db.execute(
        select(func.count(distinct(EventORM.visitor_id))).where(
            and_(
                EventORM.store_id == store_id,
                EventORM.event_type == "ENTRY",
                EventORM.is_staff == False,
                EventORM.timestamp >= since,
            )
        )
    )
    total_sessions: int = total_sessions_q.scalar() or 0
    data_confidence = total_sessions >= MIN_SESSIONS_FOR_CONFIDENCE

    # Zone visit frequency + avg dwell
    zone_q = await db.execute(
        select(
            EventORM.zone_id,
            func.count().label("visit_count"),
            func.avg(EventORM.dwell_ms).label("avg_dwell"),
        ).where(
            and_(
                EventORM.store_id == store_id,
                EventORM.event_type.in_(["ZONE_ENTER", "ZONE_DWELL", "ZONE_EXIT"]),
                EventORM.zone_id.isnot(None),
                EventORM.is_staff == False,
                EventORM.timestamp >= since,
            )
        ).group_by(EventORM.zone_id)
    )
    rows = zone_q.fetchall()

    if not rows:
        return HeatmapResponse(
            store_id=store_id,
            as_of=now.replace(tzinfo=timezone.utc),
            zones=[],
        )

    max_visits = max(row.visit_count for row in rows) or 1

    zones: List[HeatmapZone] = []
    for row in rows:
        normalized = round((row.visit_count / max_visits) * 100, 2)
        zones.append(HeatmapZone(
            zone_id=row.zone_id,
            visit_frequency=row.visit_count,
            avg_dwell_ms=round(row.avg_dwell or 0, 2),
            normalized_score=normalized,
            data_confidence=data_confidence,
        ))

    zones.sort(key=lambda z: z.normalized_score, reverse=True)

    return HeatmapResponse(
        store_id=store_id,
        as_of=now.replace(tzinfo=timezone.utc),
        zones=zones,
    )
