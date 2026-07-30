"""Dijital ikiz doğrulaması: pvlib clear-sky referansına karşı deterministik testler."""

import dataclasses
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from pvlib.location import Location

from luminmind.twin.calibration import CalibrationState
from luminmind.twin.components import LossChain
from luminmind.twin.expected import (
    TwinPlantConfig,
    expected_generation,
    expected_generation_ensemble,
    weather_to_frame,
)
from luminmind.twin.pipeline import effective_times
from luminmind.twin.plant_model import ArrayConfig, MountType, default_array_for_capacity
from luminmind.twin.soiling import SoilingConfig
from luminmind.twin.weather import IrradianceStamp, WeatherSample

KONYA = (37.87, 32.48)
DAY = datetime(2026, 7, 20, tzinfo=UTC)


def clearsky_weather(days: int = 1, scale: float = 1.0, precip_mm: float | None = None):
    """Konya için açık gökyüzü ışınımından 15 dk'lık hava DataFrame'i üretir."""
    times = pd.date_range(DAY, DAY + pd.Timedelta(days=days), freq="15min", tz="UTC")[:-1]
    location = Location(*KONYA, tz="UTC")
    clearsky = location.get_clearsky(times, model="simplified_solis")
    frame = clearsky[["ghi", "dni", "dhi"]].copy() * scale
    frame["temp_air"] = 28.0
    frame["wind_speed"] = 2.0
    frame["relative_humidity"] = 35.0
    frame["pressure"] = 1010.0
    frame["precipitation"] = float("nan") if precip_mm is None else precip_mm
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


def test_loss_chain_scales_output_when_inverter_does_not_clip(config):
    # Kırpmasız rejimde (bol AC kapasitesi) kayıplar çıktıyı orantılı ölçekler
    roomy = dataclasses.replace(config.arrays[0], dc_ac_ratio=0.8)
    weather = clearsky_weather()
    lossy = TwinPlantConfig(
        plant_id="p1", latitude=KONYA[0], longitude=KONYA[1], arrays=[roomy]
    )
    lossless = TwinPlantConfig(
        plant_id="p1",
        latitude=KONYA[0],
        longitude=KONYA[1],
        arrays=[roomy],
        losses=LossChain(
            soiling=0.0,
            mismatch=0.0,
            dc_wiring=0.0,
            connections=0.0,
            light_induced_degradation=0.0,
            nameplate=0.0,
            ac_wiring=0.0,
            transformer=0.0,
        ),
    )
    peak_lossy = max(p.expected_ac_kw for p in expected_generation(lossy, weather))
    peak_lossless = max(p.expected_ac_kw for p in expected_generation(lossless, weather))
    assert peak_lossy == pytest.approx(peak_lossless * LossChain().net_factor, rel=1e-3)


def test_inverter_clips_at_ac_nameplate():
    """DC/AC oranı arttıkça öğlen tepesi invertör anma gücüne dayanmalı.

    Eski modelde invertörün DC girdi limiti tüm dizi kapasitesine eşitti; kırpma
    hiç oluşmuyor, gerçek santral kırparken ikiz kırpmadığı için her açık günde
    sahte "eksik üretim" raporlanıyordu.
    """
    weather = clearsky_weather()
    peaks = {}
    clipped = {}
    for ratio in (1.0, 1.3, 1.6):
        array = dataclasses.replace(default_array_for_capacity(1000.0), dc_ac_ratio=ratio)
        cfg = TwinPlantConfig("p1", KONYA[0], KONYA[1], arrays=[array])
        generated = expected_generation(cfg, weather)
        peaks[ratio] = max(p.expected_ac_kw for p in generated)
        clipped[ratio] = sum(p.clipping_loss_kw for p in generated)

    assert peaks[1.6] < peaks[1.3] < peaks[1.0]
    assert clipped[1.6] > clipped[1.3] > 0.0
    # Tepe, AC anma gücünü (AC kayıpları düşülmüş) aşamaz
    ac_limit = 1000.0 / 1.6 * LossChain().ac_factor
    assert peaks[1.6] <= ac_limit * 1.01


def test_explicit_inverter_capacity_overrides_ratio():
    array = dataclasses.replace(default_array_for_capacity(1000.0), inverter_ac_kw=500.0)
    cfg = TwinPlantConfig("p1", KONYA[0], KONYA[1], arrays=[array])
    peak = max(p.expected_ac_kw for p in expected_generation(cfg, clearsky_weather()))
    assert peak <= 500.0 * LossChain().ac_factor * 1.01
    assert peak > 450.0  # kırpma var ama üretim de var


