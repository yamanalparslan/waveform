"""Dijital ikiz doğrulaması: pvlib clear-sky referansına karşı deterministik testler."""

from datetime import UTC, datetime

import pandas as pd
import pytest
from pvlib.location import Location

from luminmind.twin.components import LossChain
from luminmind.twin.expected import TwinPlantConfig, expected_generation, weather_to_frame
from luminmind.twin.plant_model import default_array_for_capacity
from luminmind.twin.weather import WeatherSample

KONYA = (37.87, 32.48)
DAY = datetime(2026, 7, 20, tzinfo=UTC)


def clearsky_weather() -> pd.DataFrame:
    """Konya için açık gökyüzü ışınımından 15 dk'lık hava DataFrame'i üretir."""
    times = pd.date_range(DAY, DAY + pd.Timedelta(days=1), freq="15min", tz="UTC")[:-1]
    location = Location(*KONYA, tz="UTC")
    clearsky = location.get_clearsky(times, model="simplified_solis")
    frame = clearsky.rename(columns={})[["ghi", "dni", "dhi"]].copy()
    frame["temp_air"] = 28.0
    frame["wind_speed"] = 2.0
    return frame


@pytest.fixture(scope="module")
def config() -> TwinPlantConfig:
    return TwinPlantConfig(
        plant_id="p1",
        latitude=KONYA[0],
        longitude=KONYA[1],
        arrays=[default_array_for_capacity(1000.0)],
    )


@pytest.fixture(scope="module")
def points(config):
    return expected_generation(config, clearsky_weather())


def test_night_output_is_zero(points):
    night = [p for p in points if p.ts.hour < 2 or p.ts.hour >= 20]
    assert night, "gece noktaları olmalı"
    assert all(p.expected_ac_kw == 0.0 for p in night)


def test_clear_sky_peak_is_plausible_for_1mw(points):
    peak = max(p.expected_ac_kw for p in points)
    # 1 MWp tesis, açık yaz günü: sıcaklık + verim + kayıp zinciriyle 650–1000 kW bandı
    assert 650.0 <= peak <= 1000.0


def test_generation_rises_towards_solar_noon(points):
    by_hour = {p.ts.hour: p.expected_ac_kw for p in points if p.ts.minute == 0}
    # Konya güneş öğlesi ~09:50 UTC; sabah saatleri monoton artmalı
    assert by_hour[5] < by_hour[7] < by_hour[9]


def test_poa_and_cell_temp_populated_at_noon(points):
    noon = next(p for p in points if p.ts.hour == 10 and p.ts.minute == 0)
    assert noon.poa_irradiance_wm2 is not None and noon.poa_irradiance_wm2 > 500
    assert noon.cell_temp_c is not None and noon.cell_temp_c > 28.0  # hücre > ortam


def test_loss_chain_scales_output(config):
    weather = clearsky_weather()
    lossless = TwinPlantConfig(
        plant_id="p1",
        latitude=config.latitude,
        longitude=config.longitude,
        arrays=config.arrays,
        losses=LossChain(cable_loss=0.0, transformer_loss=0.0),
    )
    peak_with_losses = max(p.expected_ac_kw for p in expected_generation(config, weather))
    peak_lossless = max(p.expected_ac_kw for p in expected_generation(lossless, weather))
    assert peak_with_losses == pytest.approx(peak_lossless * LossChain().net_factor, rel=1e-6)


def test_empty_weather_returns_no_points(config):
    assert expected_generation(config, weather_to_frame([])) == []


def test_weather_to_frame_columns():
    sample = WeatherSample(
        ts=datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
        ghi_wm2=800.0,
        dni_wm2=850.0,
        dhi_wm2=100.0,
        temp_c=30.0,
        wind_ms=3.0,
    )
    frame = weather_to_frame([sample])
    assert list(frame.columns) == ["ghi", "dni", "dhi", "temp_air", "wind_speed"]
    assert frame.index.tz is not None


def test_default_array_matches_capacity():
    array = default_array_for_capacity(1000.0)
    assert array.dc_capacity_w == pytest.approx(1_000_000, rel=0.02)
