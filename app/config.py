from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    DATABASE_URL: str = "sqlite+aiosqlite:///./data/store_intelligence.db"
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    LOG_LEVEL: str = "INFO"
    STALE_FEED_THRESHOLD_MINUTES: int = 10
    ANOMALY_QUEUE_SPIKE_THRESHOLD: int = 5
    ANOMALY_CONVERSION_DROP_PCT: float = 0.20
    DEAD_ZONE_MINUTES: int = 30
    DWELL_EVENT_INTERVAL_SECONDS: int = 30
    MAX_INGEST_BATCH: int = 500
    # Billing zone correlation window (seconds before POS tx counts as conversion)
    BILLING_CONVERSION_WINDOW_SEC: int = 300

    class Config:
        env_file = ".env"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
