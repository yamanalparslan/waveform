"""Celery uygulaması: Redis broker + Beat takvimi."""

from celery import Celery
from celery.schedules import crontab

from luminmind.config import get_settings

settings = get_settings()

app = Celery(
    "luminmind",
    broker=settings.redis_url,
    include=["luminmind.workers.tasks.ingestion", "luminmind.workers.tasks.downsample"],
)

app.conf.update(
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
)

app.conf.beat_schedule = {
    "ingest-all-plants": {
        "task": "luminmind.ingest_all_plants",
        # 15 dk'lık üretici verisi aralığıyla hizalı (PLAN.md Faz 1)
        "schedule": crontab(minute=f"*/{settings.ingestion_interval_minutes}"),
    },
    "downsample-previous-day": {
        "task": "luminmind.downsample_previous_day",
        # gece 00:30 UTC: dünün lm_raw verisi → lm_hourly + lm_daily
        "schedule": crontab(minute=30, hour=0),
    },
}
