"""Günlük anomali tespit görevi: beklenen vs gerçek → sınıflandırma → PostgreSQL.

Her gece (downsample sonrası) dünün 15 dk verisi analiz edilir. Olay yaşam
döngüsü: aynı tesis + tür için açık olay varsa güncellenir (tekilleştirme);
bulgu kaybolursa açık olaylar `resolved` yapılır.
"""

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from luminmind.analytics.classifiers import AnomalyFinding, classify_window
from luminmind.analytics.comparison import (
    build_deviation_series,
    min_expected_kw,
    plant_actual_from_samples,
)
from luminmind.config import Settings, get_settings
from luminmind.core.aggregate import RawSample
from luminmind.core.influx import InfluxStore
from luminmind.workers.celery_app import app

logger = logging.getLogger(__name__)


class ComparisonSource(Protocol):
    """Gerçek + beklenen serilerin kaynağı (InfluxStore veya test fake'i)."""

    async def query_raw_window(self, start: datetime, stop: datetime) -> list[RawSample]: ...

    async def query_twin_window(
        self, start: datetime, stop: datetime
    ) -> dict[str, dict[datetime, float]]: ...

    async def query_twin_band_window(
        self, start: datetime, stop: datetime
    ) -> dict[str, dict[datetime, tuple[float, float]]]: ...


async def apply_finding(
    engine: AsyncEngine,
    vendor_plant_id: str,
    finding: AnomalyFinding | None,
    analysis_end: datetime,
) -> bool:
    """Bulguyu olay tablosuna işler; yeni olay oluşturduysa True döndürür."""
    from luminmind.core.db import session_scope
    from luminmind.core.models import AnomalyEvent
    from luminmind.core.series import resolve_series_key

    async with session_scope(engine) as session:
        target = await resolve_series_key(session, vendor_plant_id)
        if target is None:
            logger.warning("seri %s Postgres'te bulunamadı; bulgu atlandı", vendor_plant_id)
            return False
        plant, site = target.plant, target.site
        # Tekilleştirme saha bazında: bir fabrikanın kirliliği diğerinin açık
        # olayını kapatmamalı
        scope = AnomalyEvent.site_id == site.id if site else AnomalyEvent.plant_id == plant.id
        open_events: Sequence[AnomalyEvent] = (
            await session.scalars(
                select(AnomalyEvent).where(scope, AnomalyEvent.status == "open")
            )
        ).all()

        if finding is None:
            for event in open_events:
                event.status = "resolved"
                event.ended_at = analysis_end
                logger.info("anomaly resolved plant=%s kind=%s", vendor_plant_id, event.kind)
            return False

        existing = next((e for e in open_events if e.kind == finding.kind), None)
        if existing is not None:
            existing.deviation_pct = finding.deviation_pct
            existing.severity = finding.severity
            existing.evidence = finding.evidence
            logger.info("anomaly updated plant=%s kind=%s", vendor_plant_id, finding.kind)
            return False
        session.add(
            AnomalyEvent(
                plant_id=plant.id,
                site_id=site.id if site else None,
                kind=finding.kind,
                severity=finding.severity,
                deviation_pct=finding.deviation_pct,
                started_at=finding.started_at,
                ended_at=None,
                status="open",
                evidence=finding.evidence,
            )
        )
        logger.info(
            "anomaly created plant=%s kind=%s deviation=%.1f%%",
            vendor_plant_id,
            finding.kind,
            finding.deviation_pct,
        )
        return True


async def run_comparison(
    settings: Settings | None = None,
    day: date | None = None,
    engine: AsyncEngine | None = None,
    source: ComparisonSource | None = None,
) -> int:
    """Verilen günü (varsayılan: dün) analiz eder; açık/yeni anomali sayısını döndürür."""
    settings = settings or get_settings()
    target_day = day or (datetime.now(tz=UTC).date() - timedelta(days=1))
    start = datetime(target_day.year, target_day.month, target_day.day, tzinfo=UTC)
    stop = start + timedelta(days=1)

    own_engine = engine is None
    own_store: InfluxStore | None = None
    if source is None:
        if not settings.influx_url:
            logger.warning("INFLUX_URL not configured; comparison skipped")
            return 0
        own_store = InfluxStore(
            url=settings.influx_url, org=settings.influx_org, token=settings.influx_token
        )
        source = own_store
    if engine is None:
        from luminmind.core.db import create_engine

        engine = create_engine(settings.postgres_dsn)

    anomalies = 0
    try:
        from luminmind.workers.tasks.accuracy import plant_capacities

        capacities = await plant_capacities(engine)
        raw_samples = await source.query_raw_window(start, stop)
        actual_by_plant = plant_actual_from_samples(raw_samples)
        expected_by_plant = await source.query_twin_window(start, stop)
        bands = await source.query_twin_band_window(start, stop)

        for plant_id, expected in expected_by_plant.items():
            actual = actual_by_plant.get(plant_id, {})
            samples = build_deviation_series(
                actual,
                expected,
                min_expected_kw_threshold=min_expected_kw(capacities.get(plant_id)),
                band=bands.get(plant_id),
            )
            finding = classify_window(samples)
            created = await apply_finding(engine, plant_id, finding, analysis_end=stop)
            if finding is not None:
                anomalies += 1
            logger.info(
                "comparison plant=%s samples=%d finding=%s created=%s",
                plant_id,
                len(samples),
                finding.kind if finding else "healthy",
                created,
            )
    finally:
        if own_store is not None:
            await own_store.__aexit__(None, None, None)
        if own_engine:
            await engine.dispose()
    return anomalies


@app.task(name="luminmind.detect_anomalies")  # type: ignore[untyped-decorator]
def detect_anomalies() -> int:
    return asyncio.run(run_comparison())
