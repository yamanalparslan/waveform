"""Saatlik dijital ikiz görevi: hava verisi çek → beklenen üretimi hesapla → Influx'a yaz.

Günün tamamı (00:00–24:00 UTC, 15 dk çözünürlük) her çalışmada yeniden hesaplanır;
aynı seri + zaman damgasına yazım üzerine yazma olduğundan görev idempotenttir ve
gün içinde güncellenen hava tahminini otomatik yansıtır.

Tesis konfigürasyonu: mock modda mock tesis sabitleri; gerçek modda Postgres
`plants` + `pv_arrays` tablolarından yüklenir (dizi kaydı yoksa kapasiteden
jenerik dizi türetilir).
"""

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from luminmind.config import Settings, get_settings
from luminmind.core.influx import InfluxStore
from luminmind.core.schemas import TwinPoint
from luminmind.twin.expected import TwinPlantConfig, expected_generation, weather_to_frame
from luminmind.twin.plant_model import ArrayConfig, default_array_for_capacity
from luminmind.twin.weather import OpenMeteoClient, WeatherProvider
from luminmind.workers.celery_app import app

logger = logging.getLogger(__name__)

# Mock tesis (adapters/mock.py ve scripts/seed.py ile aynı değerler)
_MOCK_TWIN_CONFIG = TwinPlantConfig(
    plant_id="mock-plant-1",
    latitude=37.87,
    longitude=32.48,
    arrays=[default_array_for_capacity(1000.0)],
)


class TwinSink(Protocol):
    async def write_twin(self, points: Sequence[TwinPoint]) -> None: ...


class LogTwinSink:
    async def write_twin(self, points: Sequence[TwinPoint]) -> None:
        for point in points:
            logger.debug("twin %s", point.model_dump_json(exclude_none=True))


async def load_twin_configs(engine: AsyncEngine) -> list[TwinPlantConfig]:
    """Postgres'ten tesis + dizi konfigürasyonlarını okur (koordinatsız tesisler atlanır)."""
    from luminmind.core.db import session_scope
    from luminmind.core.models import Plant

    configs: list[TwinPlantConfig] = []
    async with session_scope(engine) as session:
        plants = (await session.scalars(select(Plant))).all()
        for plant in plants:
            if plant.latitude is None or plant.longitude is None:
                logger.warning("plant %s has no coordinates; twin skipped", plant.vendor_plant_id)
                continue
            pv_arrays = await plant.awaitable_attrs.pv_arrays
            arrays = [
                ArrayConfig(
                    tilt_deg=a.tilt_deg,
                    azimuth_deg=a.azimuth_deg,
                    modules_per_string=a.modules_per_string,
                    strings=a.strings,
                    module_pdc0_w=float(a.module_params.get("pdc0", 550.0)),
                    gamma_pdc=float(a.module_params.get("gamma_pdc", -0.0035)),
                )
                for a in pv_arrays
            ]
            if not arrays:
                if plant.dc_capacity_kwp is None:
                    logger.warning(
                        "plant %s has no arrays nor capacity; twin skipped",
                        plant.vendor_plant_id,
                    )
                    continue
                arrays = [default_array_for_capacity(plant.dc_capacity_kwp)]
            configs.append(
                TwinPlantConfig(
                    plant_id=plant.vendor_plant_id,
                    latitude=plant.latitude,
                    longitude=plant.longitude,
                    arrays=arrays,
                )
            )
    return configs


async def _resolve_configs(settings: Settings) -> list[TwinPlantConfig]:
    if settings.lm_use_mock_vendors:
        return [_MOCK_TWIN_CONFIG]
    from luminmind.core.db import create_engine

    engine = create_engine(settings.postgres_dsn)
    try:
        return await load_twin_configs(engine)
    finally:
        await engine.dispose()


async def run_twin(
    settings: Settings | None = None,
    sink: TwinSink | None = None,
    weather: WeatherProvider | None = None,
    day: date | None = None,
    configs: list[TwinPlantConfig] | None = None,
) -> int:
    """Tüm tesisler için günün beklenen üretimini hesaplar; yazılan nokta sayısını döndürür."""
    settings = settings or get_settings()
    target_day = day or datetime.now(tz=UTC).date()
    configs = configs if configs is not None else await _resolve_configs(settings)

    own_weather = weather is None
    weather_client = weather or OpenMeteoClient()
    total = 0
    try:
        all_points: list[TwinPoint] = []
        for config in configs:
            samples = await weather_client.fetch_day_5m(
                config.latitude, config.longitude, target_day
            )
            points = expected_generation(config, weather_to_frame(samples))
            all_points.extend(points)
            logger.info(
                "twin computed plant=%s day=%s points=%d",
                config.plant_id,
                target_day.isoformat(),
                len(points),
            )
        if sink is not None:
            await sink.write_twin(all_points)
        elif settings.influx_url:
            async with InfluxStore(
                url=settings.influx_url, org=settings.influx_org, token=settings.influx_token
            ) as store:
                await store.write_twin(all_points)
        else:
            await LogTwinSink().write_twin(all_points)
        total = len(all_points)
    finally:
        if own_weather and isinstance(weather_client, OpenMeteoClient):
            await weather_client.aclose()
    logger.info("twin run complete: %d points", total)
    return total


@app.task(name="luminmind.compute_expected_generation")  # type: ignore[untyped-decorator]
def compute_expected_generation() -> int:
    return asyncio.run(run_twin())
