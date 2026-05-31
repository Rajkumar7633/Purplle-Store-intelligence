"""
Event ingestion: validate, deduplicate by event_id, store to DB.
POST /events/ingest is idempotent — safe to call twice with same payload.
"""
import logging
from typing import Any, List

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import StoreEvent, IngestResponse, IngestResult
from app.database import EventORM

logger = logging.getLogger(__name__)


async def ingest_events(
    raw_events: List[Any],
    db: AsyncSession,
) -> IngestResponse:
    results: List[IngestResult] = []
    accepted = 0
    duplicates = 0
    invalid = 0

    # Parse and validate each event individually
    events: List[StoreEvent] = []
    for raw in raw_events:
        try:
            events.append(StoreEvent.model_validate(raw))
        except (ValidationError, Exception) as exc:
            eid = raw.get("event_id", "unknown") if isinstance(raw, dict) else "unknown"
            results.append(IngestResult(event_id=eid, status="invalid", error=str(exc)[:200]))
            invalid += 1

    # Fetch all event_ids that already exist in one query
    event_ids = [e.event_id for e in events]
    existing_stmt = select(EventORM.event_id).where(EventORM.event_id.in_(event_ids))
    existing_result = await db.execute(existing_stmt)
    existing_ids = {row[0] for row in existing_result.fetchall()}

    for event in events:
        if event.event_id in existing_ids:
            results.append(IngestResult(
                event_id=event.event_id,
                status="duplicate",
            ))
            duplicates += 1
            continue

        try:
            orm_event = EventORM(
                event_id=event.event_id,
                store_id=event.store_id,
                camera_id=event.camera_id,
                visitor_id=event.visitor_id,
                event_type=event.event_type.value,
                timestamp=event.timestamp.replace(tzinfo=None),
                zone_id=event.zone_id,
                dwell_ms=event.dwell_ms,
                is_staff=event.is_staff,
                confidence=event.confidence,
                queue_depth=event.metadata.queue_depth,
                sku_zone=event.metadata.sku_zone,
                session_seq=event.metadata.session_seq,
            )
            db.add(orm_event)
            existing_ids.add(event.event_id)  # prevent intra-batch duplicates

            results.append(IngestResult(event_id=event.event_id, status="accepted"))
            accepted += 1

        except Exception as exc:
            logger.warning("Failed to ingest event %s: %s", event.event_id, exc)
            results.append(IngestResult(
                event_id=event.event_id,
                status="invalid",
                error=str(exc),
            ))
            invalid += 1

    if accepted > 0:
        await db.flush()

    logger.info(
        "Ingest complete",
        extra={
            "event_count": len(events),
            "accepted": accepted,
            "duplicates": duplicates,
            "invalid": invalid,
        },
    )

    return IngestResponse(
        accepted=accepted,
        duplicates=duplicates,
        invalid=invalid,
        results=results,
    )
