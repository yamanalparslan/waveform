"""Beklenen (dijital ikiz) ile gerçek üretimin hizalanması ve sapma serisi.

Gece ve düşük ışınım noktaları filtrelenir: beklenen üretim eşiğin altındayken
yüzdesel sapma anlamsızdır (payda ~0). Sapma işareti: negatif = düşük performans.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from luminmind.core.aggregate import RawSample

# Grafik ve karşılaştırma çözünürlüğü — Tescom cihazları saniye sınırında
# damga gönderiyor ve fabrikalar arası zamanlar birbirine oturmuyor; 5 dk
# bucket'a yuvarlayınca tüm cihazlar aynı bucket'ta toplanır.
_BUCKET_S = 300  # 5 dk


@dataclass(frozen=True)
class DeviationSample:
    ts: datetime
    actual_kw: float
    expected_kw: float

    @property
    def deviation_pct(self) -> float:
        """(gerçek - beklenen) / beklenen × 100; negatif = eksik üretim."""
        return (self.actual_kw - self.expected_kw) / self.expected_kw * 100.0


def _bucket(ts: datetime) -> datetime:
    """Damgayı 5 dk bucket sınırına yuvarlar (floor)."""
    epoch = int(ts.timestamp())
    aligned = epoch - (epoch % _BUCKET_S)
    return datetime.fromtimestamp(aligned, tz=ts.tzinfo)


def plant_actual_from_samples(samples: list[RawSample]) -> dict[str, dict[datetime, float]]:
    """Cihaz bazlı ham örneklerden tesis toplamı AC güç serisi üretir.

    Damgalar 5 dk bucket'a yuvarlanır; her cihazın bucket içindeki değerlerinin
    ortalaması alınır (ölçüm sıklığı cihazlar arasında değişebilir), sonra
    bucket bazında cihazlar toplanır. Böylece tesis eğrisi düzgün "toplam AC
    güç" olur, cihaz başına zigzag/spike üretmez.
    """
    # (plant, bucket, inverter) -> [values]
    buckets: dict[tuple[str, datetime, str], list[float]] = defaultdict(list)
    for sample in samples:
        v = sample.fields.get("ac_power_kw")
        if v is None:
            continue
        buckets[(sample.plant_id, _bucket(sample.ts), sample.inverter_id)].append(v)
    # Cihaz başına ortalama, sonra bucket'ta topla
    per_bucket: dict[str, dict[datetime, float]] = defaultdict(lambda: defaultdict(float))
    for (plant_id, bucket, _inv), values in buckets.items():
        per_bucket[plant_id][bucket] += sum(values) / len(values)
    return {plant: dict(series) for plant, series in per_bucket.items()}


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