def test_row_spacing_drives_self_shading_loss():
    """Sıralar sıklaştıkça (GCR ↑) sıra-arası gölgelenme kaybı artmalı.

    Kış gündönümünde güneş alçak olduğu için etki en belirgindir. Eski
    ModelChain tabanlı model bu kaybı hiç görmüyor, arazi santrallerinde
    sistematik olarak fazla üretim tahmin ediyordu.
    """
    times = pd.date_range("2026-12-21", periods=96, freq="15min", tz="UTC")
    clearsky = Location(*KONYA, tz="UTC").get_clearsky(times, model="simplified_solis")
    winter = clearsky[["ghi", "dni", "dhi"]].copy()
    winter["temp_air"] = 8.0
    winter["wind_speed"] = 2.0
    winter["relative_humidity"] = 60.0
    winter["pressure"] = 1015.0
    winter["precipitation"] = float("nan")

    energy = {}
    for gcr in (0.2, 0.5, 0.8):
        # Kırpmayı devre dışı bırak ki fark yalnızca gölgelenmeden gelsin
        array = dataclasses.replace(
            default_array_for_capacity(1000.0, tilt_deg=30.0), gcr=gcr, dc_ac_ratio=0.8
        )
        cfg = TwinPlantConfig("p1", KONYA[0], KONYA[1], arrays=[array])
        energy[gcr] = sum(p.expected_ac_kw for p in expected_generation(cfg, winter)) / 4

    assert energy[0.8] < energy[0.5] < energy[0.2]
    assert energy[0.8] < energy[0.2] * 0.85  # sıkı dizilimde kayıp belirgin


def test_rooftop_mount_skips_row_shading():
    """Çatıda tek düzlem varsayımı geçerli: GCR çıktıyı değiştirmemeli."""
    weather = clearsky_weather()
    energies = []
    for gcr in (0.2, 0.8):
        array = dataclasses.replace(
            default_array_for_capacity(1000.0), mount=MountType.ROOFTOP, gcr=gcr
        )
        cfg = TwinPlantConfig("p1", KONYA[0], KONYA[1], arrays=[array])
        energies.append(sum(p.expected_ac_kw for p in expected_generation(cfg, weather)))
    assert energies[0] == pytest.approx(energies[1], rel=1e-9)


def test_missing_ghi_is_not_reported_as_zero(config):
    """Eksik ışınım 'beklenen 0 kW' değil, 'bilinmiyor' demektir."""
    weather = clearsky_weather()
    gap = weather.index[40:44]
    weather.loc[gap, "ghi"] = float("nan")
    generated = expected_generation(config, weather)
    assert len(generated) == len(weather) - len(gap)
    assert not any(p.ts in gap for p in generated)


def test_inconsistent_dni_dhi_is_rederived_from_ghi(config):
    """DNI/DHI eksikse GHI'dan Erbs ile türetilir; üretim makul kalır."""
    weather = clearsky_weather()
    weather["dni"] = float("nan")
    weather["dhi"] = float("nan")
    peak = max(p.expected_ac_kw for p in expected_generation(config, weather))
    reference = max(p.expected_ac_kw for p in expected_generation(config, clearsky_weather()))
    assert peak == pytest.approx(reference, rel=0.10)


def test_interval_mean_stamp_shifts_solar_geometry():
    index = pd.date_range(DAY, periods=4, freq="15min", tz="UTC")
    interval = timedelta(minutes=15)
    end = effective_times(index, IrradianceStamp.INTERVAL_END, interval)
    start = effective_times(index, IrradianceStamp.INTERVAL_START, interval)
    instant = effective_times(index, IrradianceStamp.INSTANT, interval)
    assert (end == index - interval / 2).all()
    assert (start == index + interval / 2).all()
    assert (instant == index).all()


