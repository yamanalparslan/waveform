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
from datetime import UTC, datetime, time
from types import TracebackType
from typing import Any, Self

from influxdb_client.client.influxdb_client_async import InfluxDBClientAsync
from influxdb_client.client.write.point import Point
from influxdb_client.domain.write_precision import WritePrecision

from luminmind.analytics.accuracy import AccuracyScore
from luminmind.core.aggregate import DailyAggregate, HourlyAggregate, RawSample
from luminmind.core.schemas import TelemetryPoint, TwinPoint

BUCKET_RAW = "lm_raw"
BUCKET_HOURLY = "lm_hourly"
BUCKET_DAILY = "lm_daily"

MEASUREMENT_RAW = "pv_telemetry"
MEASUREMENT_HOURLY = "pv_hourly"
MEASUREMENT_DAILY = "pv_daily"
MEASUREMENT_TWIN = "twin_expected"  # D+0 en iyi tahmin (karşılaştırma motorunun girdisi)
MEASUREMENT_FORECAST = "twin_forecast"  # D+1.. ileri tahminler, horizon_days ile etiketli
MEASUREMENT_ACCURACY = "twin_accuracy"  # günlük doğruluk skorları


# Analiz ızgarası — dijital ikiz bu adımda üretir, ham ölçümler buna indirgenir.
GRID = "15m"

_SERIES_SOURCES = {
    "15m": (BUCKET_RAW, MEASUREMENT_RAW),
    "1h": (BUCKET_HOURLY, MEASUREMENT_HOURLY),
    "1d": (BUCKET_DAILY, MEASUREMENT_DAILY),
}


def plant_series_flux(
    vendor_plant_id: str,
    metric: str,
    start: datetime,
    stop: datetime,
    resolution: str = GRID,
) -> str:
    """Tesis serisi sorgusu. Saf fonksiyon olduğu için birim test edilebilir.

    **Ham kova mutlaka ızgaraya indirgenir.** Üretici telemetrisi rastgele
    anlarda gelir (5 dakikada bir, cihazlar farklı saniyelerde). Ham damgaları
    olduğu gibi döndürmek iki hata üretiyordu:

    1. Çağıranlar seriyi 15 dakikalık kabul edip `toplam × 0,25` ile enerji
       hesaplıyor; 5 dakikalık veride bu enerjiyi üçe katlıyordu (canlıda
       400 kWp'lik sahada 648 kWh/saat gibi fiziksel olarak imkânsız değerler).
    2. Cihazların damgaları çakışmadığı için "tesis toplamı" aslında tek
       cihazın anlık gücüydü — toplama hiç gerçekleşmiyordu.

    `aggregateWindow` cihaz başına ortalama alır (grup anahtarında `inverter_id`
    var); çağıran döngü sonra cihazları toplar. Damga pencerenin **başına**
    konur (`timeSrc: "_start"`), çünkü dijital ikiz de aralık başına yazıyor ve
    hizalama tam damga eşleşmesiyle yapılıyor — bir pencere kayması tüm
    karşılaştırmayı boşa düşürür.

    Saatlik/günlük kovalar zaten toplanmış yazıldığı için dokunulmaz.
    """
    bucket, measurement = _SERIES_SOURCES[resolution]
    downsample = (
        f'\n  |> aggregateWindow(every: {GRID}, fn: mean, createEmpty: false, '
        'timeSrc: "_start")'
        if bucket == BUCKET_RAW
        else ""
    )
    return f"""
from(bucket: "{bucket}")
  |> range(start: {start.isoformat()}, stop: {stop.isoformat()})
  |> filter(fn: (r) => r._measurement == "{measurement}")
  |> filter(fn: (r) => r.plant_id == "{vendor_plant_id}")
  |> filter(fn: (r) => r._field == "{metric}"){downsample}
"""


