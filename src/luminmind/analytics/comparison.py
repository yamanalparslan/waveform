"""Beklenen (dijital ikiz) ile gerçek üretimin hizalanması ve sapma serisi.

Gece ve düşük ışınım noktaları filtrelenir: beklenen üretim eşiğin altındayken
yüzdesel sapma anlamsızdır (payda ~0). Sapma işareti: negatif = düşük performans.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from luminmind.core.aggregate import RawSample


@dataclass(frozen=True)
class DeviationSample:
    ts: datetime
    actual_kw: float
    expected_kw: float

    @property
    def deviation_pct(self) -> float:
        """(gerçek - beklenen) / beklenen × 100; negatif = eksik üretim."""
        return (self.actual_kw - self.expected_kw) / self.expected_kw * 100.0


def plant_actual_from_samples(samples: list[RawSample]) -> dict[str, dict[datetime, float]]:
    """Cihaz bazlı ham örneklerden tesis toplamı AC güç serisi üretir."""
    totals: dict[str, dict[datetime, float]] = defaultdict(lambda: defaultdict(float))
    for sample in samples:
        if "ac_power_kw" in sample.fields:
            totals[sample.plant_id][sample.ts] += sample.fields["ac_power_kw"]
    return {plant: dict(series) for plant, series in totals.items()}


def build_deviation_series(
    actual: dict[datetime, float],
    expected: dict[datetime, float],
    min_expected_kw: float = 10.0,
) -> list[DeviationSample]:
    """İki seriyi zaman damgası üzerinden hizalar; düşük ışınım noktalarını eler."""
    samples = [
        DeviationSample(ts=ts, actual_kw=actual[ts], expected_kw=expected[ts])
        for ts in sorted(actual.keys() & expected.keys())
        if expected[ts] >= min_expected_kw
    ]
    return samples
