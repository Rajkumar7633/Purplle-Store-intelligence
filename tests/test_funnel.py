# PROMPT: "Write pytest tests for a session-based retail conversion funnel endpoint.
# The funnel has stages: Entry → Zone Visit → Billing Queue → Purchase.
# Key requirements: session is the unit (not raw events), re-entries must NOT
# double-count a visitor, staff must be excluded, drop_off_pct must be correct,
# and zero-purchase stores should still return valid funnel structure."
#
# CHANGES MADE:
# - AI suggested testing only happy path; added all-staff clip edge case
# - Added assertion that funnel counts are monotonically non-increasing
# - Replaced float equality with pytest.approx for drop_off_pct

import pytest
from datetime import timedelta
from tests.conftest import make_event_dict, seed_events, seed_pos, STORE_ID, NOW


@pytest.mark.asyncio
async def test_funnel_empty_store(client):
    resp = await client.get(f"/stores/{STORE_ID}/funnel")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_sessions"] == 0
    assert len(body["stages"]) == 4
    for stage in body["stages"]:
        assert stage["count"] == 0


@pytest.mark.asyncio
async def test_funnel_stages_monotonically_decreasing(client, db_session):
    """Funnel counts must never increase from one stage to the next."""
    events = [
        make_event_dict("ENTRY", visitor_id="VIS_a"),
        make_event_dict("ENTRY", visitor_id="VIS_b"),
        make_event_dict("ENTRY", visitor_id="VIS_c"),
        make_event_dict("ZONE_ENTER", visitor_id="VIS_a", zone_id="SKINCARE"),
        make_event_dict("ZONE_ENTER", visitor_id="VIS_b", zone_id="HAIRCARE"),
        make_event_dict("BILLING_QUEUE_JOIN", visitor_id="VIS_a", zone_id="BILLING", queue_depth=1),
    ]
    await seed_events(db_session, events)
    await seed_pos(db_session, STORE_ID, [NOW])

    resp = await client.get(f"/stores/{STORE_ID}/funnel")
    stages = resp.json()["stages"]
    counts = [s["count"] for s in stages]
    for i in range(1, len(counts)):
        assert counts[i] <= counts[i - 1], f"Stage {i} count {counts[i]} > stage {i-1} count {counts[i-1]}"


@pytest.mark.asyncio
async def test_funnel_reentry_not_double_counted(client, db_session):
    """Visitor re-entering must count as 1 session in funnel, not 2."""
    events = [
        make_event_dict("ENTRY", visitor_id="VIS_repeat"),
        make_event_dict("EXIT", visitor_id="VIS_repeat"),
        make_event_dict("REENTRY", visitor_id="VIS_repeat"),
        # ENTRY event already recorded from first visit
        make_event_dict("ZONE_ENTER", visitor_id="VIS_repeat", zone_id="SKINCARE"),
    ]
    await seed_events(db_session, events)
    resp = await client.get(f"/stores/{STORE_ID}/funnel")
    body = resp.json()
    assert body["total_sessions"] == 1
    entry_stage = next(s for s in body["stages"] if s["stage"] == "Entry")
    assert entry_stage["count"] == 1


@pytest.mark.asyncio
async def test_funnel_excludes_staff(client, db_session):
    events = [
        make_event_dict("ENTRY", visitor_id="VIS_customer"),
        make_event_dict("ENTRY", visitor_id="VIS_staff_01", is_staff=True),
        make_event_dict("ZONE_ENTER", visitor_id="VIS_staff_01", zone_id="BILLING", is_staff=True),
    ]
    await seed_events(db_session, events)
    resp = await client.get(f"/stores/{STORE_ID}/funnel")
    body = resp.json()
    assert body["total_sessions"] == 1


@pytest.mark.asyncio
async def test_funnel_all_staff_clip(client, db_session):
    """Edge case: clip with only staff — funnel must return zeros, not crash."""
    events = [make_event_dict("ENTRY", is_staff=True) for _ in range(5)]
    await seed_events(db_session, events)
    resp = await client.get(f"/stores/{STORE_ID}/funnel")
    body = resp.json()
    assert body["total_sessions"] == 0


@pytest.mark.asyncio
async def test_funnel_drop_off_pct_first_stage_zero(client, db_session):
    events = [make_event_dict("ENTRY", visitor_id=f"VIS_{i:03d}") for i in range(10)]
    await seed_events(db_session, events)
    resp = await client.get(f"/stores/{STORE_ID}/funnel")
    entry_stage = resp.json()["stages"][0]
    assert entry_stage["drop_off_pct"] == pytest.approx(0.0)
    assert entry_stage["count"] == 10


@pytest.mark.asyncio
async def test_funnel_drop_off_pct_computation(client, db_session):
    """5 entered, 2 visited zones → 60% drop-off at zone visit stage."""
    visitor_ids = [f"VIS_{i:03d}" for i in range(5)]
    events = [make_event_dict("ENTRY", visitor_id=vid) for vid in visitor_ids]
    events += [
        make_event_dict("ZONE_ENTER", visitor_id="VIS_000", zone_id="SKINCARE"),
        make_event_dict("ZONE_ENTER", visitor_id="VIS_001", zone_id="HAIRCARE"),
    ]
    await seed_events(db_session, events)
    resp = await client.get(f"/stores/{STORE_ID}/funnel")
    stages = resp.json()["stages"]
    zone_stage = next(s for s in stages if s["stage"] == "Zone Visit")
    assert zone_stage["count"] == 2
    assert zone_stage["drop_off_pct"] == pytest.approx(60.0)
