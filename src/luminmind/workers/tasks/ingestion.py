"""15 dakikalık ingestion görevi.

Faz 1 dikey dilimi: adaptörden (mock veya gerçek) veri çek, normalize et ve
yapılandırılmış log satırı olarak yaz. Faz 2'de log yerine InfluxDB `lm_raw`
bucket'ına yazım bağlanacak; akışın geri kalanı değişmeyecek.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from luminmind.adapters import MockAdapter, VendorAdapter
from luminmind.config import Settings, get_settings
from luminmind.core.schemas import TelemetryPoint
from luminmind.workers.celery_app import app

logger = logging.getLogger(__name__)


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


async def run_ingestion(settings: Settings | None = None) -> int:
    """Tüm adaptörleri çalıştırır; toplanan nokta sayısını döndürür."""
    settings = settings or get_settings()
    since = datetime.now(tz=UTC) - timedelta(minutes=settings.ingestion_interval_minutes)
    total = 0
    for adapter in build_adapters(settings):
        points = await ingest_adapter(adapter, since=since)
        # Faz 2: burada InfluxDB lm_raw yazımı yapılacak
        for point in points:
            logger.debug("point %s", point.model_dump_json(exclude_none=True))
        total += len(points)
    logger.info("ingestion run complete: %d points", total)
    return total


@app.task(name="luminmind.ingest_all_plants")  # type: ignore[untyped-decorator]  # celery dekoratörü tipsiz
def ingest_all_plants() -> int:
    return asyncio.run(run_ingestion())
