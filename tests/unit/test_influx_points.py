from datetime import UTC, datetime

from luminmind.core.aggregate import DailyAggregate, HourlyAggregate
from luminmind.core.influx import (
    daily_to_point,
    hourly_to_point,
    telemetry_to_point,
    twin_to_point,
)
from luminmind.core.schemas import TelemetryPoint, TwinPoint, Vendor

TS = datetime(2026, 7, 20, 7, 0, tzinfo=UTC)


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
    assert "model_version=pvwatts-v1" in line
    assert "expected_ac_kw=760.5" in line
    assert "poa_irradiance_wm2=910.2" in line


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
