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


@dataclass(frozen=True)
class RawSample:
    """Influx `pv_telemetry`'den okunan tek satır (bir cihazın bir zaman damgası)."""

    ts: datetime
    plant_id: str
    inverter_id: str
    fields: dict[str, float] = field(default_factory=dict)


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


def aggregate_hourly(samples: list[RawSample]) -> list[HourlyAggregate]:
    buckets: dict[tuple[datetime, str, str], list[RawSample]] = defaultdict(list)
    for sample in samples:
        buckets[(_hour_floor(sample.ts), sample.plant_id, sample.inverter_id)].append(sample)

    aggregates: list[HourlyAggregate] = []
    for (hour_start, plant_id, inverter_id), bucket in sorted(buckets.items()):
        bucket.sort(key=lambda s: s.ts)
        ac_values = [s.fields["ac_power_kw"] for s in bucket if "ac_power_kw" in s.fields]
        energy_values = [
            s.fields["energy_total_kwh"] for s in bucket if "energy_total_kwh" in s.fields
        ]
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
                energy_kwh=_energy_delta(energy_values),
            )
        )
    return aggregates


def _energy_delta(energy_values: list[float]) -> float | None:
    """Sayaç okumalarından saat içi üretimi çıkarır; sayaç sıfırlanmasına dayanıklı.

    Üreticiler iki tür sayaç verir: kümülatif (Huawei/SMA, hiç sıfırlanmaz) ve
    **günlük** (Tescom `gunluk_uretim_kwh`, gece yarısı sıfırlanır). Düz fark
    almak günlük sayaçta reset saatinde negatif değer üretir (ör. 18,4 → 0,0 =
    −18,4 kWh) ve günlük toplamı bozar.

    Son okuma ilkinden küçükse pencere içinde reset olmuş demektir; o saatteki
    üretim reset sonrası birikmiş değer kadardır. Kümülatif sayaçlarda bu dal
    hiç çalışmaz, davranış değişmez.
    """
    if len(energy_values) < 2:
        return None
    delta = energy_values[-1] - energy_values[0]
    if delta < 0:
        delta = energy_values[-1]
    return round(delta, 4)


def aggregate_daily(hourly: list[HourlyAggregate]) -> list[DailyAggregate]:
    """Saatlik cihaz agregatlarını tesis bazlı günlük KPI'lara indirger."""
    buckets: dict[tuple[datetime, str], list[HourlyAggregate]] = defaultdict(list)
    for aggregate in hourly:
        buckets[(_day_floor(aggregate.hour_start), aggregate.plant_id)].append(aggregate)

    dailies: list[DailyAggregate] = []
    for (day_start, plant_id), bucket in sorted(buckets.items()):
        energy = sum(a.energy_kwh for a in bucket if a.energy_kwh is not None)
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
