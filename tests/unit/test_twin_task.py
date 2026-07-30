from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from luminmind.config import Settings
from luminmind.core.db import session_scope
from luminmind.core.models import Base
from luminmind.core.schemas import TWIN_MODEL_VERSION
from luminmind.scripts.seed import seed
from luminmind.twin.plant_model import MountType
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


class FakeEnsembleWeather(FakeWeather):
    """Ensemble destekleyen sağlayıcı: üyeler farklı ışınım seviyelerinde."""

    def __init__(self):
        super().__init__()
        self.range_calls = []

    async def fetch_range_15m(self, latitude, longitude, start_day, end_day, models=None):
        self.range_calls.append((latitude, longitude, start_day, end_day, tuple(models or ())))
        base = await self.fetch_day_15m(latitude, longitude, start_day)
        members = {}
        for index, model in enumerate(models or ["default"]):
            factor = 1.0 - 0.15 * index
            members[model] = [
                WeatherSample(
                    ts=s.ts,
                    ghi_wm2=s.ghi_wm2 * factor,
                    dni_wm2=s.dni_wm2 * factor,
                    dhi_wm2=s.dhi_wm2 * factor,
                    temp_c=s.temp_c,
                    wind_ms=s.wind_ms,
                )
                for s in base
            ]
        return members


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


async def test_explicit_day_computes_only_that_day():
    """Geçmişe dönük hesaplamada ileri ufuk üretilmemeli."""
    weather = FakeWeather()
    sink = CapturingTwinSink()
    await run_twin(Settings(lm_use_mock_vendors=True), sink=sink, weather=weather, day=DAY)
    assert weather.calls == [(37.87, 32.48, DAY)]
    assert {p.horizon_days for p in sink.points} == {0}


async def test_horizon_produces_tagged_forecast_days():
    weather = FakeWeather()
    sink = CapturingTwinSink()
    settings = Settings(lm_use_mock_vendors=True, lm_twin_ensemble=False)

    await run_twin(settings, sink=sink, weather=weather, horizon_days=2)

    horizons = sorted({p.horizon_days for p in sink.points})
    assert horizons == [0, 1, 2]
    days = sorted({call[2] for call in weather.calls})
    assert len(days) == 3 and days[2] - days[0] == timedelta(days=2)


async def test_ensemble_provider_yields_uncertainty_band():
    weather = FakeEnsembleWeather()
    sink = CapturingTwinSink()
    settings = Settings(
        lm_use_mock_vendors=True,
        lm_twin_ensemble=True,
        lm_twin_ensemble_models="icon_seamless,gfs_seamless,ecmwf_ifs025",
    )

    await run_twin(settings, sink=sink, weather=weather, day=DAY, configs=None)

    assert weather.range_calls, "ensemble açıkken aralık API'si kullanılmalı"
    assert weather.range_calls[0][4] == ("icon_seamless", "gfs_seamless", "ecmwf_ifs025")
    assert sink.points
    for point in sink.points:
        assert point.expected_ac_kw_p10 is not None
        assert point.expected_ac_kw_p10 <= point.expected_ac_kw <= point.expected_ac_kw_p90


async def test_ensemble_disabled_falls_back_to_single_model():
    weather = FakeEnsembleWeather()
    sink = CapturingTwinSink()
    settings = Settings(lm_use_mock_vendors=True, lm_twin_ensemble=False)

    await run_twin(settings, sink=sink, weather=weather, day=DAY)

    assert not weather.range_calls
    assert all(p.expected_ac_kw_p10 is None for p in sink.points)


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
    assert config.altitude_m == 1020.0
    # Seed'de her invertöre bir dizi bağlı (4 × 200 kW AC / ≈990 kWp DC)
    assert len(config.arrays) == 4
    array = config.arrays[0]
    assert array.modules_per_string == 25
    assert array.strings == 18
    assert array.module_pdc0_w == 550.0
    assert array.inverter_ac_kw == 200.0
    assert config.dc_capacity_kw == pytest.approx(990.0)
    assert config.ac_capacity_kw == pytest.approx(800.0 * config.losses.ac_factor)


