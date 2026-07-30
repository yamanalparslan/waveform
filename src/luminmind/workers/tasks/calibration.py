"""Haftalık dijital ikiz kalibrasyon görevi.

Geçmiş penceredeki gerçek/beklenen çiftlerinden tesis bazlı ölçek ve saatlik
bias düzeltmesi öğrenilir, `twin_calibrations` tablosuna yeni bir satır yazılır.
Bir sonraki ikiz çalışması bu satırı otomatik okur.

**Arıza yutma koruması.** Açık anomali olaylarının zaman aralıkları fit'ten
çıkarılır. Aksi halde gerçek bir kayıp (kirlenen paneller, arızalı string)
"modelin fazla iyimser olduğu" şeklinde öğrenilir; model kendini gerçeğe
uydurur ve arıza görünmez hale gelir. Kalibrasyonun görevi *modeli* düzeltmek,
*santrali* aklamak değildir.
"""

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from luminmind.analytics.comparison import plant_actual_from_samples
from luminmind.config import Settings, get_settings
from luminmind.core.aggregate import RawSample
from luminmind.core.influx import InfluxStore
from luminmind.twin.calibration import CalibrationSample, CalibrationState, fit_calibration
from luminmind.workers.celery_app import app

logger = logging.getLogger(__name__)


class CalibrationSource(Protocol):
    async def query_raw_window(self, start: datetime, stop: datetime) -> list[RawSample]: ...

    async def query_twin_window(
        self, start: datetime, stop: datetime
    ) -> dict[str, dict[datetime, float]]: ...


async def open_anomaly_windows(
    engine: AsyncEngine, vendor_plant_id: str, now: datetime
) -> list[tuple[datetime, datetime]]:
    """Serinin açık/kabul edilmiş anomali aralıkları (fit'ten dışlanır)."""
    from luminmind.core.db import session_scope
    from luminmind.core.models import AnomalyEvent
    from luminmind.core.series import resolve_series_key

    async with session_scope(engine) as session:
        target = await resolve_series_key(session, vendor_plant_id)
        if target is None:
            return []
        # Arıza penceresi sahanın kendi olaylarından gelir; tesisin tamamını
        # almak bir fabrikanın arızasını diğerinin kalibrasyonundan da çıkarırdı
        scope = (
            AnomalyEvent.site_id == target.site.id
            if target.site
            else AnomalyEvent.plant_id == target.plant.id
        )
        events = (
            await session.scalars(
                select(AnomalyEvent).where(scope, AnomalyEvent.status.in_(("open", "acked")))
            )
        ).all()
        return [
            (
                _as_utc(event.started_at),
                _as_utc(event.ended_at) if event.ended_at is not None else now,
            )
            for event in events
        ]


