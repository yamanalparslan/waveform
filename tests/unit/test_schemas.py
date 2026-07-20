from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from luminmind.core.schemas import TelemetryPoint, Vendor


def _point(**kwargs) -> TelemetryPoint:
    defaults = {
        "vendor": Vendor.MOCK,
        "vendor_plant_id": "p1",
        "ts": datetime(2026, 7, 20, 10, 0, tzinfo=UTC),
    }
    return TelemetryPoint(**{**defaults, **kwargs})


def test_naive_timestamp_rejected():
    with pytest.raises(ValidationError, match="timezone-aware"):
        _point(ts=datetime(2026, 7, 20, 10, 0))


def test_timestamp_converted_to_utc():
    trt = timezone(timedelta(hours=3))
    point = _point(ts=datetime(2026, 7, 20, 13, 0, tzinfo=trt))
    assert point.ts == datetime(2026, 7, 20, 10, 0, tzinfo=UTC)
    assert point.ts.tzinfo == UTC


def test_measured_fields_drops_none():
    point = _point(ac_power_kw=12.5, temp_c=40.0)
    assert point.measured_fields() == {"ac_power_kw": 12.5, "temp_c": 40.0}


def test_measured_fields_keeps_zero():
    point = _point(ac_power_kw=0.0)
    assert point.measured_fields() == {"ac_power_kw": 0.0}
