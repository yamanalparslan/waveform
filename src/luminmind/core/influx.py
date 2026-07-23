"""InfluxDB 2.x erişim katmanı.

Bucket düzeni (PLAN.md §3.2):
- `lm_raw`    — 15 dk ham ölçümler (measurement: pv_telemetry)
- `lm_hourly` — saatlik agregatlar (measurement: pv_hourly)
- `lm_daily`  — günlük KPI'lar (measurement: pv_daily)

Nokta dönüşümleri saf fonksiyonlardır (birim test edilebilir); ağ erişimi
yalnızca `InfluxStore` içindedir. Aynı (measurement, tag seti, ts) ile yeniden
yazım Influx'ta üzerine yazar — downsampling görevini doğal olarak idempotent kılar.
"""

from collections.abc import Sequence
from datetime import datetime
from types import TracebackType
from typing import Any, Self

from influxdb_client.client.influxdb_client_async import InfluxDBClientAsync
from influxdb_client.client.write.point import Point
from influxdb_client.domain.write_precision import WritePrecision

from luminmind.core.aggregate import DailyAggregate, HourlyAggregate, RawSample
from luminmind.core.schemas import TelemetryPoint, TwinPoint

BUCKET_RAW = "lm_raw"
BUCKET_HOURLY = "lm_hourly"
BUCKET_DAILY = "lm_daily"

MEASUREMENT_RAW = "pv_telemetry"
MEASUREMENT_HOURLY = "pv_hourly"
MEASUREMENT_DAILY = "pv_daily"
MEASUREMENT_TWIN = "twin_expected"


def telemetry_to_point(point: TelemetryPoint) -> Point:
    record = (
        Point(MEASUREMENT_RAW)
        .tag("plant_id", point.vendor_plant_id)
        .tag("vendor", point.vendor.value)
        .time(point.ts, WritePrecision.S)
    )
    if point.vendor_device_id is not None:
        record.tag("inverter_id", point.vendor_device_id)
    for name, value in point.measured_fields().items():
        record.field(name, value)
    return record


def twin_to_point(point: TwinPoint) -> Point:
    record = (
        Point(MEASUREMENT_TWIN)
        .tag("plant_id", point.plant_id)
        .tag("model_version", point.model_version)
        .time(point.ts, WritePrecision.S)
        .field("expected_ac_kw", point.expected_ac_kw)
    )
    if point.poa_irradiance_wm2 is not None:
        record.field("poa_irradiance_wm2", point.poa_irradiance_wm2)
    if point.cell_temp_c is not None:
        record.field("cell_temp_c", point.cell_temp_c)
    return record


def hourly_to_point(aggregate: HourlyAggregate) -> Point:
    record = (
        Point(MEASUREMENT_HOURLY)
        .tag("plant_id", aggregate.plant_id)
        .tag("inverter_id", aggregate.inverter_id)
        .time(aggregate.hour_start, WritePrecision.S)
        .field("sample_count", float(aggregate.sample_count))
    )
    if aggregate.ac_power_kw_mean is not None:
        record.field("ac_power_kw_mean", aggregate.ac_power_kw_mean)
    if aggregate.ac_power_kw_max is not None:
        record.field("ac_power_kw_max", aggregate.ac_power_kw_max)
    if aggregate.energy_kwh is not None:
        record.field("energy_kwh", aggregate.energy_kwh)
    return record


def daily_to_point(aggregate: DailyAggregate) -> Point:
    return (
        Point(MEASUREMENT_DAILY)
        .tag("plant_id", aggregate.plant_id)
        .time(aggregate.day_start, WritePrecision.S)
        .field("energy_kwh", aggregate.energy_kwh)
        .field("peak_ac_power_kw", aggregate.peak_ac_power_kw)
    )