async def test_load_twin_configs_reads_inverter_capacity_and_calibration(seeded_engine):
    """Kırpma seviyesi invertör kaydından, düzeltme kalibrasyon tablosundan gelmeli."""
    from luminmind.core.models import Plant, PvArray, TwinCalibration

    async with session_scope(seeded_engine) as session:
        plant = (await session.scalars(select(Plant))).one()
        array = (await session.scalars(select(PvArray))).first()
        array.mount_type = "single_axis_tracker"
        array.gcr = 0.35
        array.albedo = 0.25
        array.module_type = "polysi"
        session.add(
            TwinCalibration(
                plant_id=plant.id,
                fitted_at=datetime(2026, 7, 1, tzinfo=UTC),
                scale=0.93,
                hour_bias={"7": 0.88},
                sample_count=1200,
                quality={"mape_after_pct": 3.1},
            )
        )

    [config] = await load_twin_configs(seeded_engine)
    tracked = next(a for a in config.arrays if a.mount is MountType.SINGLE_AXIS_TRACKER)
    assert tracked.inverter_ac_kw == 200.0  # bağlı invertörün AC anma gücü
    assert tracked.inverter_ac_capacity_w == 200_000.0
    assert tracked.gcr == 0.35
    assert tracked.albedo == 0.25
    assert tracked.module_type == "polysi"
    assert config.calibration is not None
    assert config.calibration.scale == 0.93
    assert config.calibration.hour_bias == {7: 0.88}


async def test_unknown_mount_type_falls_back_safely(seeded_engine):
    from luminmind.core.models import PvArray

    async with session_scope(seeded_engine) as session:
        array = (await session.scalars(select(PvArray))).first()
        array.mount_type = "floating_offshore_unicorn"

    [config] = await load_twin_configs(seeded_engine)
    assert all(a.mount is MountType.FIXED_GROUND for a in config.arrays)


async def test_age_degradation_enters_the_loss_chain(seeded_engine):
    from luminmind.core.models import Plant

    async with session_scope(seeded_engine) as session:
        plant = (await session.scalars(select(Plant))).one()
        plant.commissioned_on = date(2016, 7, 20)

    [config] = await load_twin_configs(seeded_engine, today=date(2026, 7, 20))
    # 10 yıl × %0,5 = %5 bozunum
    assert config.losses.age_degradation == pytest.approx(0.05, abs=0.001)


async def test_zero_capacity_plant_is_skipped(seeded_engine):
    """Kapasite 0 'sıfır güçlü santral' değil, 'kapasite girilmemiş' demektir.

    Sahte bir tek modüllük dizi türetmek o tesiste beklenen üretimi ~0 yapar;
    gerçek üretim karşısında sapma sonsuza gider ve hem anomali motoru hem
    doğruluk skoru anlamsızlaşır.
    """
    from luminmind.core.models import Plant

    async with session_scope(seeded_engine) as session:
        plant = (await session.scalars(select(Plant))).one()
        for array in await plant.awaitable_attrs.pv_arrays:
            await session.delete(array)
        plant.dc_capacity_kwp = 0.0

    assert await load_twin_configs(seeded_engine) == []

    # Kapasite girilince ikiz yeniden devreye girer
    async with session_scope(seeded_engine) as session:
        plant = (await session.scalars(select(Plant))).one()
        plant.dc_capacity_kwp = 250.0
    [config] = await load_twin_configs(seeded_engine)
    assert config.dc_capacity_kw > 200.0


async def test_missing_capacity_is_skipped(seeded_engine):
    from luminmind.core.models import Plant

    async with session_scope(seeded_engine) as session:
        plant = (await session.scalars(select(Plant))).one()
        for array in await plant.awaitable_attrs.pv_arrays:
            await session.delete(array)
        plant.dc_capacity_kwp = None

    assert await load_twin_configs(seeded_engine) == []