def _num(record: Point, name: str, value: float | int | None) -> Point:
    """Sayısal alanı **daima float olarak** yazar; `None` alanı hiç yazmaz.

    InfluxDB bir alanın tipini o alanın *ilk* yazımında sabitler. Python'da
    `3532.4` float, `0` ise int olduğu için aynı alan bir gün tam sayı denk
    gelirse integer tiplenir ve sonraki tüm ondalıklı yazımlar sunucu tarafında
    **sessizce düşer**:

        partial write: field type conflict: input field "energy_kwh" on
        measurement "pv_daily" is type float, already exists as type integer
        dropped=2

    Hata yalnızca worker logunda görünür; panel eksik günü "üretim yok" diye
    gösterir. Bu yüzden tip dönüşümü her çağrı yerinde tekrarlanmak yerine tek
    kapıdan geçiyor — `sample_count` alanında zaten elle yapılıyordu, ama diğer
    alanlar açıkta kalmıştı.
    """
    if value is not None:
        record.field(name, float(value))
    return record


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
        _num(record, name, value)
    return record


def twin_to_point(point: TwinPoint) -> Point:
    """TwinPoint → Influx noktası.

    `model_version` bilinçli olarak **etiket değil alan**dır: etiket olsaydı
    model sürümü değiştiğinde aynı (tesis, zaman) için iki ayrı seri oluşur ve
    `query_twin_window` bunları toplayarak beklenen üretimi ikiye katlardı.
    Alan olarak tutulunca yeniden yazım doğal olarak üzerine yazar.
    """
    measurement = MEASUREMENT_TWIN if point.horizon_days == 0 else MEASUREMENT_FORECAST
    record = Point(measurement).tag("plant_id", point.plant_id).time(
        point.ts, WritePrecision.S
    )
    _num(record, "expected_ac_kw", point.expected_ac_kw)
    record.field("model_version", point.model_version)  # metin alanı
    if point.horizon_days != 0:
        record.tag("horizon_days", str(point.horizon_days))
    for name, value in (
        ("expected_ac_kw_p10", point.expected_ac_kw_p10),
        ("expected_ac_kw_p90", point.expected_ac_kw_p90),
        ("poa_irradiance_wm2", point.poa_irradiance_wm2),
        ("cell_temp_c", point.cell_temp_c),
        ("clipping_loss_kw", point.clipping_loss_kw),
        ("soiling_ratio", point.soiling_ratio),
    ):
        _num(record, name, value)
    return record


def accuracy_to_point(score: AccuracyScore) -> Point:
    record = (
        Point(MEASUREMENT_ACCURACY)
        .tag("plant_id", score.plant_id)
        .tag("horizon_days", str(score.horizon_days))
        .time(datetime.combine(score.day, time.min, tzinfo=UTC), WritePrecision.S)
        .field("model_version", score.model_version)  # metin alanı
    )
    for name, value in (
        ("sample_count", score.sample_count),
        ("capacity_kw", score.capacity_kw),
        ("mae_kw", score.mae_kw),
        ("rmse_kw", score.rmse_kw),
        ("mbe_kw", score.mbe_kw),
        ("nmae_pct", score.nmae_pct),
        ("nrmse_pct", score.nrmse_pct),
        ("nmbe_pct", score.nmbe_pct),
        ("r2", score.r2),
        ("energy_actual_kwh", score.energy_actual_kwh),
        ("energy_expected_kwh", score.energy_expected_kwh),
        ("energy_error_pct", score.energy_error_pct),
        ("skill_vs_reference", score.skill_vs_reference),
        ("band_coverage_pct", score.band_coverage_pct),
    ):
        _num(record, name, value)
    return record


def hourly_to_point(aggregate: HourlyAggregate) -> Point:
    record = (
        Point(MEASUREMENT_HOURLY)
        .tag("plant_id", aggregate.plant_id)
        .tag("inverter_id", aggregate.inverter_id)
        .time(aggregate.hour_start, WritePrecision.S)
    )
    for name, value in (
        ("sample_count", aggregate.sample_count),
        ("ac_power_kw_mean", aggregate.ac_power_kw_mean),
        ("ac_power_kw_max", aggregate.ac_power_kw_max),
        ("energy_kwh", aggregate.energy_kwh),
    ):
        _num(record, name, value)
    return record


