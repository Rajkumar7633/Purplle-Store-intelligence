# PROMPT: "Write pytest tests for a retail store anomaly detection API.
# Anomaly types: BILLING_QUEUE_SPIKE (queue >= threshold), CONVERSION_DROP (>20% drop vs 7d avg),
# DEAD_ZONE (no zone activity in 30 min), STALE_FEED (no events in 10 min).
# Each anomaly must have severity (INFO/WARN/CRITICAL) and suggested_action.
# Test: no anomalies when normal, correct detection when triggered, severity escalation."
#
# CHANGES MADE:
# - AI generated only happy-path tests; added tests for severity escalation
# - Added test that DEAD_ZONE only fires for zones that were active today (not phantom zones)
# - Removed mock patching in favor of real DB state for better integration coverage

import pytest
from datetime import timedelta, timezone
from tests.conftest import make_event_dict, seed_events, seed_pos, STORE_ID, NOW


@pytest.mark.asyncio
async def test_anomalies_empty_store(client):
    resp = await client.get(f"/stores/{STORE_ID}/anomalies")
    assert resp.status_code == 200
    body = resp.json()
    assert "active_anomalies" in body
    # Empty store may trigger EMPTY_STORE but not crash
    for a in body["active_anomalies"]:
        assert "anomaly_type" in a
        assert "severity" in a
        assert "suggested_action" in a
        assert len(a["suggested_action"]) > 0


@pytest.mark.asyncio
async def test_anomaly_queue_spike_detected(client, db_session):
    events = [
        make_event_dict(
            "BILLING_QUEUE_JOIN",
            zone_id="BILLING",
            queue_depth=6,  # above threshold of 5
            timestamp=NOW - timedelta(minutes=2),
        )
    ]
    await seed_events(db_session, events)
    resp = await client.get(f"/stores/{STORE_ID}/anomalies")
    anomalies = resp.json()["active_anomalies"]
    queue_anomalies = [a for a in anomalies if a["anomaly_type"] == "BILLING_QUEUE_SPIKE"]
    assert len(queue_anomalies) >= 1
    assert queue_anomalies[0]["severity"] in ("WARN", "CRITICAL")


@pytest.mark.asyncio
async def test_anomaly_queue_spike_severity_escalation(client, db_session):
    """queue_depth >= 2*threshold → CRITICAL, otherwise WARN."""
    events = [
        make_event_dict(
            "BILLING_QUEUE_JOIN",
            zone_id="BILLING",
            queue_depth=12,  # >= 2 * threshold(5) = 10
            timestamp=NOW - timedelta(minutes=1),
        )
    ]
    await seed_events(db_session, events)
    resp = await client.get(f"/stores/{STORE_ID}/anomalies")
    anomalies = resp.json()["active_anomalies"]
    queue_anomalies = [a for a in anomalies if a["anomaly_type"] == "BILLING_QUEUE_SPIKE"]
    assert queue_anomalies[0]["severity"] == "CRITICAL"


@pytest.mark.asyncio
async def test_anomaly_no_queue_spike_when_normal(client, db_session):
    events = [
        make_event_dict("BILLING_QUEUE_JOIN", zone_id="BILLING", queue_depth=2,
                        timestamp=NOW - timedelta(minutes=1))
    ]
    await seed_events(db_session, events)
    resp = await client.get(f"/stores/{STORE_ID}/anomalies")
    anomalies = resp.json()["active_anomalies"]
    queue_anomalies = [a for a in anomalies if a["anomaly_type"] == "BILLING_QUEUE_SPIKE"]
    assert len(queue_anomalies) == 0


@pytest.mark.asyncio
async def test_anomaly_stale_feed_detected(client, db_session):
    # Insert an event that's 15 minutes old (> 10 min threshold)
    old_ts = NOW - timedelta(minutes=15)
    events = [make_event_dict("ENTRY", timestamp=old_ts)]
    await seed_events(db_session, events)
    resp = await client.get(f"/stores/{STORE_ID}/anomalies")
    anomalies = resp.json()["active_anomalies"]
    stale = [a for a in anomalies if a["anomaly_type"] == "STALE_FEED"]
    assert len(stale) >= 1


@pytest.mark.asyncio
async def test_anomaly_no_stale_feed_with_recent_events(client, db_session):
    events = [make_event_dict("ENTRY", timestamp=NOW - timedelta(minutes=1))]
    await seed_events(db_session, events)
    resp = await client.get(f"/stores/{STORE_ID}/anomalies")
    anomalies = resp.json()["active_anomalies"]
    stale = [a for a in anomalies if a["anomaly_type"] == "STALE_FEED"]
    assert len(stale) == 0


@pytest.mark.asyncio
async def test_anomaly_dead_zone_only_for_active_zones(client, db_session):
    """DEAD_ZONE should only fire for zones that were active today."""
    # SKINCARE was active 4 hours ago — always outside the 30-min dead-zone window
    old_activity = NOW - timedelta(hours=4)
    events = [
        make_event_dict("ZONE_ENTER", zone_id="SKINCARE", timestamp=old_activity),
    ]
    await seed_events(db_session, events)
    resp = await client.get(f"/stores/{STORE_ID}/anomalies")
    anomalies = resp.json()["active_anomalies"]
    dead_zones = [a for a in anomalies if a["anomaly_type"] == "DEAD_ZONE"]
    zone_ids = [a["context"]["zone_id"] for a in dead_zones]
    # SKINCARE was active today → should appear as dead zone (no recent activity)
    assert "SKINCARE" in zone_ids
    # A zone never visited today should NOT appear
    assert "FRAGRANCE" not in zone_ids


@pytest.mark.asyncio
async def test_anomaly_response_structure(client, db_session):
    """All anomalies must have required fields."""
    events = [
        make_event_dict("BILLING_QUEUE_JOIN", zone_id="BILLING", queue_depth=10,
                        timestamp=NOW - timedelta(minutes=1))
    ]
    await seed_events(db_session, events)
    resp = await client.get(f"/stores/{STORE_ID}/anomalies")
    for anomaly in resp.json()["active_anomalies"]:
        assert "anomaly_id" in anomaly
        assert "anomaly_type" in anomaly
        assert "severity" in anomaly
        assert "detected_at" in anomaly
        assert "description" in anomaly
        assert "suggested_action" in anomaly
        assert anomaly["severity"] in ("INFO", "WARN", "CRITICAL")