def test_soiling_accumulates_when_dry_and_resets_with_rain():
    cfg = TwinPlantConfig(
        "p1", KONYA[0], KONYA[1], arrays=[default_array_for_capacity(1000.0)],
        soiling=SoilingConfig(),
    )
    dry = expected_generation(cfg, clearsky_weather(days=15, precip_mm=0.0))
    assert dry[0].soiling_ratio == pytest.approx(1.0, abs=1e-3)
    assert dry[-1].soiling_ratio is not None and dry[-1].soiling_ratio < 0.99

    rainy_weather = clearsky_weather(days=15, precip_mm=0.0)
    rainy_weather.iloc[96 * 10 : 96 * 10 + 48, rainy_weather.columns.get_loc("precipitation")] = 1.0
    rainy = expected_generation(cfg, rainy_weather)
    assert rainy[-1].soiling_ratio > dry[-1].soiling_ratio  # yağış temizledi


def test_no_precipitation_data_keeps_base_ratio():
    cfg = TwinPlantConfig(
        "p1", KONYA[0], KONYA[1], arrays=[default_array_for_capacity(1000.0)],
        soiling=SoilingConfig(base_ratio=0.95),
    )
    generated = expected_generation(cfg, clearsky_weather(days=5))  # precipitation = NaN
    assert all(p.soiling_ratio == pytest.approx(0.95) for p in generated)


def test_calibration_scales_expected_output(config):
    weather = clearsky_weather()
    calibrated = dataclasses.replace(
        config, calibration=CalibrationState(plant_id="p1", scale=0.90)
    )
    base_peak = max(p.expected_ac_kw for p in expected_generation(config, weather))
    tuned_peak = max(p.expected_ac_kw for p in expected_generation(calibrated, weather))
    assert tuned_peak == pytest.approx(base_peak * 0.90, rel=1e-3)


def test_hour_bias_applies_only_to_its_hour(config):
    weather = clearsky_weather()
    calibrated = dataclasses.replace(
        config, calibration=CalibrationState(plant_id="p1", hour_bias={7: 0.80})
    )
    base = {p.ts: p.expected_ac_kw for p in expected_generation(config, weather)}
    tuned = {p.ts: p.expected_ac_kw for p in expected_generation(calibrated, weather)}
    at_seven = [ts for ts in base if ts.hour == 7]
    at_nine = [ts for ts in base if ts.hour == 9]
    # Nokta değerleri 3 haneye yuvarlanır; tolerans buna göre
    assert all(tuned[ts] == pytest.approx(base[ts] * 0.80, abs=0.01) for ts in at_seven)
    assert all(tuned[ts] == pytest.approx(base[ts], abs=0.01) for ts in at_nine)


def test_ensemble_produces_bracketing_band(config):
    members = {name: clearsky_weather(scale=s) for name, s in
               (("a", 1.0), ("b", 0.85), ("c", 1.05), ("d", 0.70))}
    generated = expected_generation_ensemble(config, members, horizon_days=1)
    noon = next(p for p in generated if p.ts.hour == 10 and p.ts.minute == 0)
    assert noon.horizon_days == 1
    assert noon.expected_ac_kw_p10 is not None and noon.expected_ac_kw_p90 is not None
    assert noon.expected_ac_kw_p10 <= noon.expected_ac_kw <= noon.expected_ac_kw_p90
    assert noon.uncertainty_kw is not None and noon.uncertainty_kw > 0


def test_single_member_ensemble_has_no_false_band(config):
    generated = expected_generation_ensemble(config, {"only": clearsky_weather()})
    assert all(p.expected_ac_kw_p10 is None for p in generated)


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
    assert list(frame.columns) == [
        "ghi",
        "dni",
        "dhi",
        "temp_air",
        "wind_speed",
        "relative_humidity",
        "precipitation",
        "pressure",
    ]
    assert frame.index.tz is not None


def test_default_array_matches_capacity():
    array = default_array_for_capacity(1000.0)
    assert array.dc_capacity_w == pytest.approx(1_000_000, rel=0.02)
    # AC anma gücü verilmediğinde DC/AC oranından türetilir (kırpma gerçekçi olur)
    assert array.inverter_ac_capacity_w == pytest.approx(array.dc_capacity_w / 1.2)


def test_array_config_rejects_impossible_geometry():
    with pytest.raises(ValueError, match="gcr"):
        ArrayConfig(tilt_deg=25, azimuth_deg=180, modules_per_string=20, strings=5, gcr=0.0)
    with pytest.raises(ValueError, match="albedo"):
        ArrayConfig(tilt_deg=25, azimuth_deg=180, modules_per_string=20, strings=5, albedo=1.5)