def daily_to_point(aggregate: DailyAggregate) -> Point:
    record = (
        Point(MEASUREMENT_DAILY)
        .tag("plant_id", aggregate.plant_id)
        .time(aggregate.day_start, WritePrecision.S)
    )
    for name, value in (
        ("energy_kwh", aggregate.energy_kwh),
        ("peak_ac_power_kw", aggregate.peak_ac_power_kw),
    ):
        _num(record, name, value)
    return record


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

    async def write_accuracy(self, scores: Sequence[AccuracyScore]) -> None:
        if not scores:
            return
        records = [accuracy_to_point(s) for s in scores]
        await self._client.write_api().write(bucket=BUCKET_DAILY, record=records)

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
        resolution: str = GRID,
    ) -> list[tuple[datetime, float]]:
        """Tesis bazlı seri: ızgaraya indirgenmiş cihaz ortalamalarının toplamı.

        Sorgu `plant_series_flux()` içinde kuruluyor — indirgemenin neden zorunlu
        olduğu ve damganın neden pencere başına konduğu orada anlatılıyor.
        """
        tables = await self._client.query_api().query(
            plant_series_flux(vendor_plant_id, metric, start, stop, resolution)
        )
        totals: dict[datetime, float] = {}
        for table in tables:
            for record in table.records:
                ts = record.get_time()
                totals[ts] = totals.get(ts, 0.0) + float(record.get_value())
        return sorted(totals.items())

    async def last_sample_ts(self, lookback_hours: int = 48) -> dict[str, datetime]:
        """Seri anahtarı → en son yazılmış ölçümün zamanı.

        Ingestion'ın nereden devam edeceğini bu belirler. "Şimdi eksi bir
        çevrim" varsayımı, çekim durduğunda (host uykusu, container yeniden
        başlatma) aradaki süreyi kalıcı olarak boş bırakıyordu: kaçan çevrimin
        verisi bir daha hiç istenmiyordu.
        """
        flux = f"""
from(bucket: "{BUCKET_RAW}")
  |> range(start: -{int(lookback_hours)}h)
  |> filter(fn: (r) => r._measurement == "{MEASUREMENT_RAW}")
  |> filter(fn: (r) => r._field == "ac_power_kw")
  |> group(columns: ["plant_id"])
  |> last()
"""
        tables = await self._client.query_api().query(flux)
        latest: dict[str, datetime] = {}
        for table in tables:
            for record in table.records:
                key = str(record.values.get("plant_id", ""))
                ts = record.get_time()
                if key and (key not in latest or ts > latest[key]):
                    latest[key] = ts
        return latest

    async def query_energy_counters(
        self,
        vendor_plant_id: str,
        start: datetime,
        stop: datetime,
    ) -> dict[str, list[tuple[datetime, float]]]:
        """Sahadaki her cihazın enerji sayacı okumaları: {cihaz no: [(ts, kWh)]}.

        Ham damgalar korunur; ızgaraya indirgemek burada zararlı olurdu — sayacın
        pencere ortalaması alınırsa artışlar bulanır ve son okuma kaybolur.
        Cihazlar **toplanmaz**: her sayaç kendi başına monotondur, önce cihaz
        bazında fark alınıp sonra toplanmaları gerekir (`counter_energy_kwh`).
        """
        flux = f"""
from(bucket: "{BUCKET_RAW}")
  |> range(start: {start.isoformat()}, stop: {stop.isoformat()})
  |> filter(fn: (r) => r._measurement == "{MEASUREMENT_RAW}")
  |> filter(fn: (r) => r.plant_id == "{vendor_plant_id}")
  |> filter(fn: (r) => r._field == "energy_total_kwh")
"""
        tables = await self._client.query_api().query(flux)
        by_device: dict[str, list[tuple[datetime, float]]] = {}
        for table in tables:
            for record in table.records:
                device = str(record.values.get("inverter_id", ""))
                by_device.setdefault(device, []).append(
                    (record.get_time(), float(record.get_value()))
                )
        return {device: sorted(rows) for device, rows in by_device.items()}

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

    async def query_forecast_window(
        self, start: datetime, stop: datetime, horizon_days: int
    ) -> dict[str, dict[datetime, float]]:
        """İleri tahmin serisini (`twin_forecast`) belirli bir ufuk için okur."""
        flux = f"""
from(bucket: "{BUCKET_RAW}")
  |> range(start: {start.isoformat()}, stop: {stop.isoformat()})
  |> filter(fn: (r) => r._measurement == "{MEASUREMENT_FORECAST}")
  |> filter(fn: (r) => r.horizon_days == "{horizon_days}")
  |> filter(fn: (r) => r._field == "expected_ac_kw")
"""
        tables = await self._client.query_api().query(flux)
        result: dict[str, dict[datetime, float]] = {}
        for table in tables:
            for record in table.records:
                plant_id = str(record.values.get("plant_id", ""))
                result.setdefault(plant_id, {})[record.get_time()] = float(record.get_value())
        return result

    async def query_twin_band_window(
        self, start: datetime, stop: datetime
    ) -> dict[str, dict[datetime, tuple[float, float]]]:
        """Ensemble belirsizlik bandını (P10, P90) tesis bazında okur.

        Band yalnızca çok modelli çalıştırmada yazılır; tek modelli günlerde
        sözlük boş döner ve çağıran taraf sabit eşiğe düşer.
        """
        flux = f"""
from(bucket: "{BUCKET_RAW}")
  |> range(start: {start.isoformat()}, stop: {stop.isoformat()})
  |> filter(fn: (r) => r._measurement == "{MEASUREMENT_TWIN}")
  |> filter(fn: (r) => r._field == "expected_ac_kw_p10" or r._field == "expected_ac_kw_p90")
"""
        tables = await self._client.query_api().query(flux)
        collected: dict[str, dict[datetime, dict[str, float]]] = {}
        for table in tables:
            for record in table.records:
                plant_id = str(record.values.get("plant_id", ""))
                bucket = collected.setdefault(plant_id, {}).setdefault(record.get_time(), {})
                bucket[record.get_field()] = float(record.get_value())
        return {
            plant_id: {
                ts: (values["expected_ac_kw_p10"], values["expected_ac_kw_p90"])
                for ts, values in series.items()
                if "expected_ac_kw_p10" in values and "expected_ac_kw_p90" in values
            }
            for plant_id, series in collected.items()
        }

    async def query_raw_window(self, start: datetime, stop: datetime) -> list[RawSample]:
        """`lm_raw`'dan bir zaman penceresini (tüm tesisler) RawSample listesi olarak okur."""
        flux = f"""
from(bucket: "{BUCKET_RAW}")
  |> range(start: {start.isoformat()}, stop: {stop.isoformat()})
  |> filter(fn: (r) => r._measurement == "{MEASUREMENT_RAW}")
"""
        tables = await self._client.query_api().query(flux)
        # `vendor` anahtara dahil: sayaç semantiği (günlük sıfırlanan / ömürlük)
        # ondan çözülüyor ve atılırsa agregasyon her sayacı ömürlük sanar.
        grouped: dict[tuple[datetime, str, str, str], dict[str, float]] = {}
        for table in tables:
            for record in table.records:
                values: dict[str, Any] = record.values
                key = (
                    record.get_time(),
                    str(values.get("plant_id", "")),
                    str(values.get("inverter_id", "")),
                    str(values.get("vendor", "")),
                )
                grouped.setdefault(key, {})[record.get_field()] = float(record.get_value())
        return [
            RawSample(
                ts=ts,
                plant_id=plant_id,
                inverter_id=inverter_id,
                fields=fields,
                vendor=vendor,
            )
            for (ts, plant_id, inverter_id, vendor), fields in sorted(grouped.items())
        ]
