"""
Direct unit tests for service functions (metrics, heatmap, funnel, health).
Calls service functions directly with the test DB session for higher coverage.
"""
import pytest
import uuid
from datetime import datetime, timezone, timedelta

from app.metrics import get_store_metrics
from app.heatmap import get_store_heatmap
from app.funnel import get_store_funnel
from app.anomalies import get_store_anomalies

from tests.conftest import STORE_ID, seed_events, seed_pos, make_event_dict

NOW = datetime.now(timezone.utc)


# ── Metrics service unit tests ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_metrics_service_returns_metrics(db_session):
    events = [make_event_dict("ENTRY") for _ in range(3)]
    events += [make_event_dict("ZONE_ENTER", zone_id="SKINCARE", dwell_ms=5000) for _ in range(2)]
    await seed_events(db_session, events)
    result = await get_store_metrics(STORE_ID, db_session)
    assert result.store_id == STORE_ID
    assert result.unique_visitors == 3
    assert result.total_entries == 3


@pytest.mark.asyncio
async def test_metrics_service_conversion_no_pos(db_session):
    events = [make_event_dict("ENTRY") for _ in range(5)]
    await seed_events(db_session, events)
    result = await get_store_metrics(STORE_ID, db_session)
    assert result.conversion_rate == 0.0


@pytest.mark.asyncio
async def test_metrics_service_with_zone_dwell(db_session):
    vid = f"VIS_{uuid.uuid4().hex[:6]}"
    events = [
        make_event_dict("ENTRY", visitor_id=vid),
        make_event_dict("ZONE_ENTER", visitor_id=vid, zone_id="HAIRCARE", dwell_ms=0),
        make_event_dict("ZONE_DWELL", visitor_id=vid, zone_id="HAIRCARE", dwell_ms=60000),
        make_event_dict("ZONE_EXIT", visitor_id=vid, zone_id="HAIRCARE", dwell_ms=90000),
    ]
    await seed_events(db_session, events)
    result = await get_store_metrics(STORE_ID, db_session)
    zone_ids = [z.zone_id for z in result.avg_dwell_per_zone]
    assert "HAIRCARE" in zone_ids


@pytest.mark.asyncio
async def test_metrics_service_queue_depth(db_session):
    events = [
        make_event_dict("BILLING_QUEUE_JOIN", zone_id="BILLING", queue_depth=3),
        make_event_dict("BILLING_QUEUE_JOIN", zone_id="BILLING", queue_depth=4),
    ]
    await seed_events(db_session, events)
    result = await get_store_metrics(STORE_ID, db_session)
    assert result.current_queue_depth == 4


@pytest.mark.asyncio
async def test_metrics_service_abandonment(db_session):
    events = [
        make_event_dict("BILLING_QUEUE_JOIN", queue_depth=1),
        make_event_dict("BILLING_QUEUE_JOIN", queue_depth=2),
        make_event_dict("BILLING_QUEUE_ABANDON", queue_depth=1),
    ]
    await seed_events(db_session, events)
    result = await get_store_metrics(STORE_ID, db_session)
    assert result.abandonment_rate == pytest.approx(0.5, rel=0.01)


@pytest.mark.asyncio
async def test_metrics_service_exits(db_session):
    events = [
        make_event_dict("ENTRY"),
        make_event_dict("ENTRY"),
        make_event_dict("EXIT"),
    ]
    await seed_events(db_session, events)
    result = await get_store_metrics(STORE_ID, db_session)
    assert result.total_exits == 1


# ── Heatmap service unit tests ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_heatmap_service_zones(db_session):
    events = [
        make_event_dict("ZONE_ENTER", zone_id="SKINCARE"),
        make_event_dict("ZONE_ENTER", zone_id="SKINCARE"),
        make_event_dict("ZONE_ENTER", zone_id="FRAGRANCE"),
        make_event_dict("ZONE_DWELL", zone_id="SKINCARE", dwell_ms=30000),
    ]
    await seed_events(db_session, events)
    result = await get_store_heatmap(STORE_ID, db_session)
    assert result.store_id == STORE_ID
    zone_ids = [z.zone_id for z in result.zones]
    assert "SKINCARE" in zone_ids
    assert "FRAGRANCE" in zone_ids


