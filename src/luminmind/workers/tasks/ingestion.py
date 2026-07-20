"""15 dakikalık ingestion görevi.

Akış: adaptörden (mock veya gerçek) veri çek → normalize et → InfluxDB `lm_raw`
bucket'ına yaz. `INFLUX_URL` boşsa (Influx'sız dev/test modu) noktalar yalnızca
loglanır; akışın geri kalanı aynıdır.
"""

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol

from luminmind.adapters import MockAdapter, VendorAdapter
from luminmind.config import Settings, get_settings
from luminmind.core.influx import InfluxStore
from luminmind.core.schemas import TelemetryPoint
from luminmind.workers.celery_app import app

logger = logging.getLogger(__name__)


class TelemetrySink(Protocol):
    """Normalize edilmiş noktaların yazıldığı hedef (Influx veya test fake'i)."""

    async def write_telemetry(self, points: Sequence[TelemetryPoint]) -> None: ...


class LogSink:
    """Influx yapılandırılmamışken noktaları loglayan varsayılan hedef."""

    async def write_telemetry(self, points: Sequence[TelemetryPoint]) -> None:
        for point in points:
            logger.debug("point %s", point.model_dump_json(exclude_none=True))


def build_adapters(settings: Settings) -> list[VendorAdapter]:
    """Konfigürasyona göre aktif adaptörleri kurar.

    Mock modda tek MockAdapter döner. Gerçek kimlik bilgileri girildiğinde
    Huawei/SMA adaptörleri buraya eklenir (PLAN.md Faz 2: kimlik bilgileri
    PostgreSQL VENDOR_CREDENTIALS tablosundan okunacak).
    """
    if settings.lm_use_mock_vendors:
        return [MockAdapter()]
    adapters: list[VendorAdapter] = []
    if settings.huawei_base_url:
        from luminmind.adapters import HuaweiAdapter

        adapters.append(
            HuaweiAdapter(
                base_url=settings.huawei_base_url,
                username=settings.huawei_username,
                system_code=settings.huawei_system_code,
            )
        )
    if settings.sma_base_url:
        from luminmind.adapters import SmaAdapter

        adapters.append(
            SmaAdapter(
                base_url=settings.sma_base_url,
                client_id=settings.sma_client_id,
                client_secret=settings.sma_client_secret,
            )
        )
    return adapters


async def ingest_adapter(adapter: VendorAdapter, since: datetime) -> list[TelemetryPoint]:
    """Tek adaptörün tüm tesislerini çeker; tesis bazlı hatalar diğerlerini engellemez."""
    collected: list[TelemetryPoint] = []
    async with adapter:
        plants = await adapter.fetch_plants()
        for plant in plants:
            try:
                points = await adapter.fetch_telemetry(plant.vendor_plant_id, since=since)
            except Exception:
                logger.exception(
                    "telemetry fetch failed vendor=%s plant=%s",
                    adapter.vendor,
                    plant.vendor_plant_id,
                )
                continue
            collected.extend(points)
            logger.info(
                "ingested vendor=%s plant=%s points=%d",
                adapter.vendor,
                plant.vendor_plant_id,
                len(points),
            )
    return collected


async def _ingest_to(sink: TelemetrySink, settings: Settings) -> int:
    since = datetime.now(tz=UTC) - timedelta(minutes=settings.ingestion_interval_minutes)
    total = 0
    for adapter in build_adapters(settings):
        points = await ingest_adapter(adapter, since=since)
        await sink.write_telemetry(points)
        total += len(points)
    logger.info("ingestion run complete: %d points", total)
    return total


async def run_ingestion(
    settings: Settings | None = None, sink: TelemetrySink | None = None
) -> int:
    """Tüm adaptörleri çalıştırıp noktaları hedefe yazar; nokta sayısını döndürür."""
    settings = settings or get_settings()
    if sink is not None:
        return await _ingest_to(sink, settings)
    if settings.influx_url:
        async with InfluxStore(
            url=settings.influx_url, org=settings.influx_org, token=settings.influx_token
        ) as store:
            return await _ingest_to(store, settings)
    return await _ingest_to(LogSink(), settings)


@app.task(name="luminmind.ingest_all_plants")  # type: ignore[untyped-decorator]  # celery dekoratörü tipsiz
def ingest_all_plants() -> int:
    return asyncio.run(run_ingestion())
