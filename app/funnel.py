"""
Session-based conversion funnel.
Unit of counting is a session (unique visitor), not raw events.
Re-entries are NOT double-counted — same visitor_id treated as one session.
Funnel: Entry → Zone Visit → Billing Queue → Purchase
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Set, Dict

from sqlalchemy import select, and_, distinct
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import EventORM, POSTransactionORM
from app.models import FunnelResponse, FunnelStage
from app.config import get_settings
from app.time_utils import get_recent_event_window

logger = logging.getLogger(__name__)
settings = get_settings()


async def get_store_funnel(store_id: str, db: AsyncSession) -> FunnelResponse:
    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    since = await get_recent_event_window(store_id, db, now)
    if since != today_start:
        logger.warning(
            "No events today for %s, falling back to earliest available historical data",
            store_id,
        )

    # ── Stage 1: All unique customer sessions (entries) ──────────────────
    entry_q = await db.execute(
        select(distinct(EventORM.visitor_id)).where(
            and_(
                EventORM.store_id == store_id,
                EventORM.event_type == "ENTRY",
                EventORM.is_staff == False,
                EventORM.timestamp >= since,
            )
        )
    )
    entered_visitors: Set[str] = {row[0] for row in entry_q.fetchall()}
    entered_list = list(entered_visitors)
    total_sessions = len(entered_visitors)

    # ── Stage 2: Visitors who entered at least one product zone ──────────
    zone_q = await db.execute(
        select(distinct(EventORM.visitor_id)).where(
            and_(
                EventORM.store_id == store_id,
                EventORM.event_type.in_(["ZONE_ENTER", "ZONE_DWELL"]),
                EventORM.zone_id.isnot(None),
                EventORM.zone_id.not_ilike("%entry%"),
                EventORM.zone_id.not_ilike("%exit%"),
                EventORM.is_staff == False,
                EventORM.timestamp >= since,
                EventORM.visitor_id.in_(entered_list),
            )
        )
    )
    zone_visitors: Set[str] = {row[0] for row in zone_q.fetchall()}

    # ── Stage 3: Visitors who entered billing queue ───────────────────────
    billing_q = await db.execute(
        select(distinct(EventORM.visitor_id)).where(
            and_(
                EventORM.store_id == store_id,
                EventORM.event_type.in_(["BILLING_QUEUE_JOIN", "ZONE_ENTER"]),
                EventORM.zone_id.ilike("%billing%"),
                EventORM.is_staff == False,
                EventORM.timestamp >= since,
                EventORM.visitor_id.in_(entered_list),
            )
        )
    )
    billing_visitors: Set[str] = {row[0] for row in billing_q.fetchall()}

    # ── Stage 4: Visitors who completed purchase (POS correlation) ───────
    converted_visitors = await _get_converted_visitors(
        store_id, db, since, entered_visitors
    )

    # ── Build funnel stages ───────────────────────────────────────────────
    counts = [
        ("Entry", total_sessions),
        ("Zone Visit", len(zone_visitors)),
        ("Billing Queue", len(billing_visitors)),
        ("Purchase", len(converted_visitors)),
    ]

    stages: List[FunnelStage] = []
    for i, (stage_name, count) in enumerate(counts):
        if i == 0:
            drop_pct = 0.0
        else:
            prev_count = counts[i - 1][1]
            drop_pct = round((1 - count / prev_count) * 100, 2) if prev_count > 0 else 0.0
        stages.append(FunnelStage(stage=stage_name, count=count, drop_off_pct=drop_pct))

    return FunnelResponse(
        store_id=store_id,
        as_of=now.replace(tzinfo=timezone.utc),
        stages=stages,
        total_sessions=total_sessions,
    )


async def _get_converted_visitors(
    store_id: str,
    db: AsyncSession,
    since: datetime,
    candidate_visitors: Set[str],
) -> Set[str]:
    """Visitors present in billing zone within BILLING_CONVERSION_WINDOW_SEC before a POS tx."""
    if not candidate_visitors:
        return set()

    window_sec = settings.BILLING_CONVERSION_WINDOW_SEC

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
        return set()

    converted: Set[str] = set()
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
                    EventORM.visitor_id.in_(list(candidate_visitors)),
                )
            )
        )
        for row in billing_q.fetchall():
            converted.add(row[0])

    return converted
