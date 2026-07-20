from datetime import UTC, datetime

from luminmind.adapters.normalize import normalize_huawei_dev_kpi, normalize_sma_measurements
from luminmind.core.schemas import Vendor


def test_huawei_dev_kpi_normalization(load_fixture):
    payload = load_fixture("huawei/dev_five_minutes.json")
    points = normalize_huawei_dev_kpi("NE=33554616", payload)

    assert len(points) == 2
    first = points[0]
    assert first.vendor == Vendor.HUAWEI
    assert first.vendor_plant_id == "NE=33554616"
    assert first.vendor_device_id == "1000000031104426"
    assert first.ts == datetime(2026, 7, 20, 7, 0, tzinfo=UTC)
    assert first.ac_power_kw == 182.4
    assert first.dc_power_kw == 188.1
    assert first.dc_voltage_v == 612.3
    assert first.dc_current_a == 9.1
    assert first.energy_total_kwh == 152340.5
    assert first.temp_c == 41.2


def test_huawei_missing_collect_time_skipped():
    payload = {"success": True, "data": [{"devId": 1, "dataItemMap": {"active_power": 5}}]}
    assert normalize_huawei_dev_kpi("p", payload) == []


def test_huawei_partial_data_items():
    payload = {
        "success": True,
        "data": [{"devId": 1, "collectTime": 1752994800000, "dataItemMap": {"active_power": 5}}],
    }
    points = normalize_huawei_dev_kpi("p", payload)
    assert points[0].ac_power_kw == 5.0
    assert points[0].dc_power_kw is None
    assert points[0].measured_fields() == {"ac_power_kw": 5.0}


def test_sma_measurements_normalization(load_fixture):
    payload = load_fixture("sma/measurements.json")
    points = normalize_sma_measurements("sma-plant-1", payload)

    assert len(points) == 2
    first = points[0]
    assert first.vendor == Vendor.SMA
    assert first.vendor_device_id == "inv-01"
    # W → kW ve Wh → kWh dönüşümü
    assert first.ac_power_kw == 182.4
    assert first.energy_total_kwh == 152340.5
    assert first.ts == datetime(2026, 7, 20, 7, 0, tzinfo=UTC)

    # +03:00 damgası UTC'ye çevrilir
    second = points[1]
    assert second.ts == datetime(2026, 7, 20, 4, 0, tzinfo=UTC)


def test_sma_empty_payload():
    assert normalize_sma_measurements("p", {}) == []
