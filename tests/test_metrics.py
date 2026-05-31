# PROMPT: "Write pytest tests for a real-time retail store metrics API endpoint.
# The endpoint GET /stores/{store_id}/metrics must: exclude is_staff=True visitors,
# handle zero-purchase stores (conversion_rate=0.0, not null), compute conversion rate
# via POS timestamp correlation, return current queue depth from latest BILLING_QUEUE_JOIN,
# and handle empty store (no events) without crashing."
#
# CHANGES MADE:
# - Added explicit POS seeding to test conversion rate (AI suggested mocking, rejected — real DB is better)
# - Tested that re-entry doesn't inflate unique_visitors (same visitor_id counted once)
# - Added assertion on avg_dwell_per_zone structure, not just presence

import pytest
from datetime import timezone, timedelta
from tests.conftest import make_event_dict, seed_events, seed_pos, STORE_ID, NOW


@pytest.mark.asyncio
async def test_metrics_empty_store(client):
    """Empty store → all zeros, no crash."""
    resp = await client.get(f"/stores/{STORE_ID}/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["unique_visitors"] == 0
    assert body["conversion_rate"] == 0.0
    assert body["current_queue_depth"] == 0
    assert body["abandonment_rate"] == 0.0


@pytest.mark.asyncio
async def test_metrics_unique_visitors_excludes_staff(client, db_session):
    events = [
        make_event_dict("ENTRY", visitor_id="VIS_cust01"),
        make_event_dict("ENTRY", visitor_id="VIS_cust02"),
        make_event_dict("ENTRY", visitor_id="VIS_staff01", is_staff=True),
    ]
    await seed_events(db_session, events)
    resp = await client.get(f"/stores/{STORE_ID}/metrics")
    assert resp.json()["unique_visitors"] == 2  # staff excluded


@pytest.mark.asyncio
async def test_metrics_reentry_not_double_counted(client, db_session):
    """Same visitor_id with ENTRY + REENTRY should count as 1 unique visitor."""
    events = [
        make_event_dict("ENTRY", visitor_id="VIS_abc123"),
        make_event_dict("EXIT", visitor_id="VIS_abc123"),
        make_event_dict("REENTRY", visitor_id="VIS_abc123"),
        # REENTRY still has ENTRY in the record from first visit
    ]
    await seed_events(db_session, events)
    resp = await client.get(f"/stores/{STORE_ID}/metrics")
    assert resp.json()["unique_visitors"] == 1


@pytest.mark.asyncio
async def test_metrics_zero_purchases(client, db_session):
    events = [make_event_dict("ENTRY") for _ in range(10)]
    await seed_events(db_session, events)
    resp = await client.get(f"/stores/{STORE_ID}/metrics")
    body = resp.json()
    assert body["unique_visitors"] == 10
    assert body["conversion_rate"] == 0.0  # not null


@pytest.mark.asyncio
async def test_metrics_conversion_rate_with_pos(client, db_session):
    # Visitor in billing zone 2 minutes before POS tx
    billing_ts = NOW.replace(tzinfo=None) - timedelta(minutes=2)
    billing_events = [
        make_event_dict("ENTRY", visitor_id="VIS_buyer"),
        make_event_dict("BILLING_QUEUE_JOIN", visitor_id="VIS_buyer",
                        zone_id="BILLING", queue_depth=1,
                        timestamp=NOW - timedelta(minutes=2)),
        make_event_dict("ENTRY", visitor_id="VIS_browser"),
    ]
    await seed_events(db_session, billing_events)
    await seed_pos(db_session, STORE_ID, [NOW])

    resp = await client.get(f"/stores/{STORE_ID}/metrics")
    body = resp.json()
    assert body["conversion_rate"] > 0.0
    assert body["unique_visitors"] == 2


@pytest.mark.asyncio
async def test_metrics_queue_depth(client, db_session):
    events = [
        make_event_dict("BILLING_QUEUE_JOIN", zone_id="BILLING", queue_depth=3),
    ]
    await seed_events(db_session, events)
    resp = await client.get(f"/stores/{STORE_ID}/metrics")
    assert resp.json()["current_queue_depth"] == 3


@pytest.mark.asyncio
async def test_metrics_abandonment_rate(client, db_session):
    events = [
        make_event_dict("BILLING_QUEUE_JOIN", zone_id="BILLING", queue_depth=2),
        make_event_dict("BILLING_QUEUE_JOIN", zone_id="BILLING", queue_depth=2),
        make_event_dict("BILLING_QUEUE_ABANDON"),
    ]
    await seed_events(db_session, events)
    resp = await client.get(f"/stores/{STORE_ID}/metrics")
    body = resp.json()
    assert 0.0 <= body["abandonment_rate"] <= 1.0


@pytest.mark.asyncio
async def test_metrics_avg_dwell_structure(client, db_session):
    events = [
        make_event_dict("ZONE_DWELL", zone_id="SKINCARE", dwell_ms=45000),
        make_event_dict("ZONE_DWELL", zone_id="SKINCARE", dwell_ms=30000),
        make_event_dict("ZONE_DWELL", zone_id="HAIRCARE", dwell_ms=20000),
    ]
    await seed_events(db_session, events)
    resp = await client.get(f"/stores/{STORE_ID}/metrics")
    zones = resp.json()["avg_dwell_per_zone"]
    zone_ids = {z["zone_id"] for z in zones}
    assert "SKINCARE" in zone_ids
    assert "HAIRCARE" in zone_ids
    skincare = next(z for z in zones if z["zone_id"] == "SKINCARE")
    assert skincare["avg_dwell_ms"] == pytest.approx(37500.0)
