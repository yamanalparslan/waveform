"""Downsampling agregasyon mantığı (saf fonksiyonlar — Influx'tan bağımsız test edilir).

Kurallar (PLAN.md §3.2):
- 15 dk → saatlik: güçler `mean` (+ `max`), kümülatif enerji `last - first`.
- saatlik → günlük: tesis bazında enerji toplamı ve tepe güç.
Aynı pencere yeniden hesaplanırsa aynı sonuç çıkar; Influx'ta aynı seri+ts
üzerine yazıldığı için görev idempotenttir.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from luminmind.analytics.rollup import counter_increments, counter_kind_for


@dataclass(frozen=True)
class RawSample:
    """Influx `pv_telemetry`'den okunan tek satır (bir cihazın bir zaman damgası).

    `vendor` sayaç semantiğini çözmek için taşınır (günlük sıfırlanan mı,
    ömürlük mü — bkz. `analytics.rollup.CounterKind`). Etiket Influx'ta zaten
    yazılı; okumada atılırsa agregasyon her sayacı ömürlük sanar ve günlük
    sayaçta pencerenin ilk okumasını sessizce düşürür.
    """

    ts: datetime
    plant_id: str
    inverter_id: str
    fields: dict[str, float] = field(default_factory=dict)
    vendor: str = ""


@dataclass(frozen=True)
class HourlyAggregate:
    hour_start: datetime
    plant_id: str
    inverter_id: str
    sample_count: int
    ac_power_kw_mean: float | None
    ac_power_kw_max: float | None
    energy_kwh: float | None  # saat içi üretim (kümülatif sayaçtan last-first)


@dataclass(frozen=True)
class DailyAggregate:
    day_start: datetime
    plant_id: str
    energy_kwh: float
    peak_ac_power_kw: float


def _hour_floor(ts: datetime) -> datetime:
    return ts.replace(minute=0, second=0, microsecond=0)


def _day_floor(ts: datetime) -> datetime:
    return ts.replace(hour=0, minute=0, second=0, microsecond=0)


def _hourly_energy(samples: list[RawSample]) -> dict[tuple[datetime, str, str], float]:
    """Cihaz bazında sayaç artışlarını saatlere dağıtır.

    Enerji **cihazın tüm serisi üzerinden** yürünerek hesaplanır, saat saat
    değil: saat içinde "son − ilk" almak iki saat arasındaki aralığı hiçbir
    saate saymıyordu ve 15 dakikalık örneklemede her saatin dörtte birini
    siliyordu (bkz. `rollup.counter_increments`).

    Sayaç semantiği cihazın üreticisinden çözülür; günlük sıfırlanan sayaçta
    pencerenin ilk okuması da üretim sayılır.
    """
    series: dict[tuple[str, str, str], list[tuple[datetime, float]]] = defaultdict(list)
    for sample in samples:
        value = sample.fields.get("energy_total_kwh")
        if value is not None:
            series[(sample.plant_id, sample.inverter_id, sample.vendor)].append(
                (sample.ts, value)
            )

    energy: dict[tuple[datetime, str, str], float] = defaultdict(float)
    for (plant_id, inverter_id, vendor), readings in series.items():
        for ts, increment in counter_increments(readings, counter_kind_for(vendor)):
            energy[(_hour_floor(ts), plant_id, inverter_id)] += increment
    return dict(energy)


def aggregate_hourly(samples: list[RawSample]) -> list[HourlyAggregate]:
    buckets: dict[tuple[datetime, str, str], list[RawSample]] = defaultdict(list)
    for sample in samples:
        buckets[(_hour_floor(sample.ts), sample.plant_id, sample.inverter_id)].append(sample)

    energy_by_hour = _hourly_energy(samples)
    aggregates: list[HourlyAggregate] = []
    for key, bucket in sorted(buckets.items()):
        hour_start, plant_id, inverter_id = key
        bucket.sort(key=lambda s: s.ts)
        ac_values = [s.fields["ac_power_kw"] for s in bucket if "ac_power_kw" in s.fields]
        energy = energy_by_hour.get(key)
        aggregates.append(
            HourlyAggregate(
                hour_start=hour_start,
                plant_id=plant_id,
                inverter_id=inverter_id,
                sample_count=len(bucket),
                ac_power_kw_mean=(
                    None if not ac_values else round(sum(ac_values) / len(ac_values), 4)
                ),
                ac_power_kw_max=None if not ac_values else max(ac_values),
                energy_kwh=None if energy is None else round(energy, 4),
            )
        )
    return aggregates


def aggregate_daily(hourly: list[HourlyAggregate]) -> list[DailyAggregate]:
    """Saatlik cihaz agregatlarını tesis bazlı günlük KPI'lara indirger."""
    buckets: dict[tuple[datetime, str], list[HourlyAggregate]] = defaultdict(list)
    for aggregate in hourly:
        buckets[(_day_floor(aggregate.hour_start), aggregate.plant_id)].append(aggregate)

    dailies: list[DailyAggregate] = []
    for (day_start, plant_id), bucket in sorted(buckets.items()):
        # `float(...)` şart: `sum()` boş üreteçte **int** 0 döner ve `round(0, 4)`
        # de int kalır. Alan `float` diye anotasyonlu olduğu için bu fark tip
        # denetiminden geçiyor ama Influx'a int olarak yazılıyor — Influx da alan
        # tipini ilk yazımda sabitlediği için `pv_daily.energy_kwh` integer
        # tiplenmiş ve sonraki tüm ondalıklı yazımlar sunucuda sessizce
        # düşmüştü (22–29.07.2026 arası günlük enerji bu yüzden hep 0 göründü).
        energy = float(sum(a.energy_kwh for a in bucket if a.energy_kwh is not None))
        peaks = [a.ac_power_kw_max for a in bucket if a.ac_power_kw_max is not None]
        dailies.append(
            DailyAggregate(
                day_start=day_start,
                plant_id=plant_id,
                energy_kwh=round(energy, 4),
                peak_ac_power_kw=max(peaks) if peaks else 0.0,
            )
        )
    return dailies
