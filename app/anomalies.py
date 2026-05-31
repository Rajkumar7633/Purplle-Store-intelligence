"""
Anomaly detection engine.
Detects: BILLING_QUEUE_SPIKE, CONVERSION_DROP, DEAD_ZONE, STALE_FEED, EMPTY_STORE
"""
import uuid
import logging
from datetime import datetime, timezone, timedelta
from typing import List

from sqlalchemy import select, func, and_, distinct
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import EventORM, POSTransactionORM
from app.models import (
    AnomaliesResponse, Anomaly, AnomalyType, AnomalySeverity
)
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


async def get_store_anomalies(store_id: str, db: AsyncSession) -> AnomaliesResponse:
    now = datetime.utcnow()
    anomalies: List[Anomaly] = []

    checks = [
        _check_queue_spike(store_id, db, now),
        _check_conversion_drop(store_id, db, now),
        _check_dead_zones(store_id, db, now),
        _check_stale_feed(store_id, db, now),
        _check_empty_store(store_id, db, now),
    ]

    import asyncio
    results = await asyncio.gather(*checks, return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            logger.warning("Anomaly check failed: %s", result)
        elif result is not None:
            if isinstance(result, list):
                anomalies.extend(result)
            else:
                anomalies.append(result)

    return AnomaliesResponse(
        store_id=store_id,
        as_of=now.replace(tzinfo=timezone.utc),
        active_anomalies=anomalies,
    )


async def _check_queue_spike(
    store_id: str, db: AsyncSession, now: datetime
) -> List[Anomaly]:
    recent_cutoff = now - timedelta(minutes=5)
    q = await db.execute(
        select(EventORM.queue_depth).where(
            and_(
                EventORM.store_id == store_id,
                EventORM.event_type == "BILLING_QUEUE_JOIN",
                EventORM.timestamp >= recent_cutoff,
                EventORM.queue_depth.isnot(None),
            )
        ).order_by(EventORM.timestamp.desc()).limit(1)
    )
    row = q.fetchone()
    if row and row[0] and row[0] >= settings.ANOMALY_QUEUE_SPIKE_THRESHOLD:
        severity = (
            AnomalySeverity.CRITICAL if row[0] >= settings.ANOMALY_QUEUE_SPIKE_THRESHOLD * 2
            else AnomalySeverity.WARN
        )
        return [Anomaly(
            anomaly_id=str(uuid.uuid4()),
            anomaly_type=AnomalyType.BILLING_QUEUE_SPIKE,
            severity=severity,
            store_id=store_id,
            detected_at=now.replace(tzinfo=timezone.utc),
            description=f"Billing queue depth {row[0]} exceeds threshold {settings.ANOMALY_QUEUE_SPIKE_THRESHOLD}",
            suggested_action="Open additional billing counter or redirect staff to billing zone",
            context={"current_queue_depth": row[0], "threshold": settings.ANOMALY_QUEUE_SPIKE_THRESHOLD},
        )]
    return []


async def _check_conversion_drop(
    store_id: str, db: AsyncSession, now: datetime
) -> List[Anomaly]:
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Today's conversion
    today_visitors_q = await db.execute(
        select(func.count(distinct(EventORM.visitor_id))).where(
            and_(
                EventORM.store_id == store_id,
                EventORM.event_type == "ENTRY",
                EventORM.is_staff == False,
                EventORM.timestamp >= today_start,
            )
        )
    )
    today_visitors: int = today_visitors_q.scalar() or 0
    if today_visitors == 0:
        return []

    today_pos_q = await db.execute(
        select(func.count()).where(
            and_(
                POSTransactionORM.store_id == store_id,
                POSTransactionORM.timestamp >= today_start,
            )
        )
    )
    today_txns: int = today_pos_q.scalar() or 0
    today_rate = today_txns / today_visitors if today_visitors > 0 else 0.0

    # 7-day average (excluding today)
    week_start = today_start - timedelta(days=7)
    week_visitors_q = await db.execute(
        select(func.count(distinct(EventORM.visitor_id))).where(
            and_(
                EventORM.store_id == store_id,
                EventORM.event_type == "ENTRY",
                EventORM.is_staff == False,
                EventORM.timestamp >= week_start,
                EventORM.timestamp < today_start,
            )
        )
    )
    week_visitors: int = week_visitors_q.scalar() or 0

    if week_visitors == 0:
        return []

    week_pos_q = await db.execute(
        select(func.count()).where(
            and_(
                POSTransactionORM.store_id == store_id,
                POSTransactionORM.timestamp >= week_start,
                POSTransactionORM.timestamp < today_start,
            )
        )
    )
    week_txns: int = week_pos_q.scalar() or 0
    week_rate = week_txns / week_visitors if week_visitors > 0 else 0.0

    if week_rate > 0 and (week_rate - today_rate) / week_rate >= settings.ANOMALY_CONVERSION_DROP_PCT:
        drop_pct = round((week_rate - today_rate) / week_rate * 100, 1)
        return [Anomaly(
            anomaly_id=str(uuid.uuid4()),
            anomaly_type=AnomalyType.CONVERSION_DROP,
            severity=AnomalySeverity.WARN,
            store_id=store_id,
            detected_at=now.replace(tzinfo=timezone.utc),
            description=f"Conversion rate dropped {drop_pct}% vs 7-day average ({week_rate:.2%} → {today_rate:.2%})",
            suggested_action="Review staff placement, product visibility, and billing queue wait times",
            context={
                "today_rate": round(today_rate, 4),
                "week_avg_rate": round(week_rate, 4),
                "drop_pct": drop_pct,
            },
        )]
    return []


async def _check_dead_zones(
    store_id: str, db: AsyncSession, now: datetime
) -> List[Anomaly]:
    cutoff = now - timedelta(minutes=settings.DEAD_ZONE_MINUTES)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Zones that were active today
    active_zones_q = await db.execute(
        select(distinct(EventORM.zone_id)).where(
            and_(
                EventORM.store_id == store_id,
                EventORM.zone_id.isnot(None),
                EventORM.timestamp >= today_start,
            )
        )
    )
    all_active_zones = {row[0] for row in active_zones_q.fetchall()}

    # Zones with recent activity
    recent_zones_q = await db.execute(
        select(distinct(EventORM.zone_id)).where(
            and_(
                EventORM.store_id == store_id,
                EventORM.zone_id.isnot(None),
                EventORM.timestamp >= cutoff,
            )
        )
    )
    recent_zones = {row[0] for row in recent_zones_q.fetchall()}

    dead_zones = all_active_zones - recent_zones
    anomalies = []
    for zone in dead_zones:
        anomalies.append(Anomaly(
            anomaly_id=str(uuid.uuid4()),
            anomaly_type=AnomalyType.DEAD_ZONE,
            severity=AnomalySeverity.INFO,
            store_id=store_id,
            detected_at=now.replace(tzinfo=timezone.utc),
            description=f"Zone '{zone}' has had no customer visits in the last {settings.DEAD_ZONE_MINUTES} minutes",
            suggested_action=f"Check camera coverage for zone '{zone}', or review product placement",
            context={"zone_id": zone, "inactive_minutes": settings.DEAD_ZONE_MINUTES},
        ))
    return anomalies


async def _check_stale_feed(
    store_id: str, db: AsyncSession, now: datetime
) -> List[Anomaly]:
    q = await db.execute(
        select(func.max(EventORM.timestamp)).where(
            EventORM.store_id == store_id
        )
    )
    last_event = q.scalar()
    if last_event is None:
        return []

    lag_minutes = (now - last_event).total_seconds() / 60
    if lag_minutes >= settings.STALE_FEED_THRESHOLD_MINUTES:
        severity = (
            AnomalySeverity.CRITICAL if lag_minutes >= settings.STALE_FEED_THRESHOLD_MINUTES * 3
            else AnomalySeverity.WARN
        )
        return [Anomaly(
            anomaly_id=str(uuid.uuid4()),
            anomaly_type=AnomalyType.STALE_FEED,
            severity=severity,
            store_id=store_id,
            detected_at=now.replace(tzinfo=timezone.utc),
            description=f"No events received for {lag_minutes:.1f} minutes (threshold: {settings.STALE_FEED_THRESHOLD_MINUTES} min)",
            suggested_action="Check camera connectivity, pipeline process, and network",
            context={"lag_minutes": round(lag_minutes, 1), "last_event_at": last_event.isoformat()},
        )]
    return []


async def _check_empty_store(
    store_id: str, db: AsyncSession, now: datetime
) -> List[Anomaly]:
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    q = await db.execute(
        select(func.count()).where(
            and_(
                EventORM.store_id == store_id,
                EventORM.event_type == "ENTRY",
                EventORM.is_staff == False,
                EventORM.timestamp >= today_start,
            )
        )
    )
    count: int = q.scalar() or 0

    # Store has been open for over 1 hour with zero visitors
    store_open_minutes = (now - today_start).total_seconds() / 60
    if count == 0 and store_open_minutes > 60:
        return [Anomaly(
            anomaly_id=str(uuid.uuid4()),
            anomaly_type=AnomalyType.EMPTY_STORE,
            severity=AnomalySeverity.INFO,
            store_id=store_id,
            detected_at=now.replace(tzinfo=timezone.utc),
            description="Zero customer entries recorded today",
            suggested_action="Verify entry camera is operational and detection pipeline is running",
            context={"store_open_minutes": round(store_open_minutes, 1)},
        )]
    return []
