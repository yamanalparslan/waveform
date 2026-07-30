from datetime import UTC, datetime, timedelta

from luminmind.core.aggregate import DailyAggregate, HourlyAggregate
from luminmind.core.influx import (
    BUCKET_DAILY,
    BUCKET_HOURLY,
    BUCKET_RAW,
    daily_to_point,
    hourly_to_point,
    plant_series_flux,
    telemetry_to_point,
    twin_to_point,
)
from luminmind.core.schemas import TelemetryPoint, TwinPoint, Vendor

TS = datetime(2026, 7, 20, 7, 0, tzinfo=UTC)


# ------------------------------ seri sorgusu ------------------------------
# Ham kova indirgenmezse çağıranlar 5 dakikalık noktaları 15 dakikalık sanıp
# enerjiyi üçe katlıyor (canlıda 400 kWp sahada 648 kWh/saat gözlendi) ve
# "tesis toplamı" tek cihazın anlık gücü olarak kalıyor.


def _flux(resolution: str) -> str:
    return plant_series_flux("tescom-izmir-uretim", "ac_power_kw", TS, TS + timedelta(days=1),
                             resolution)


def test_raw_series_is_downsampled_to_the_analysis_grid():
    flux = _flux("15m")
    assert BUCKET_RAW in flux
    assert "aggregateWindow(every: 15m, fn: mean" in flux


def test_downsampled_stamp_is_the_window_start():
    """İkiz aralık başına yazıyor; pencere sonu damgası hizalamayı tümden kaçırır."""
    assert 'timeSrc: "_start"' in _flux("15m")


def test_empty_windows_are_not_invented():
    """createEmpty gece boyunca sıfır üretir; bu, veri yokluğunu 0 kW'a çevirirdi."""
    assert "createEmpty: false" in _flux("15m")


def test_preaggregated_buckets_are_not_downsampled_again():
    for resolution, bucket in (("1h", BUCKET_HOURLY), ("1d", BUCKET_DAILY)):
        flux = _flux(resolution)
        assert bucket in flux
        assert "aggregateWindow" not in flux


def test_series_query_filters_by_plant_and_field():
    flux = _flux("15m")
    assert 'r.plant_id == "tescom-izmir-uretim"' in flux
    assert 'r._field == "ac_power_kw"' in flux
    # Cihaz etiketi filtrelenmez: ortalama cihaz başına alınır, toplama çağıranda
    assert "inverter_id" not in flux


def test_telemetry_line_protocol():
    point = TelemetryPoint(
        vendor=Vendor.MOCK,
        vendor_plant_id="p1",
        vendor_device_id="inv-01",
        ts=TS,
        ac_power_kw=120.5,
        temp_c=40.0,
    )
    line = telemetry_to_point(point).to_line_protocol()
    assert line.startswith("pv_telemetry,")
    assert "plant_id=p1" in line
    assert "inverter_id=inv-01" in line
    assert "vendor=mock" in line
    assert "ac_power_kw=120.5" in line
    assert "temp_c=40" in line
    assert "dc_power_kw" not in line  # None alanlar yazılmaz
    assert line.endswith(str(int(TS.timestamp())))


def test_telemetry_without_device_omits_inverter_tag():
    point = TelemetryPoint(vendor=Vendor.MOCK, vendor_plant_id="p1", ts=TS, ac_power_kw=1.0)
    assert "inverter_id" not in telemetry_to_point(point).to_line_protocol()


def test_twin_line_protocol():
    point = TwinPoint(
        plant_id="p1",
        ts=TS,
        expected_ac_kw=760.5,
        poa_irradiance_wm2=910.2,
        cell_temp_c=52.1,
    )
    line = twin_to_point(point).to_line_protocol()
    assert line.startswith("twin_expected,")
    assert "plant_id=p1" in line
    # model_version alan (field) olmalı, etiket değil — etiket olsaydı sürüm
    # değişiminde aynı zaman damgası için iki seri oluşur ve toplanırdı
    assert 'model_version="physical-v2"' in line
    assert "model_version" not in line.split(" ")[0]  # etiket bölümünde yok
    assert "expected_ac_kw=760.5" in line
    assert "poa_irradiance_wm2=910.2" in line


def test_forecast_points_go_to_separate_measurement():
    point = TwinPoint(
        plant_id="p1",
        ts=TS,
        expected_ac_kw=500.0,
        expected_ac_kw_p10=430.0,
        expected_ac_kw_p90=580.0,
        horizon_days=1,
    )
    line = twin_to_point(point).to_line_protocol()
    assert line.startswith("twin_forecast,")
    assert "horizon_days=1" in line
    assert "expected_ac_kw_p10=430" in line
    assert "expected_ac_kw_p90=580" in line


def test_accuracy_line_protocol():
    from datetime import date

    from luminmind.analytics.accuracy import AccuracyScore
    from luminmind.core.influx import accuracy_to_point

    score = AccuracyScore(
        plant_id="p1",
        day=date(2026, 7, 20),
        model_version="physical-v2",
        sample_count=96,
        capacity_kw=1000.0,
        mae_kw=18.0,
        rmse_kw=31.0,
        mbe_kw=-4.0,
        nmae_pct=1.8,
        nrmse_pct=3.1,
        nmbe_pct=-0.4,
        r2=0.97,
        energy_actual_kwh=6100.0,
        energy_expected_kwh=6050.0,
        energy_error_pct=-0.82,
        skill_vs_reference=0.42,
    )
    line = accuracy_to_point(score).to_line_protocol()
    assert line.startswith("twin_accuracy,")
    assert "plant_id=p1" in line
    assert "nmae_pct=1.8" in line
    assert "skill_vs_reference=0.42" in line


def test_hourly_line_protocol():
    agg = HourlyAggregate(
        hour_start=TS,
        plant_id="p1",
        inverter_id="inv-01",
        sample_count=4,
        ac_power_kw_mean=120.0,
        ac_power_kw_max=140.0,
        energy_kwh=95.0,
    )
    line = hourly_to_point(agg).to_line_protocol()
    assert line.startswith("pv_hourly,")
    assert "ac_power_kw_mean=120" in line
    assert "energy_kwh=95" in line
    assert "sample_count=4" in line


def test_daily_line_protocol():
    agg = DailyAggregate(
        day_start=TS.replace(hour=0), plant_id="p1", energy_kwh=5200.0, peak_ac_power_kw=940.0
    )
    line = daily_to_point(agg).to_line_protocol()
    assert line.startswith("pv_daily,plant_id=p1")
    assert "energy_kwh=5200" in line
    assert "peak_ac_power_kw=940" in line
