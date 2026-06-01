from datetime import datetime, timedelta

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import EventORM


async def get_recent_event_window(store_id: str, db: AsyncSession, now: datetime) -> datetime:
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    last_24h_start = now - timedelta(hours=24)

    event_count_today_q = await db.execute(
        select(func.count()).where(
            EventORM.store_id == store_id,
            EventORM.timestamp >= today_start,
        )
    )
    today_events = event_count_today_q.scalar() or 0
    if today_events > 0:
        return today_start

    event_count_24h_q = await db.execute(
        select(func.count()).where(
            EventORM.store_id == store_id,
            EventORM.timestamp >= last_24h_start,
        )
    )
    last_24h_events = event_count_24h_q.scalar() or 0
    if last_24h_events > 0:
        return last_24h_start

    earliest_event_q = await db.execute(
        select(func.min(EventORM.timestamp)).where(
            EventORM.store_id == store_id,
        )
    )
    earliest_event = earliest_event_q.scalar()
    return earliest_event if earliest_event is not None else today_start
