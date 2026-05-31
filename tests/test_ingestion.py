# PROMPT: "Write comprehensive pytest tests for a FastAPI event ingestion endpoint.
# The endpoint POST /events/ingest accepts up to 500 events, is idempotent by event_id,
# returns partial success on malformed events, and deduplicates within a batch.
# Test: happy path, duplicate handling, intra-batch dedup, oversized batch, malformed events,
# idempotency (calling twice returns same accepted/duplicate counts), staff flag passthrough,
# zero-event batch."
#
# CHANGES MADE:
# - Used conftest fixtures (client, db_session, seed_events) instead of raw httpx
# - Added edge case: all-staff batch should still be accepted (not filtered at ingest)
# - Replaced generic assert with specific count assertions for audit trail
# - Added test for batch at exactly MAX_INGEST_BATCH boundary

import uuid
import pytest
from tests.conftest import make_event_dict, seed_events, STORE_ID


@pytest.mark.asyncio
async def test_ingest_happy_path(client):
    events = [make_event_dict() for _ in range(5)]
    resp = await client.post("/events/ingest", json={"events": events})
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] == 5
    assert body["duplicates"] == 0
    assert body["invalid"] == 0


@pytest.mark.asyncio
async def test_ingest_idempotent_second_call(client):
    events = [make_event_dict() for _ in range(3)]
    r1 = await client.post("/events/ingest", json={"events": events})
    r2 = await client.post("/events/ingest", json={"events": events})
    assert r1.json()["accepted"] == 3
    assert r2.json()["duplicates"] == 3
    assert r2.json()["accepted"] == 0


@pytest.mark.asyncio
async def test_ingest_intra_batch_dedup(client):
    """Same event_id appearing twice in one batch → only accepted once."""
    event = make_event_dict()
    resp = await client.post("/events/ingest", json={"events": [event, event]})
    body = resp.json()
    assert body["accepted"] == 1
    assert body["duplicates"] == 1


@pytest.mark.asyncio
async def test_ingest_partial_success_on_invalid(client):
    valid = make_event_dict()
    invalid = {**make_event_dict(), "event_id": "not-a-uuid"}
    resp = await client.post("/events/ingest", json={"events": [valid, invalid]})
    body = resp.json()
    assert body["accepted"] == 1
    assert body["invalid"] == 1


@pytest.mark.asyncio
async def test_ingest_oversized_batch_rejected(client):
    events = [make_event_dict() for _ in range(501)]
    resp = await client.post("/events/ingest", json={"events": events})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_ingest_staff_events_accepted(client):
    """Staff events are accepted at ingest — filtering happens at metrics layer."""
    events = [make_event_dict(is_staff=True) for _ in range(3)]
    resp = await client.post("/events/ingest", json={"events": events})
    assert resp.json()["accepted"] == 3


@pytest.mark.asyncio
async def test_ingest_zero_events(client):
    resp = await client.post("/events/ingest", json={"events": []})
    body = resp.json()
    assert body["accepted"] == 0


@pytest.mark.asyncio
async def test_ingest_all_event_types(client):
    types = ["ENTRY", "EXIT", "ZONE_ENTER", "ZONE_EXIT", "ZONE_DWELL",
             "BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON", "REENTRY"]
    events = [make_event_dict(event_type=t) for t in types]
    resp = await client.post("/events/ingest", json={"events": events})
    assert resp.json()["accepted"] == len(types)


@pytest.mark.asyncio
async def test_ingest_batch_at_limit(client):
    events = [make_event_dict() for _ in range(500)]
    resp = await client.post("/events/ingest", json={"events": events})
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 500