@pytest.mark.asyncio
async def test_heatmap_service_sorted_by_score(db_session):
    events = [make_event_dict("ZONE_ENTER", zone_id="SKINCARE") for _ in range(5)]
    events += [make_event_dict("ZONE_ENTER", zone_id="FRAGRANCE") for _ in range(2)]
    await seed_events(db_session, events)
    result = await get_store_heatmap(STORE_ID, db_session)
    scores = [z.normalized_score for z in result.zones]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] == 100.0


@pytest.mark.asyncio
async def test_heatmap_service_confidence_low_sessions(db_session):
    events = [make_event_dict("ZONE_ENTER", zone_id="MAKEUP")]
    await seed_events(db_session, events)
    result = await get_store_heatmap(STORE_ID, db_session)
    assert result.zones[0].data_confidence is False


@pytest.mark.asyncio
async def test_heatmap_service_empty(db_session):
    result = await get_store_heatmap(STORE_ID, db_session)
    assert result.zones == []


# ── Funnel service unit tests ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_funnel_service_full_path(db_session):
    vid = f"VIS_{uuid.uuid4().hex[:6]}"
    events = [
        make_event_dict("ENTRY", visitor_id=vid),
        make_event_dict("ZONE_ENTER", visitor_id=vid, zone_id="SKINCARE"),
        make_event_dict("BILLING_QUEUE_JOIN", visitor_id=vid, zone_id="BILLING"),
    ]
    await seed_events(db_session, events)
    await seed_pos(db_session, STORE_ID, [NOW + timedelta(minutes=1)])
    result = await get_store_funnel(STORE_ID, db_session)
    assert result.store_id == STORE_ID
    assert result.total_sessions == 1
    assert result.stages[0].stage == "Entry"
    assert result.stages[0].count == 1


@pytest.mark.asyncio
async def test_funnel_service_no_data(db_session):
    result = await get_store_funnel(STORE_ID, db_session)
    assert result.total_sessions == 0
    assert all(s.count == 0 for s in result.stages)


@pytest.mark.asyncio
async def test_funnel_service_billing_stage(db_session):
    vids = [f"VIS_{uuid.uuid4().hex[:6]}" for _ in range(4)]
    events = [make_event_dict("ENTRY", visitor_id=v) for v in vids]
    events += [make_event_dict("ZONE_ENTER", visitor_id=v, zone_id="SKINCARE") for v in vids[:3]]
    events += [make_event_dict("BILLING_QUEUE_JOIN", visitor_id=v, zone_id="BILLING") for v in vids[:2]]
    await seed_events(db_session, events)
    result = await get_store_funnel(STORE_ID, db_session)
    entry_count = next(s.count for s in result.stages if s.stage == "Entry")
    billing_count = next(s.count for s in result.stages if s.stage == "Billing Queue")
    assert entry_count == 4
    assert billing_count == 2


# ── Anomaly service unit tests ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_anomaly_service_no_anomalies(db_session):
    result = await get_store_anomalies(STORE_ID, db_session)
    assert result.store_id == STORE_ID
    assert isinstance(result.active_anomalies, list)


@pytest.mark.asyncio
async def test_anomaly_service_queue_spike_direct(db_session):
    events = [
        make_event_dict("BILLING_QUEUE_JOIN", queue_depth=6),
    ]
    await seed_events(db_session, events)
    result = await get_store_anomalies(STORE_ID, db_session)
    types = [a.anomaly_type for a in result.active_anomalies]
    assert "BILLING_QUEUE_SPIKE" in types
