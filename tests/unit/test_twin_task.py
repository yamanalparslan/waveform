from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from luminmind.config import Settings
from luminmind.core.db import session_scope
from luminmind.core.models import Base
from luminmind.core.schemas import TWIN_MODEL_VERSION
from luminmind.scripts.seed import seed
from luminmind.twin.weather import WeatherSample
from luminmind.workers.tasks.twin import load_twin_configs, run_twin

DAY = date(2026, 7, 20)


class FakeWeather:
    def __init__(self):
        self.calls = []

    async def fetch_day_15m(self, latitude, longitude, day):
        self.calls.append((latitude, longitude, day))
        base = datetime(day.year, day.month, day.day, tzinfo=UTC)
        # öğlen civarı 4 slot: yüksek ışınım
        return [
            WeatherSample(
                ts=base + timedelta(hours=10, minutes=15 * i),
                ghi_wm2=850.0,
                dni_wm2=880.0,
                dhi_wm2=110.0,
                temp_c=30.0,
                wind_ms=3.0,
            )
            for i in range(4)
        ]


class CapturingTwinSink:
    def __init__(self):
        self.points = []

    async def write_twin(self, points):
        self.points.extend(points)


async def test_run_twin_mock_mode_writes_expected_points():
    settings = Settings(lm_use_mock_vendors=True)
    sink = CapturingTwinSink()
    weather = FakeWeather()

    total = await run_twin(settings, sink=sink, weather=weather, day=DAY)

    assert total == 4 == len(sink.points)
    assert weather.calls == [(37.87, 32.48, DAY)]
    first = sink.points[0]
    assert first.plant_id == "mock-plant-1"
    assert first.model_version == TWIN_MODEL_VERSION
    assert first.expected_ac_kw > 400.0  # 1 MWp tesiste öğlen ciddi üretim beklenir


@pytest.fixture
async def seeded_engine():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with session_scope(engine) as session:
        await seed(session)
    yield engine
    await engine.dispose()


async def test_load_twin_configs_from_seeded_db(seeded_engine):
    configs = await load_twin_configs(seeded_engine)
    assert len(configs) == 1
    config = configs[0]
    assert config.plant_id == "mock-plant-1"
    assert config.latitude == 37.87
    [array] = config.arrays
    assert array.modules_per_string == 25
    assert array.strings == 73
    assert array.module_pdc0_w == 550.0
