"""
Health endpoint — accurate service status for on-call engineers.
STALE_FEED warning if any store has >10 min event lag.
"""
import time
import logging
from datetime import datetime, timezone

from sqlalchemy import select, func, distinct
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import EventORM, check_db_health
from app.models import HealthResponse, StoreFeedStatus
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_startup_time = time.time()


async def get_health(db: AsyncSession) -> HealthResponse:
    now = datetime.utcnow()
    warnings = []

    # DB check
    db_ok = await check_db_health()
    db_status = "ok" if db_ok else "error"
    if not db_ok:
        warnings.append("Database is unavailable")

    # Total events ingested
    total_q = await db.execute(select(func.count()).select_from(EventORM))
    total_events: int = total_q.scalar() or 0

    # Per-store last event time
    store_last_q = await db.execute(
        select(
            EventORM.store_id,
            func.max(EventORM.timestamp).label("last_event"),
        ).group_by(EventORM.store_id)
    )
    store_rows = store_last_q.fetchall()

    stores = []
    for row in store_rows:
        store_id = row.store_id
        last_event_dt = row.last_event

        if last_event_dt is None:
            stores.append(StoreFeedStatus(
                store_id=store_id,
                last_event_at=None,
                lag_minutes=None,
                status="NO_DATA",
            ))
            continue

        lag_minutes = (now - last_event_dt).total_seconds() / 60
        if lag_minutes >= settings.STALE_FEED_THRESHOLD_MINUTES:
            status = "STALE_FEED"
            warnings.append(f"Store {store_id} feed is stale ({lag_minutes:.1f} min)")
        else:
            status = "OK"

        stores.append(StoreFeedStatus(
            store_id=store_id,
            last_event_at=last_event_dt.replace(tzinfo=timezone.utc),
            lag_minutes=round(lag_minutes, 1),
            status=status,
        ))

    overall_status = "healthy"
    if not db_ok:
        overall_status = "unhealthy"
    elif warnings:
        overall_status = "degraded"

    return HealthResponse(
        status=overall_status,
        uptime_seconds=round(time.time() - _startup_time, 1),
        database=db_status,
        total_events_ingested=total_events,
        stores=stores,
        warnings=warnings,
    )
