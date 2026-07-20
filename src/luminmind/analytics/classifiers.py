"""Sapma sınıflandırıcıları: mikro çatlak / gölgelenme / kirlilik (PLAN.md Faz 5).

Kural tabanlı + istatistiksel (medyan/MAD) yaklaşım. Etiketli gerçek arıza verisi
olmadığından sonuçlar "şüphe" seviyesindedir (PLAN.md risk #8) — belirsizlik
`severity` ve `evidence` alanlarıyla taşınır.

Kurallar:
- **Mikro çatlak:** pencere içinde ani, kalıcı basamak düşüş (öncesi sağlıklı,
  sonrası belirgin eksik üretim).
- **Gölgelenme:** günün belirli saat bandında tekrarlayan çukur; diğer saatler
  sağlıklı. En az 2 günde tekrar şartı aranır.
- **Kirlilik:** tüm gün boyunca yaklaşık üniform, orta şiddette kalıcı kayıp
  (medyan belirgin negatif, dağılım dar).

Öncelik: mikro çatlak > gölgelenme > kirlilik (yapısal arıza önce raporlanır).
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np

from luminmind.analytics.comparison import DeviationSample

KIND_MICROCRACK = "microcrack"
KIND_SHADING = "shading"
KIND_SOILING = "soiling"

_CRITICAL_LOSS_PCT = 15.0


@dataclass(frozen=True)
class ClassifierConfig:
    min_samples: int = 16  # en az 4 saatlik gündüz verisi
    step_min_delta_pct: float = 8.0  # mikro çatlak basamak eşiği
    step_min_side_samples: int = 8  # basamağın iki yanında asgari örnek
    shading_band_loss_pct: float = 10.0  # gölge bandı kayıp eşiği
    shading_healthy_pct: float = -4.0  # diğer saatlerin "sağlıklı" sınırı
    shading_min_days: int = 2  # bandın tekrar etmesi gereken gün sayısı
    soiling_min_loss_pct: float = 5.0  # kirlilik medyan kayıp eşiği
    soiling_uniform_mad_pct: float = 4.0  # üniformluk (MAD) eşiği


@dataclass(frozen=True)
class AnomalyFinding:
    kind: str
    severity: str  # warning | critical
    deviation_pct: float  # karakteristik kayıp (negatif)
    started_at: datetime
    ended_at: datetime | None
    evidence: dict[str, Any] = field(default_factory=dict)


def _severity(loss_pct: float) -> str:
    return "critical" if abs(loss_pct) >= _CRITICAL_LOSS_PCT else "warning"


def _detect_microcrack(
    samples: list[DeviationSample], deviations: np.ndarray, config: ClassifierConfig
) -> AnomalyFinding | None:
    n = len(samples)
    side = config.step_min_side_samples
    # Yerel pencereli basamak tespiti: basamağı gerçek anına sabitler
    # (global medyan doygunlaştığı için erken indekslere kayardı)
    best_index, best_delta = -1, 0.0
    for i in range(side, n - side + 1):
        before = float(np.median(deviations[i - side : i]))
        after = float(np.median(deviations[i : i + side]))
        delta = after - before
        if delta < best_delta:
            best_index, best_delta = i, delta
    if best_index < 0 or abs(best_delta) < config.step_min_delta_pct:
        return None
    # kalıcılık: basamak sonrası pencerenin tamamı belirgin negatif kalmalı
    after_median = float(np.median(deviations[best_index:]))
    if after_median > -config.step_min_delta_pct:
        return None
    # başlangıcı netleştir: yerel medyanlar pencere içinde doyduğu için basamağın
    # gerçek anı, önce/sonra medyanlarının orta eşiğini ilk aşan örnektir
    before_local = float(np.median(deviations[best_index - side : best_index]))
    after_local = float(np.median(deviations[best_index : best_index + side]))
    midpoint = (before_local + after_local) / 2.0
    start_index = best_index
    for j in range(max(0, best_index - side), min(n, best_index + side)):
        if deviations[j] <= midpoint:
            start_index = j
            break
    return AnomalyFinding(
        kind=KIND_MICROCRACK,
        severity=_severity(after_median),
        deviation_pct=round(after_median, 2),
        started_at=samples[start_index].ts,
        ended_at=None,
        evidence={
            "step_delta_pct": round(best_delta, 2),
            "before_median_pct": round(float(np.median(deviations[:best_index])), 2),
            "after_median_pct": round(after_median, 2),
        },
    )


def _detect_shading(
    samples: list[DeviationSample], deviations: np.ndarray, config: ClassifierConfig
) -> AnomalyFinding | None:
    by_hour: dict[int, list[float]] = {}
    days_by_hour: dict[int, set[str]] = {}
    for sample, deviation in zip(samples, deviations, strict=True):
        hour = sample.ts.hour
        by_hour.setdefault(hour, []).append(float(deviation))
        days_by_hour.setdefault(hour, set()).add(sample.ts.date().isoformat())

    hour_medians = {h: float(np.median(v)) for h, v in by_hour.items()}
    bad_hours = sorted(
        h for h, m in hour_medians.items() if m <= -config.shading_band_loss_pct
    )
    good_hours = [h for h, m in hour_medians.items() if m >= config.shading_healthy_pct]
    if not bad_hours or not good_hours:
        return None
    # band bitişik olmalı (tek gölge kaynağı varsayımı) ve en az N gün tekrar etmeli
    if bad_hours[-1] - bad_hours[0] >= len(bad_hours) + 1:
        return None
    recurring_days = set.intersection(*(days_by_hour[h] for h in bad_hours))
    if len(recurring_days) < config.shading_min_days:
        return None
    band_median = float(np.median([m for h, m in hour_medians.items() if h in bad_hours]))
    first_bad = next(s for s in samples if s.ts.hour == bad_hours[0])
    return AnomalyFinding(
        kind=KIND_SHADING,
        severity=_severity(band_median),
        deviation_pct=round(band_median, 2),
        started_at=first_bad.ts,
        ended_at=None,
        evidence={
            "band_hours_utc": bad_hours,
            "band_median_pct": round(band_median, 2),
            "recurring_days": sorted(recurring_days),
        },
    )


def _detect_soiling(
    samples: list[DeviationSample], deviations: np.ndarray, config: ClassifierConfig
) -> AnomalyFinding | None:
    median = float(np.median(deviations))
    mad = float(np.median(np.abs(deviations - median)))
    if median > -config.soiling_min_loss_pct or mad > config.soiling_uniform_mad_pct:
        return None
    return AnomalyFinding(
        kind=KIND_SOILING,
        severity=_severity(median),
        deviation_pct=round(median, 2),
        started_at=samples[0].ts,
        ended_at=None,
        evidence={"median_pct": round(median, 2), "mad_pct": round(mad, 2)},
    )


def classify_window(
    samples: list[DeviationSample], config: ClassifierConfig | None = None
) -> AnomalyFinding | None:
    """Analiz penceresini sınıflandırır; sağlıklıysa None döndürür."""
    config = config or ClassifierConfig()
    if len(samples) < config.min_samples:
        return None
    deviations = np.array([s.deviation_pct for s in samples])
    for detector in (_detect_microcrack, _detect_shading, _detect_soiling):
        finding = detector(samples, deviations, config)
        if finding is not None:
            return finding
    return None
