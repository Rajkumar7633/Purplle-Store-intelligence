"""
Shared fixtures for all test modules.
"""
import asyncio
import uuid
from datetime import datetime, timezone, timedelta
from typing import AsyncGenerator, List

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from app.main import app
from app.database import Base, get_db, EventORM, POSTransactionORM
from app.models import StoreEvent, EventType, EventMetadata

# ── In-memory SQLite for tests ────────────────────────────────────────────────
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

test_engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False, class_=AsyncSession)


@pytest_asyncio.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(autouse=True)
async def create_test_tables():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    async with TestSessionLocal() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    async def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac
    app.dependency_overrides.clear()


# ── Helper factories ──────────────────────────────────────────────────────────

STORE_ID = "STORE_BLR_002"
NOW = datetime.now(timezone.utc)


def make_event_dict(
    event_type: str = "ENTRY",
    visitor_id: str = None,
    store_id: str = STORE_ID,
    zone_id: str = None,
    is_staff: bool = False,
    dwell_ms: int = 0,
    confidence: float = 0.92,
    timestamp: datetime = None,
    queue_depth: int = None,
) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": "CAM_ENTRY_01",
        "visitor_id": visitor_id or f"VIS_{uuid.uuid4().hex[:6]}",
        "event_type": event_type,
        "timestamp": (timestamp or NOW).isoformat(),
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": confidence,
        "metadata": {
            "queue_depth": queue_depth,
            "sku_zone": zone_id,
            "session_seq": 1,
        },
    }


def make_store_event(**kwargs) -> StoreEvent:
    d = make_event_dict(**kwargs)
    return StoreEvent(**d)


async def seed_events(db: AsyncSession, events: List[dict]) -> None:
    for e in events:
        ts = datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00"))
        db.add(EventORM(
            event_id=e["event_id"],
            store_id=e["store_id"],
            camera_id=e["camera_id"],
            visitor_id=e["visitor_id"],
            event_type=e["event_type"],
            timestamp=ts.replace(tzinfo=None),
            zone_id=e.get("zone_id"),
            dwell_ms=e.get("dwell_ms", 0),
            is_staff=e.get("is_staff", False),
            confidence=e.get("confidence", 0.9),
            queue_depth=e.get("metadata", {}).get("queue_depth"),
            sku_zone=e.get("metadata", {}).get("sku_zone"),
            session_seq=e.get("metadata", {}).get("session_seq"),
        ))
    await db.flush()


async def seed_pos(db: AsyncSession, store_id: str, timestamps: List[datetime]) -> None:
    for i, ts in enumerate(timestamps):
        db.add(POSTransactionORM(
            store_id=store_id,
            transaction_id=f"TXN_{uuid.uuid4().hex[:8]}",
            timestamp=ts.replace(tzinfo=None),
            basket_value_inr=1200.0,
        ))
    await db.flush()
