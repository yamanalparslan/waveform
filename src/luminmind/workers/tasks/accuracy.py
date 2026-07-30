"""Günlük tahmin doğruluğu skorlama görevi.

Her gece (downsample ve anomali analizinden sonra) dünün beklenen serisi
gerçekle karşılaştırılır ve `twin_accuracy` ölçümüne yazılır. Referans olarak
"persistence" (bir önceki günün gerçek üretimi) kullanılır: fizik modeli bu
naif referansı yenemiyorsa (skill ≤ 0) modelde yapısal bir sorun vardır.

Bu görev olmadan model geliştirmenin doğrulaması yoktur — bir değişikliğin
tahmini iyileştirip iyileştirmediği ancak buradaki seriye bakarak söylenebilir.
"""

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncEngine

from luminmind.analytics.accuracy import (
    AccuracyScore,
    align_series,
    persistence_reference,
    score_day,
)
from luminmind.analytics.comparison import plant_actual_from_samples
from luminmind.config import Settings, get_settings
from luminmind.core.aggregate import RawSample
from luminmind.core.influx import InfluxStore
from luminmind.core.schemas import TWIN_MODEL_VERSION
from luminmind.workers.celery_app import app

logger = logging.getLogger(__name__)

_SLOT_HOURS = 0.25


class AccuracySource(Protocol):
    """Skorlamanın okuduğu zaman serisi kaynağı."""

    async def query_raw_window(self, start: datetime, stop: datetime) -> list[RawSample]: ...

    async def query_twin_window(
        self, start: datetime, stop: datetime
    ) -> dict[str, dict[datetime, float]]: ...

    async def query_twin_band_window(
        self, start: datetime, stop: datetime
    ) -> dict[str, dict[datetime, tuple[float, float]]]: ...


class AccuracySink(Protocol):
    async def write_accuracy(self, scores: Sequence[AccuracyScore]) -> None: ...


async def plant_capacities(engine: AsyncEngine) -> dict[str, float]:
    """Seri anahtarı → normalizasyon kapasitesi (kW).

    Anahtar artık sahanın seri anahtarıdır; nMAE/nRMSE kapasiteye normalize
    edildiği için 400 kWp'lik Üretim ile 250 kWp'lik Mekanik fabrikanın aynı
    kapasiteyle ölçülmesi ikisinin de skorunu bozardı.
    """
    from luminmind.core.db import session_scope
    from luminmind.core.series import series_capacities

    async with session_scope(engine) as session:
        return await series_capacities(session)


async def run_accuracy(
    settings: Settings | None = None,
    day: date | None = None,
    engine: AsyncEngine | None = None,
    source: AccuracySource | None = None,
    sink: AccuracySink | None = None,
    capacities: dict[str, float] | None = None,
) -> list[AccuracyScore]:
    """Verilen günü (varsayılan: dün) skorlar ve yazar; skor listesini döndürür."""
    settings = settings or get_settings()
    target_day = day or (datetime.now(tz=UTC).date() - timedelta(days=1))
    start = datetime(target_day.year, target_day.month, target_day.day, tzinfo=UTC)
    stop = start + timedelta(days=1)

    own_store: InfluxStore | None = None
    own_engine = engine is None
    if source is None or sink is None:
        if not settings.influx_url:
            logger.warning("INFLUX_URL not configured; accuracy scoring skipped")
            return []
        own_store = InfluxStore(
            url=settings.influx_url, org=settings.influx_org, token=settings.influx_token
        )
        source = source or own_store
        sink = sink or own_store

    scores: list[AccuracyScore] = []
    try:
        if capacities is None:
            if engine is None:
                from luminmind.core.db import create_engine

                engine = create_engine(settings.postgres_dsn)
            capacities = await plant_capacities(engine)

        actual_by_plant = plant_actual_from_samples(await source.query_raw_window(start, stop))
        previous_actual = plant_actual_from_samples(
            await source.query_raw_window(start - timedelta(days=1), start)
        )
        expected_by_plant = await source.query_twin_window(start, stop)
        bands = await source.query_twin_band_window(start, stop)

        for plant_id, expected in expected_by_plant.items():
            actual = actual_by_plant.get(plant_id, {})
            capacity = capacities.get(plant_id)
            if capacity is None:
                logger.warning("plant %s has no capacity; accuracy skipped", plant_id)
                continue
            pairs = align_series(
                actual,
                expected,
                reference=persistence_reference(previous_actual.get(plant_id, {})),
                band=bands.get(plant_id),
            )
            score = score_day(
                plant_id=plant_id,
                day=target_day,
                pairs=pairs,
                capacity_kw=capacity,
                model_version=TWIN_MODEL_VERSION,
                interval_hours=_SLOT_HOURS,
            )
            if score is None:
                logger.info("accuracy skipped plant=%s: not enough paired samples", plant_id)
                continue
            scores.append(score)
            logger.info(
                "accuracy plant=%s day=%s nMAE=%.2f%% nRMSE=%.2f%% bias=%.2f%% skill=%s",
                plant_id,
                target_day.isoformat(),
                score.nmae_pct,
                score.nrmse_pct,
                score.nmbe_pct,
                score.skill_vs_reference,
            )

        await sink.write_accuracy(scores)
    finally:
        if own_store is not None:
            await own_store.__aexit__(None, None, None)
        if own_engine and engine is not None:
            await engine.dispose()
    return scores


@app.task(name="luminmind.score_twin_accuracy")  # type: ignore[untyped-decorator]
def score_twin_accuracy() -> int:
    return len(asyncio.run(run_accuracy()))
