"""Celery uygulaması: Redis broker + Beat takvimi."""

from celery import Celery
from celery.schedules import crontab

from luminmind.config import get_settings

settings = get_settings()

app = Celery(
    "luminmind",
    broker=settings.redis_url,
    include=[
        "luminmind.workers.tasks.ingestion",
        "luminmind.workers.tasks.downsample",
        "luminmind.workers.tasks.twin",
        "luminmind.workers.tasks.forecast",
        "luminmind.workers.tasks.comparison",
        "luminmind.workers.tasks.arbitrage",
    ],
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
        # Üreticiden 5 dk aralıklarla çekim — Tescom yerel API'si anlık veriyi tazeliyor
        "schedule": crontab(minute=f"*/{settings.ingestion_interval_minutes}"),
    },
    "compute-expected-generation": {
        "task": "luminmind.compute_expected_generation",
        # saat başı +10 dk: güncel hava tahminiyle günün beklenen üretimini yeniden hesapla
        "schedule": crontab(minute=10),
    },
    "downsample-previous-day": {
        "task": "luminmind.downsample_previous_day",
        # gece 00:30 UTC: dünün lm_raw verisi → lm_hourly + lm_daily
        "schedule": crontab(minute=30, hour=0),
    },
    "detect-anomalies": {
        "task": "luminmind.detect_anomalies",
        # gece 01:00 UTC (downsample sonrası): dünün beklenen vs gerçek analizi
        "schedule": crontab(minute=0, hour=1),
    },
    "plan-arbitrage": {
        "task": "luminmind.plan_arbitrage",
        # 12:00 UTC = 15:00 TRT (GÖP sonuçları ~14:00 TRT'de açıklanır): yarının planı
        "schedule": crontab(minute=0, hour=12),
    },
    "forecast-generation": {
        "task": "luminmind.forecast_generation",
        # Günde 4 kez (00/06/12/18 UTC): yarının üretim tahminini güncel hava
        # tahminiyle tazele — GÖP teklifi ve dengeleme için gün-öncesi öngörü
        "schedule": crontab(minute=15, hour="*/6"),
    },
}
