"""Tests for /heatmap and /health endpoints."""
import pytest
import uuid
from datetime import datetime, timezone, timedelta

from tests.conftest import (
    STORE_ID, seed_events, seed_pos, make_event_dict
)


# ── Heatmap tests ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_heatmap_empty_store(client):
    resp = await client.get(f"/stores/{STORE_ID}/heatmap")
    assert resp.status_code == 200
    body = resp.json()
    assert body["store_id"] == STORE_ID
    assert isinstance(body["zones"], list)
    assert len(body["zones"]) == 0


@pytest.mark.asyncio
async def test_heatmap_zones_present(client, db_session):
    events = [
        make_event_dict("ZONE_ENTER", zone_id="SKINCARE", dwell_ms=0),
        make_event_dict("ZONE_ENTER", zone_id="SKINCARE", dwell_ms=0),
        make_event_dict("ZONE_ENTER", zone_id="FRAGRANCE", dwell_ms=0),
        make_event_dict("ZONE_EXIT", zone_id="SKINCARE", dwell_ms=15000),
    ]
    await seed_events(db_session, events)
    resp = await client.get(f"/stores/{STORE_ID}/heatmap")
    assert resp.status_code == 200
    zones = resp.json()["zones"]
    zone_ids = [z["zone_id"] for z in zones]
    assert "SKINCARE" in zone_ids
    assert "FRAGRANCE" in zone_ids


@pytest.mark.asyncio
async def test_heatmap_top_zone_has_score_100(client, db_session):
    events = [make_event_dict("ZONE_ENTER", zone_id="SKINCARE") for _ in range(5)]
    events += [make_event_dict("ZONE_ENTER", zone_id="FRAGRANCE") for _ in range(2)]
    await seed_events(db_session, events)
    resp = await client.get(f"/stores/{STORE_ID}/heatmap")
    zones = resp.json()["zones"]
    assert zones[0]["normalized_score"] == 100.0


@pytest.mark.asyncio
async def test_heatmap_excludes_staff(client, db_session):
    events = [
        make_event_dict("ZONE_ENTER", zone_id="SKINCARE", is_staff=False),
        make_event_dict("ZONE_ENTER", zone_id="STAFF_ONLY", is_staff=True),
    ]
    await seed_events(db_session, events)
    resp = await client.get(f"/stores/{STORE_ID}/heatmap")
    zone_ids = [z["zone_id"] for z in resp.json()["zones"]]
    assert "SKINCARE" in zone_ids
    assert "STAFF_ONLY" not in zone_ids


@pytest.mark.asyncio
async def test_heatmap_dwell_ms_in_response(client, db_session):
    events = [
        make_event_dict("ZONE_ENTER", zone_id="SKINCARE", dwell_ms=30000),
        make_event_dict("ZONE_ENTER", zone_id="SKINCARE", dwell_ms=10000),
    ]
    await seed_events(db_session, events)
    resp = await client.get(f"/stores/{STORE_ID}/heatmap")
    zones = {z["zone_id"]: z for z in resp.json()["zones"]}
    assert zones["SKINCARE"]["avg_dwell_ms"] == 20000.0


@pytest.mark.asyncio
async def test_heatmap_response_schema(client, db_session):
    events = [make_event_dict("ZONE_ENTER", zone_id="HAIRCARE")]
    await seed_events(db_session, events)
    resp = await client.get(f"/stores/{STORE_ID}/heatmap")
    zone = resp.json()["zones"][0]
    assert "zone_id" in zone
    assert "visit_frequency" in zone
    assert "avg_dwell_ms" in zone
    assert "normalized_score" in zone
    assert "data_confidence" in zone


# ── Health tests ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health_empty_db(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in ("healthy", "degraded")
    assert body["database"] == "ok"
    assert "total_events_ingested" in body


@pytest.mark.asyncio
async def test_health_with_events(client, db_session):
    events = [make_event_dict("ENTRY") for _ in range(5)]
    await seed_events(db_session, events)
    resp = await client.get("/health")
    body = resp.json()
    assert body["total_events_ingested"] >= 5


@pytest.mark.asyncio
async def test_health_stale_feed_warning(client, db_session):
    old_ts = datetime.now(timezone.utc) - timedelta(hours=6)
    events = [make_event_dict("ENTRY", timestamp=old_ts)]
    await seed_events(db_session, events)
    resp = await client.get("/health")
    body = resp.json()
    store_statuses = [s["status"] for s in body.get("stores", [])]
    assert "STALE_FEED" in store_statuses


@pytest.mark.asyncio
async def test_health_ok_feed_with_recent_events(client, db_session):
    events = [make_event_dict("ENTRY")]
    await seed_events(db_session, events)
    resp = await client.get("/health")
    body = resp.json()
    store = next((s for s in body["stores"] if s["store_id"] == STORE_ID), None)
    assert store is not None
    assert store["status"] == "OK"


@pytest.mark.asyncio
async def test_health_version_field(client):
    resp = await client.get("/health")
    assert resp.json()["version"] == "1.0.0"
