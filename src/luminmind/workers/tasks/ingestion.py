"""15 dakikalık ingestion görevi.

Akış: adaptörden (mock veya gerçek) veri çek → normalize et → InfluxDB `lm_raw`
bucket'ına yaz. `INFLUX_URL` boşsa (Influx'sız dev/test modu) noktalar yalnızca
loglanır; akışın geri kalanı aynıdır.
"""

import asyncio
import logging
from collections.abc import Mapping, Sequence
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
    if settings.tescom_base_url:
        from luminmind.adapters import TescomAdapter
        from luminmind.adapters.tescom import FactorySite

        adapters.append(
            TescomAdapter(
                base_url=settings.tescom_base_url,
                api_key=settings.tescom_api_key,
                plant_id=settings.tescom_plant_id,
                plant_name=settings.tescom_plant_name,
                latitude=settings.tescom_latitude,
                longitude=settings.tescom_longitude,
                dc_capacity_kwp=settings.tescom_dc_capacity_kwp or None,
                timezone=settings.tescom_timezone,
                factories={
                    factory_id: FactorySite(plant_id=key, name=name, dc_capacity_kwp=kwp)
                    for factory_id, (key, name, kwp) in settings.tescom_factory_sites.items()
                },
            )
        )
    return adapters


async def ingest_adapter(
    adapter: VendorAdapter,
    since: datetime,
    since_by_plant: "Mapping[str, datetime] | None" = None,
) -> list[TelemetryPoint]:
    """Tek adaptörün tüm tesislerini çeker; tesis bazlı hatalar diğerlerini engellemez.

    `since_by_plant` verilirse her tesis kendi son kaydından devam eder — bir
    tesisin çekimi kesilmişken diğerlerini gereksiz yere geriye sarmamak için.
    """
    collected: list[TelemetryPoint] = []
    async with adapter:
        plants = await adapter.fetch_plants()
        for plant in plants:
            plant_since = (since_by_plant or {}).get(plant.vendor_plant_id, since)
            try:
                points = await adapter.fetch_telemetry(
                    plant.vendor_plant_id, since=plant_since
                )
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


async def _resume_points(
    sink: TelemetrySink, settings: Settings, default_since: datetime
) -> dict[str, datetime]:
    """Seri anahtarı → çekimin devam edeceği an (deponun son kaydı).

    Depoya sorulamıyorsa (LogSink, test fake'i) boş döner; çağıran varsayılan
    pencereye düşer. Geriye sarma `ingestion_backfill_max_hours` ile sınırlı:
    uzun bir duruştan sonra tek çevrimde onbinlerce nokta çekilmesin.
    """
    reader = getattr(sink, "last_sample_ts", None)
    if reader is None:
        return {}
    horizon = datetime.now(tz=UTC) - timedelta(
        hours=settings.ingestion_backfill_max_hours
    )
    try:
        latest = await reader(lookback_hours=settings.ingestion_backfill_max_hours)
    except Exception:
        logger.exception("last sample lookup failed; falling back to fixed window")
        return {}
    return {
        key: max(ts, horizon)
        for key, ts in latest.items()
        if max(ts, horizon) < default_since
    }


async def _ingest_to(sink: TelemetrySink, settings: Settings) -> int:
    since = datetime.now(tz=UTC) - timedelta(minutes=settings.ingestion_interval_minutes)
    # Kaçırılan aralık: "şimdi eksi bir çevrim" varsayımı, çekim durduğunda
    # (host uykusu, yeniden başlatma) aradaki veriyi bir daha hiç istemiyordu.
    since_by_plant = await _resume_points(sink, settings, since)
    if since_by_plant:
        logger.info("resuming ingestion from last stored sample: %s", since_by_plant)
    total = 0
    all_points: list[TelemetryPoint] = []
    for adapter in build_adapters(settings):
        points = await ingest_adapter(adapter, since=since, since_by_plant=since_by_plant)
        await sink.write_telemetry(points)
        total += len(points)
        all_points.extend(points)
    if all_points:
        try:
            await sync_inverter_health(all_points, settings)
        except Exception:
            logger.exception("inverter health sync failed")
    logger.info("ingestion run complete: %d points", total)
    return total


async def sync_inverter_health(
    points: list[TelemetryPoint], settings: Settings
) -> None:
    """Cihaz durum cache'ini günceller ve sağlık uyarılarını yazar (kural motoru)."""
    from sqlalchemy import select

    from luminmind.analytics.inverter_health import (
        apply_findings,
        evaluate_inverter,
        latest_snapshots,
        upsert_inverter_state,
    )
    from luminmind.core.db import create_engine, session_scope
    from luminmind.core.models import Inverter, Plant

    snapshots = latest_snapshots(points)
    if not snapshots:
        return
    engine = create_engine(settings.postgres_dsn)
    try:
        async with session_scope(engine) as session:
            await upsert_inverter_state(session, snapshots.values())
        # ikinci oturum: durum yazıldıktan sonra kuralları değerlendir
        async with session_scope(engine) as session:
            now = datetime.now(tz=UTC)
            plants = (await session.scalars(select(Plant))).all()
            for plant in plants:
                inverters = (
                    await session.scalars(
                        select(Inverter).where(Inverter.plant_id == plant.id)
                    )
                ).all()
                findings = []
                for inv in inverters:
                    findings.extend(evaluate_inverter(inv, now))
                created, resolved = await apply_findings(session, plant.id, findings, now)
                if created or resolved:
                    logger.info(
                        "inverter health plant=%s created=%d resolved=%d",
                        plant.vendor_plant_id,
                        created,
                        resolved,
                    )
    finally:
        await engine.dispose()


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