class InfluxStore:
    """Async yazma/okuma sarmalayıcı; `async with` ile kullanılır."""

    def __init__(self, url: str, org: str, token: str) -> None:
        self._org = org
        self._client = InfluxDBClientAsync(url=url, token=token, org=org)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._client.close()

    async def write_telemetry(self, points: Sequence[TelemetryPoint]) -> None:
        if not points:
            return
        records = [telemetry_to_point(p) for p in points]
        await self._client.write_api().write(bucket=BUCKET_RAW, record=records)

    async def write_twin(self, points: Sequence[TwinPoint]) -> None:
        if not points:
            return
        records = [twin_to_point(p) for p in points]
        # Beklenen üretim ham verilerle aynı bucket'ta tutulur (PLAN.md §3.2)
        await self._client.write_api().write(bucket=BUCKET_RAW, record=records)

    async def write_hourly(self, aggregates: Sequence[HourlyAggregate]) -> None:
        if not aggregates:
            return
        records = [hourly_to_point(a) for a in aggregates]
        await self._client.write_api().write(bucket=BUCKET_HOURLY, record=records)

    async def write_daily(self, aggregates: Sequence[DailyAggregate]) -> None:
        if not aggregates:
            return
        records = [daily_to_point(a) for a in aggregates]
        await self._client.write_api().write(bucket=BUCKET_DAILY, record=records)

    async def query_plant_series(
        self,
        vendor_plant_id: str,
        metric: str,
        start: datetime,
        stop: datetime,
        resolution: str = "15m",
    ) -> list[tuple[datetime, float]]:
        """Tesis bazlı tek metrik serisi; cihaz değerleri zaman damgasında toplanır."""
        sources = {
            "15m": (BUCKET_RAW, MEASUREMENT_RAW),
            "1h": (BUCKET_HOURLY, MEASUREMENT_HOURLY),
            "1d": (BUCKET_DAILY, MEASUREMENT_DAILY),
        }
        bucket, measurement = sources[resolution]
        flux = f"""
from(bucket: "{bucket}")
  |> range(start: {start.isoformat()}, stop: {stop.isoformat()})
  |> filter(fn: (r) => r._measurement == "{measurement}")
  |> filter(fn: (r) => r.plant_id == "{vendor_plant_id}")
  |> filter(fn: (r) => r._field == "{metric}")
"""
        tables = await self._client.query_api().query(flux)
        totals: dict[datetime, float] = {}
        for table in tables:
            for record in table.records:
                ts = record.get_time()
                totals[ts] = totals.get(ts, 0.0) + float(record.get_value())
        return sorted(totals.items())
    async def query_device_series(
        self,
        vendor_plant_id: str,
        vendor_device_id: str,
        metric: str,
        start: datetime,
        stop: datetime,
    ) -> list[tuple[datetime, float]]:
        """Tek cihaz + tek metrik serisi (15 dk çözünürlük)."""
        flux = f"""
from(bucket: "{BUCKET_RAW}")
  |> range(start: {start.isoformat()}, stop: {stop.isoformat()})
  |> filter(fn: (r) => r._measurement == "{MEASUREMENT_RAW}")
  |> filter(fn: (r) => r.plant_id == "{vendor_plant_id}")
  |> filter(fn: (r) => r.inverter_id == "{vendor_device_id}")
  |> filter(fn: (r) => r._field == "{metric}")
"""
        tables = await self._client.query_api().query(flux)
        points: list[tuple[datetime, float]] = []
        for table in tables:
            for record in table.records:
                points.append((record.get_time(), float(record.get_value())))
        return sorted(points)

    async def query_twin_window(
        self, start: datetime, stop: datetime
    ) -> dict[str, dict[datetime, float]]:
        """`twin_expected`'dan tesis bazlı beklenen üretim serilerini okur."""
        flux = f"""
from(bucket: "{BUCKET_RAW}")
  |> range(start: {start.isoformat()}, stop: {stop.isoformat()})
  |> filter(fn: (r) => r._measurement == "{MEASUREMENT_TWIN}")
  |> filter(fn: (r) => r._field == "expected_ac_kw")
"""
        tables = await self._client.query_api().query(flux)
        result: dict[str, dict[datetime, float]] = {}
        for table in tables:
            for record in table.records:
                plant_id = str(record.values.get("plant_id", ""))
                result.setdefault(plant_id, {})[record.get_time()] = float(record.get_value())
        return result

    async def query_raw_window(self, start: datetime, stop: datetime) -> list[RawSample]:
        """`lm_raw`'dan bir zaman penceresini (tüm tesisler) RawSample listesi olarak okur."""
        flux = f"""
from(bucket: "{BUCKET_RAW}")
  |> range(start: {start.isoformat()}, stop: {stop.isoformat()})
  |> filter(fn: (r) => r._measurement == "{MEASUREMENT_RAW}")
"""
        tables = await self._client.query_api().query(flux)
        grouped: dict[tuple[datetime, str, str], dict[str, float]] = {}
        for table in tables:
            for record in table.records:
                values: dict[str, Any] = record.values
                key = (
                    record.get_time(),
                    str(values.get("plant_id", "")),
                    str(values.get("inverter_id", "")),
                )
                grouped.setdefault(key, {})[record.get_field()] = float(record.get_value())
        return [
            RawSample(ts=ts, plant_id=plant_id, inverter_id=inverter_id, fields=fields)
            for (ts, plant_id, inverter_id), fields in sorted(grouped.items())
        ]
