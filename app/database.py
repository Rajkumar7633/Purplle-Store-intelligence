"""
SQLAlchemy async database setup with SQLite + aiosqlite.
Tables: events, pos_transactions, anomaly_log
"""
import os
from datetime import datetime
from typing import AsyncGenerator

from sqlalchemy import (
    Column, String, Integer, Float, Boolean,
    DateTime, Text, Index, UniqueConstraint,
    text,
)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

settings = get_settings()

# Ensure data directory exists
os.makedirs(os.path.dirname(settings.DATABASE_URL.replace("sqlite+aiosqlite:///", "")), exist_ok=True)

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False},
)

AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


class EventORM(Base):
    __tablename__ = "events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(String(36), nullable=False)
    store_id = Column(String(50), nullable=False)
    camera_id = Column(String(50), nullable=False)
    visitor_id = Column(String(30), nullable=False)
    event_type = Column(String(30), nullable=False)
    timestamp = Column(DateTime, nullable=False)
    zone_id = Column(String(50), nullable=True)
    dwell_ms = Column(Integer, default=0)
    is_staff = Column(Boolean, default=False)
    confidence = Column(Float, nullable=False)
    queue_depth = Column(Integer, nullable=True)
    sku_zone = Column(String(50), nullable=True)
    session_seq = Column(Integer, nullable=True)
    ingested_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("event_id", name="uq_event_id"),
        Index("ix_events_store_ts", "store_id", "timestamp"),
        Index("ix_events_visitor", "visitor_id"),
        Index("ix_events_type", "event_type"),
    )


class POSTransactionORM(Base):
    __tablename__ = "pos_transactions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    store_id = Column(String(50), nullable=False)
    transaction_id = Column(String(50), nullable=False)
    timestamp = Column(DateTime, nullable=False)
    basket_value_inr = Column(Float, nullable=False)

    __table_args__ = (
        UniqueConstraint("transaction_id", name="uq_txn_id"),
        Index("ix_pos_store_ts", "store_id", "timestamp"),
    )


class AnomalyLogORM(Base):
    __tablename__ = "anomaly_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    anomaly_id = Column(String(36), nullable=False)
    anomaly_type = Column(String(40), nullable=False)
    severity = Column(String(10), nullable=False)
    store_id = Column(String(50), nullable=False)
    detected_at = Column(DateTime, nullable=False)
    resolved_at = Column(DateTime, nullable=True)
    description = Column(Text, nullable=False)
    suggested_action = Column(Text, nullable=False)
    context_json = Column(Text, nullable=True)

    __table_args__ = (
        Index("ix_anomaly_store", "store_id", "detected_at"),
    )


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def check_db_health() -> bool:
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
