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
from luminmind.core.schemas import PlantMeta, TelemetryPoint
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
            )
        )
    return adapters


async def ingest_adapter(
    adapter: VendorAdapter, since: datetime
) -> tuple[list[TelemetryPoint], list[PlantMeta]]:
    """Tek adaptörün tüm tesislerini çeker; tesis bazlı hatalar diğerlerini engellemez.

    Ölçüm noktalarının yanında keşfedilen tesis meta verilerini de döndürür
    (Postgres'e otomatik senkron için — üretici yeni bir tesis/fabrika ekleyince
    LuminMind'da otomatik belirir).
    """
    collected: list[TelemetryPoint] = []
    discovered: list[PlantMeta] = []
    async with adapter:
        plants = await adapter.fetch_plants()
        discovered.extend(plants)
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
    return collected, discovered


async def _ingest_to(sink: TelemetrySink, settings: Settings) -> int:
    since = datetime.now(tz=UTC) - timedelta(minutes=settings.ingestion_interval_minutes)
    total = 0
    all_points: list[TelemetryPoint] = []
    all_plants: list[PlantMeta] = []
    for adapter in build_adapters(settings):
        points, plants = await ingest_adapter(adapter, since=since)
        await sink.write_telemetry(points)
        total += len(points)
        all_points.extend(points)
        all_plants.extend(plants)
    if all_plants:
        try:
            await upsert_discovered_plants(all_plants, settings)
        except Exception:
            logger.exception("plant upsert failed")
    if all_points:
        try:
            await sync_inverter_health(all_points, settings)
        except Exception:
            logger.exception("inverter health sync failed")
    logger.info("ingestion run complete: %d points", total)
    return total


async def upsert_discovered_plants(
    plant_metas: list[PlantMeta], settings: Settings
) -> None:
    """Üreticiden keşfedilen tesisleri Postgres'e ekler (yeni fabrika keşfi için).

    Idempotent: mevcut (vendor, vendor_plant_id) satırı varsa dokunmaz. Bir sahibi
    olması gerektiği için ilk admin kullanıcıya atanır.
    """
    from sqlalchemy import select

    from luminmind.core.db import create_engine, session_scope
    from luminmind.core.models import Plant, User

    engine = create_engine(settings.postgres_dsn)
    try:
        async with session_scope(engine) as session:
            admin = (
                await session.scalars(select(User).where(User.role == "admin").limit(1))
            ).one_or_none()
            if admin is None:
                return  # ilk seed henüz atılmadıysa atla
            for pm in plant_metas:
                existing = (
                    await session.scalars(
                        select(Plant).where(
                            Plant.vendor == pm.vendor.value,
                            Plant.vendor_plant_id == pm.vendor_plant_id,
                        )
                    )
                ).one_or_none()
                if existing is not None:
                    continue
                session.add(
                    Plant(
                        owner_id=admin.id,
                        name=pm.name,
                        vendor=pm.vendor.value,
                        vendor_plant_id=pm.vendor_plant_id,
                        latitude=pm.latitude,
                        longitude=pm.longitude,
                        dc_capacity_kwp=pm.dc_capacity_kwp,
                    )
                )
                logger.info(
                    "auto-created plant vendor=%s plant_id=%s name=%s",
                    pm.vendor.value, pm.vendor_plant_id, pm.name,
                )
    finally:
        await engine.dispose()


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
