"""Gece downsampling görevi: lm_raw → lm_hourly → lm_daily.

Her gece 00:30 UTC'de bir önceki günün 15 dk'lık verisini okur, saatlik ve
günlük agregatları hesaplayıp yazar. Aynı seri + zaman damgasına yazım Influx'ta
üzerine yazma olduğundan görev idempotenttir — gün içinde tekrar çalıştırılabilir.
"""

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta

from luminmind.config import Settings, get_settings
from luminmind.core.aggregate import aggregate_daily, aggregate_hourly
from luminmind.core.influx import InfluxStore
from luminmind.workers.celery_app import app

logger = logging.getLogger(__name__)


async def run_downsample(day: date | None = None, settings: Settings | None = None) -> int:
    """Verilen günü (varsayılan: dün, UTC) downsample eder; yazılan agregat sayısını döndürür."""
    settings = settings or get_settings()
    if not settings.influx_url:
        logger.warning("INFLUX_URL not configured; downsample skipped")
        return 0
    target_day = day or (datetime.now(tz=UTC).date() - timedelta(days=1))
    start = datetime(target_day.year, target_day.month, target_day.day, tzinfo=UTC)
    stop = start + timedelta(days=1)

    async with InfluxStore(
        url=settings.influx_url, org=settings.influx_org, token=settings.influx_token
    ) as store:
        samples = await store.query_raw_window(start, stop)
        hourly = aggregate_hourly(samples)
        daily = aggregate_daily(hourly)
        await store.write_hourly(hourly)
        await store.write_daily(daily)

    logger.info(
        "downsample %s: %d raw -> %d hourly, %d daily",
        target_day.isoformat(),
        len(samples),
        len(hourly),
        len(daily),
    )
    return len(hourly) + len(daily)


@app.task(name="luminmind.downsample_previous_day")  # type: ignore[untyped-decorator]
def downsample_previous_day() -> int:
    return asyncio.run(run_downsample())
