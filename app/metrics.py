"""
Real-time store metrics computation.
All metrics are computed live from the event store — never from yesterday's cache.
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional

from sqlalchemy import select, func, and_, distinct
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import EventORM, POSTransactionORM
from app.models import MetricsResponse, ZoneDwell
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


async def get_store_metrics(store_id: str, db: AsyncSession) -> MetricsResponse:
    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # ── Unique visitors today (exclude staff) ─────────────────────────────
    unique_visitors_q = await db.execute(
        select(func.count(distinct(EventORM.visitor_id))).where(
            and_(
                EventORM.store_id == store_id,
                EventORM.event_type == "ENTRY",
                EventORM.is_staff == False,
                EventORM.timestamp >= today_start,
            )
        )
    )
    unique_visitors: int = unique_visitors_q.scalar() or 0

    # ── Total entries / exits today ──────────────────────────────────────
    total_entries_q = await db.execute(
        select(func.count()).where(
            and_(
                EventORM.store_id == store_id,
                EventORM.event_type == "ENTRY",
                EventORM.is_staff == False,
                EventORM.timestamp >= today_start,
            )
        )
    )
    total_entries: int = total_entries_q.scalar() or 0

    total_exits_q = await db.execute(
        select(func.count()).where(
            and_(
                EventORM.store_id == store_id,
                EventORM.event_type == "EXIT",
                EventORM.is_staff == False,
                EventORM.timestamp >= today_start,
            )
        )
    )
    total_exits: int = total_exits_q.scalar() or 0

    # ── Conversion rate via POS correlation ──────────────────────────────
    conversion_rate = await _compute_conversion_rate(store_id, db, today_start)

    # ── Avg dwell per zone ────────────────────────────────────────────────
    zone_dwell_rows = await db.execute(
        select(
            EventORM.zone_id,
            func.avg(EventORM.dwell_ms).label("avg_dwell"),
            func.count().label("visit_count"),
        ).where(
            and_(
                EventORM.store_id == store_id,
                EventORM.event_type.in_(["ZONE_DWELL", "ZONE_ENTER"]),
                EventORM.zone_id.isnot(None),
                EventORM.is_staff == False,
                EventORM.timestamp >= today_start,
            )
        ).group_by(EventORM.zone_id)
    )
    avg_dwell_per_zone: List[ZoneDwell] = [
        ZoneDwell(
            zone_id=row.zone_id,
            avg_dwell_ms=round(row.avg_dwell or 0, 2),
            visit_count=row.visit_count,
        )
        for row in zone_dwell_rows.fetchall()
    ]

    # ── Current queue depth (most recent BILLING_QUEUE_JOIN) ──────────────
    queue_q = await db.execute(
        select(EventORM.queue_depth).where(
            and_(
                EventORM.store_id == store_id,
                EventORM.event_type == "BILLING_QUEUE_JOIN",
                EventORM.timestamp >= today_start,
            )
        ).order_by(EventORM.timestamp.desc(), EventORM.id.desc()).limit(1)
    )
    queue_row = queue_q.fetchone()
    current_queue_depth: int = (queue_row[0] or 0) if queue_row else 0

    # ── Abandonment rate ──────────────────────────────────────────────────
    abandonment_rate = await _compute_abandonment_rate(store_id, db, today_start)

    return MetricsResponse(
        store_id=store_id,
        as_of=now.replace(tzinfo=timezone.utc),
        unique_visitors=unique_visitors,
        conversion_rate=round(conversion_rate, 4),
        avg_dwell_per_zone=avg_dwell_per_zone,
        current_queue_depth=current_queue_depth,
        abandonment_rate=round(abandonment_rate, 4),
        total_entries=total_entries,
        total_exits=total_exits,
    )


async def _compute_conversion_rate(
    store_id: str, db: AsyncSession, since: datetime
) -> float:
    """
    A visitor is converted if they were in the billing zone within
    BILLING_CONVERSION_WINDOW_SEC before any POS transaction.
    """
    window_sec = settings.BILLING_CONVERSION_WINDOW_SEC

    # All unique customer visitors today
    all_visitors_q = await db.execute(
        select(distinct(EventORM.visitor_id)).where(
            and_(
                EventORM.store_id == store_id,
                EventORM.event_type == "ENTRY",
                EventORM.is_staff == False,
                EventORM.timestamp >= since,
            )
        )
    )
    all_visitor_ids = {row[0] for row in all_visitors_q.fetchall()}
    if not all_visitor_ids:
        return 0.0

    # POS transactions today
    pos_q = await db.execute(
        select(POSTransactionORM.timestamp).where(
            and_(
                POSTransactionORM.store_id == store_id,
                POSTransactionORM.timestamp >= since,
            )
        )
    )
    pos_timestamps = [row[0] for row in pos_q.fetchall()]
    if not pos_timestamps:
        return 0.0

    # Visitors who were in billing zone within window_sec before a POS tx
    converted_visitors: set = set()
    for pos_ts in pos_timestamps:
        window_start = pos_ts - timedelta(seconds=window_sec)
        billing_q = await db.execute(
            select(distinct(EventORM.visitor_id)).where(
                and_(
                    EventORM.store_id == store_id,
                    EventORM.event_type.in_(["BILLING_QUEUE_JOIN", "ZONE_ENTER"]),
                    EventORM.zone_id.ilike("%billing%"),
                    EventORM.is_staff == False,
                    EventORM.timestamp >= window_start,
                    EventORM.timestamp <= pos_ts,
                )
            )
        )
        for row in billing_q.fetchall():
            converted_visitors.add(row[0])

    return len(converted_visitors) / len(all_visitor_ids)


async def _compute_abandonment_rate(
    store_id: str, db: AsyncSession, since: datetime
) -> float:
    joins_q = await db.execute(
        select(func.count()).where(
            and_(
                EventORM.store_id == store_id,
                EventORM.event_type == "BILLING_QUEUE_JOIN",
                EventORM.is_staff == False,
                EventORM.timestamp >= since,
            )
        )
    )
    total_joins: int = joins_q.scalar() or 0
    if total_joins == 0:
        return 0.0

    abandons_q = await db.execute(
        select(func.count()).where(
            and_(
                EventORM.store_id == store_id,
                EventORM.event_type == "BILLING_QUEUE_ABANDON",
                EventORM.is_staff == False,
                EventORM.timestamp >= since,
            )
        )
    )
    total_abandons: int = abandons_q.scalar() or 0
    return total_abandons / total_joins