def _as_utc(value: datetime) -> datetime:
    """Naif damgayı UTC kabul eder (SQLite tz bilgisini saklamaz, Postgres saklar)."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _excluded(ts: datetime, windows: list[tuple[datetime, datetime]]) -> bool:
    return any(start <= ts <= end for start, end in windows)


async def persist_state(engine: AsyncEngine, state: CalibrationState) -> bool:
    """Kalibrasyon durumunu yeni satır olarak yazar; yazdıysa True."""
    from luminmind.core.db import session_scope
    from luminmind.core.models import TwinCalibration
    from luminmind.core.series import resolve_series_key

    async with session_scope(engine) as session:
        target = await resolve_series_key(session, state.plant_id)
        if target is None:
            logger.warning("seri %s bulunamadı; kalibrasyon saklanmadı", state.plant_id)
            return False
        session.add(
            TwinCalibration(
                plant_id=target.plant.id,
                site_id=target.site.id if target.site else None,
                fitted_at=state.fitted_at or datetime.now(tz=UTC),
                scale=state.scale,
                soiling_base_ratio=state.soiling_base_ratio,
                hour_bias={str(h): v for h, v in state.hour_bias.items()},
                sample_count=state.sample_count,
                quality=state.quality,
                version=state.version,
            )
        )
    return True


async def current_state(engine: AsyncEngine, vendor_plant_id: str) -> CalibrationState | None:
    from luminmind.core.db import session_scope
    from luminmind.core.models import TwinCalibration
    from luminmind.core.series import resolve_series_key

    async with session_scope(engine) as session:
        target = await resolve_series_key(session, vendor_plant_id)
        if target is None:
            return None
        scope = (
            TwinCalibration.site_id == target.site.id
            if target.site
            else TwinCalibration.plant_id == target.plant.id
        )
        row = (
            await session.scalars(
                select(TwinCalibration)
                .where(scope)
                .order_by(TwinCalibration.fitted_at.desc())
                .limit(1)
            )
        ).one_or_none()
        if row is None:
            return None
        return CalibrationState(
            plant_id=vendor_plant_id,
            scale=row.scale,
            hour_bias={int(h): float(v) for h, v in (row.hour_bias or {}).items()},
            soiling_base_ratio=row.soiling_base_ratio,
            sample_count=row.sample_count,
            fitted_at=row.fitted_at,
            quality={k: float(v) for k, v in (row.quality or {}).items()},
            version=row.version,
        )


async def run_calibration(
    settings: Settings | None = None,
    until: date | None = None,
    engine: AsyncEngine | None = None,
    source: CalibrationSource | None = None,
) -> list[CalibrationState]:
    """Pencere içindeki veriden her tesis için kalibrasyon üretir ve saklar."""
    from luminmind.workers.tasks.accuracy import plant_capacities

    settings = settings or get_settings()
    if not settings.lm_twin_calibration_enabled:
        logger.info("twin calibration disabled by settings")
        return []

    end_day = until or datetime.now(tz=UTC).date()
    stop = datetime(end_day.year, end_day.month, end_day.day, tzinfo=UTC)
    start = stop - timedelta(days=settings.lm_twin_calibration_window_days)

    own_store: InfluxStore | None = None
    own_engine = engine is None
    if source is None:
        if not settings.influx_url:
            logger.warning("INFLUX_URL not configured; calibration skipped")
            return []
        own_store = InfluxStore(
            url=settings.influx_url, org=settings.influx_org, token=settings.influx_token
        )
        source = own_store
    if engine is None:
        from luminmind.core.db import create_engine

        engine = create_engine(settings.postgres_dsn)

    states: list[CalibrationState] = []
    try:
        capacities = await plant_capacities(engine)
        actual_by_plant = plant_actual_from_samples(await source.query_raw_window(start, stop))
        expected_by_plant = await source.query_twin_window(start, stop)

        for plant_id, expected in expected_by_plant.items():
            capacity = capacities.get(plant_id)
            if capacity is None:
                logger.warning("plant %s has no capacity; calibration skipped", plant_id)
                continue
            actual = actual_by_plant.get(plant_id, {})
            windows = await open_anomaly_windows(engine, plant_id, stop)
            samples = [
                CalibrationSample(ts=ts, actual_kw=actual[ts], expected_kw=expected[ts])
                for ts in sorted(actual.keys() & expected.keys())
                if not _excluded(ts, windows)
            ]
            previous = await current_state(engine, plant_id)
            state = fit_calibration(
                plant_id=plant_id,
                samples=samples,
                capacity_kw=capacity,
                previous=previous,
            )
            if state is previous or state.fitted_at is None:
                continue  # yeterli veri yoktu, durum değişmedi
            if await persist_state(engine, state):
                states.append(state)
    finally:
        if own_store is not None:
            await own_store.__aexit__(None, None, None)
        if own_engine:
            await engine.dispose()
    logger.info("calibration run complete: %d plants updated", len(states))
    return states


@app.task(name="luminmind.calibrate_twin")  # type: ignore[untyped-decorator]
def calibrate_twin() -> int:
    return len(asyncio.run(run_calibration()))
