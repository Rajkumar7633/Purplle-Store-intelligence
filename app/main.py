"""
Store Intelligence API — FastAPI entrypoint.
All endpoints are production-aware with structured logging, idempotency, and graceful degradation.
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import httpx
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db, init_db
from app.logging_config import RequestLoggingMiddleware, setup_logging
from app.models import (
    IngestRequest, IngestResponse,
    MetricsResponse, FunnelResponse, HeatmapResponse,
    AnomaliesResponse, HealthResponse,
)
from app.ingestion import ingest_events
from app.metrics import get_store_metrics
from app.funnel import get_store_funnel
from app.heatmap import get_store_heatmap
from app.anomalies import get_store_anomalies
from app.health import get_health

settings = get_settings()
logger = logging.getLogger(__name__)


async def keep_alive_task(app_url: str) -> None:
    """Background task to keep the service warm on Render (prevent spin-down after 15 min inactivity)."""
    await asyncio.sleep(60)  # Wait 60 seconds for service to fully start
    while True:
        try:
            await asyncio.sleep(240)  # Ping every 4 minutes (well under 15-min Render timeout)
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"{app_url}/health")
                if response.status_code == 200:
                    logger.debug("Keep-alive ping successful")
        except Exception as exc:
            logger.warning("Keep-alive ping failed (may be in local dev): %s", type(exc).__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator:
    setup_logging(settings.LOG_LEVEL)
    await init_db()
    logger.info("Store Intelligence API started")
    
    # Start keep-alive task if running on Render (has RENDER env var)
    import os
    keep_alive_handle = None
    if os.getenv("RENDER"):
        app_url = os.getenv("APP_URL", "http://localhost:8000")
        logger.info("Render detected. Starting keep-alive task for %s", app_url)
        keep_alive_handle = asyncio.create_task(keep_alive_task(app_url))
    
    yield
    
    # Cancel keep-alive task on shutdown
    if keep_alive_handle:
        keep_alive_handle.cancel()
        try:
            await keep_alive_handle
        except asyncio.CancelledError:
            pass
    
    logger.info("Store Intelligence API shutting down")


app = FastAPI(
    title="Store Intelligence API",
    description="Real-time retail analytics from CCTV event streams",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Error handlers ────────────────────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    logger.error("Unhandled exception: %s", exc, exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"error": "internal_server_error", "message": "An unexpected error occurred"},
    )


# ── Event Ingestion ───────────────────────────────────────────────────────────

@app.post(
    "/events/ingest",
    response_model=IngestResponse,
    status_code=status.HTTP_200_OK,
    summary="Ingest a batch of up to 500 events (idempotent by event_id)",
)
async def ingest(
    payload: IngestRequest,
    db: AsyncSession = Depends(get_db),
) -> IngestResponse:
    if len(payload.events) > settings.MAX_INGEST_BATCH:
        raise HTTPException(
            status_code=422,
            detail=f"Batch exceeds maximum of {settings.MAX_INGEST_BATCH} events",
        )
    return await ingest_events(list(payload.events), db)


# ── Store Metrics ─────────────────────────────────────────────────────────────

@app.get(
    "/stores/{store_id}/metrics",
    response_model=MetricsResponse,
    summary="Real-time store metrics: visitors, conversion, dwell, queue",
)
async def metrics(
    store_id: str,
    db: AsyncSession = Depends(get_db),
) -> MetricsResponse:
    return await get_store_metrics(store_id, db)


# ── Conversion Funnel ─────────────────────────────────────────────────────────

@app.get(
    "/stores/{store_id}/funnel",
    response_model=FunnelResponse,
    summary="Session-based conversion funnel with drop-off percentages",
)
async def funnel(
    store_id: str,
    db: AsyncSession = Depends(get_db),
) -> FunnelResponse:
    return await get_store_funnel(store_id, db)


# ── Zone Heatmap ──────────────────────────────────────────────────────────────

@app.get(
    "/stores/{store_id}/heatmap",
    response_model=HeatmapResponse,
    summary="Zone visit frequency and dwell, normalised 0-100",
)
async def heatmap(
    store_id: str,
    db: AsyncSession = Depends(get_db),
) -> HeatmapResponse:
    return await get_store_heatmap(store_id, db)


# ── Anomalies ─────────────────────────────────────────────────────────────────

@app.get(
    "/stores/{store_id}/anomalies",
    response_model=AnomaliesResponse,
    summary="Active operational anomalies: queue spike, conversion drop, dead zone",
)
async def anomalies(
    store_id: str,
    db: AsyncSession = Depends(get_db),
) -> AnomaliesResponse:
    return await get_store_anomalies(store_id, db)


# ── Health ────────────────────────────────────────────────────────────────────

@app.get(
    "/health",
    response_model=HealthResponse,
    summary="Service health: DB status, per-store feed lag, STALE_FEED warnings",
)
async def health(
    db: AsyncSession = Depends(get_db),
) -> HealthResponse:
    return await get_health(db)


# ── POS Transaction Ingest (for pipeline correlation) ─────────────────────────

from pydantic import BaseModel
from typing import List as PList


class POSTransaction(BaseModel):
    store_id: str
    transaction_id: str
    timestamp: str
    basket_value_inr: float


class POSIngestRequest(BaseModel):
    transactions: PList[POSTransaction]


@app.post(
    "/pos/ingest",
    summary="Ingest POS transactions for conversion rate correlation",
)
async def ingest_pos(
    payload: POSIngestRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.database import POSTransactionORM
    from sqlalchemy import select
    from datetime import datetime

    accepted = 0
    duplicates = 0
    for txn in payload.transactions:
        existing = await db.execute(
            select(POSTransactionORM.id).where(
                POSTransactionORM.transaction_id == txn.transaction_id
            )
        )
        if existing.fetchone():
            duplicates += 1
            continue
        ts = datetime.fromisoformat(txn.timestamp.replace("Z", "+00:00"))
        db.add(POSTransactionORM(
            store_id=txn.store_id,
            transaction_id=txn.transaction_id,
            timestamp=ts.replace(tzinfo=None),
            basket_value_inr=txn.basket_value_inr,
        ))
        accepted += 1

    if accepted > 0:
        await db.flush()

    return {"accepted": accepted, "duplicates": duplicates}
