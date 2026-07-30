"""Dijital ikizin doğruluk skor tahtası.

Bu modül olmadan model geliştirmek körlemedir: bir değişikliğin tahmini
iyileştirip iyileştirmediğini söyleyecek tek sayı yoktur. Buradaki metrikler
her gece hesaplanıp `lm_daily` bucket'ına yazılır; böylece model sürümleri
zaman içinde karşılaştırılabilir olur.

Metrik seçimi:

- **nMAE / nRMSE / nMBE** — kapasiteye normalize edilmiş hata. Yüzdesel hata
  (MAPE) düşük üretimde patladığı için sektör standardı normalizasyon
  kapasiteyedir; farklı büyüklükteki tesisler ancak böyle kıyaslanabilir.
- **nMBE (bias)** — işaretli ortalama hata. Sistematik kaymanın tek göstergesi
  budur ve kalibrasyonun hedefidir. MAE düşerken MBE sıfıra yaklaşmıyorsa
  model rastgele değil, yanlı hata yapıyordur.
- **Enerji hatası (%)** — santral sahibinin gerçekten baktığı sayı: "bugün ne
  kadar üreteceğim" tahmininin günlük toplamdaki isabeti.
- **Skill skoru** — modelin bir referansa (dünün üretimi = persistence) göre ne
  kadar iyi olduğu. Skill ≤ 0 ise fizik modeli, "dün ne olduysa bugün de o
  olur" demekten daha kötüdür ve derhal bakılmalıdır.
- **Band kapsaması** — ensemble P10–P90 bandının gerçek üretimi kapsama oranı.
  İyi kalibre bir band ~%80 kapsar; çok düşükse band dar (aşırı güven), çok
  yüksekse geniş (işe yaramaz).
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

import numpy as np

MIN_SCORED_SAMPLES = 8


@dataclass(frozen=True)
class AccuracyPair:
    """Tek zaman damgasında gerçek ve beklenen üretim (kW)."""

    ts: datetime
    actual_kw: float
    expected_kw: float
    reference_kw: float | None = None  # persistence gibi referans tahmin
    p10_kw: float | None = None
    p90_kw: float | None = None


@dataclass(frozen=True)
class AccuracyScore:
    """Bir tesisin bir günlük tahmin doğruluğu."""

    plant_id: str
    day: date
    model_version: str
    sample_count: int
    capacity_kw: float
    mae_kw: float
    rmse_kw: float
    mbe_kw: float
    nmae_pct: float
    nrmse_pct: float
    nmbe_pct: float
    r2: float
    energy_actual_kwh: float
    energy_expected_kwh: float
    energy_error_pct: float
    skill_vs_reference: float | None = None
    band_coverage_pct: float | None = None
    horizon_days: int = 0

    @property
    def is_biased(self) -> bool:
        """Sistematik kayma anlamlı mı — kalibrasyon tetikleyicisi."""
        return abs(self.nmbe_pct) >= 2.0


def _safe_ratio(numerator: float, denominator: float) -> float:
    return float(numerator / denominator) if denominator else 0.0


def score_day(
    plant_id: str,
    day: date,
    pairs: Sequence[AccuracyPair],
    capacity_kw: float,
    model_version: str,
    interval_hours: float = 0.25,
    horizon_days: int = 0,
) -> AccuracyScore | None:
    """Bir günün eşleşmiş serilerinden skor üretir; veri yetersizse None.

    Gece noktaları dahil edilir — model gecede de doğru olmak zorundadır ve
    onları dışlamak nRMSE'yi yapay olarak iyileştirir. Ancak normalizasyon
    kapasiteye yapıldığı için sıfırlar metriği bozmaz, sadece seyreltir.
    """
    usable = [
        p
        for p in pairs
        if math.isfinite(p.actual_kw) and math.isfinite(p.expected_kw) and p.actual_kw >= 0.0
    ]
    if len(usable) < MIN_SCORED_SAMPLES or capacity_kw <= 0.0:
        return None

    actual = np.array([p.actual_kw for p in usable], dtype=float)
    expected = np.array([p.expected_kw for p in usable], dtype=float)
    error = expected - actual

    mae = float(np.mean(np.abs(error)))
    rmse = float(np.sqrt(np.mean(error**2)))
    mbe = float(np.mean(error))

    variance = float(np.sum((actual - actual.mean()) ** 2))
    r2 = 1.0 - _safe_ratio(float(np.sum(error**2)), variance) if variance > 0 else 0.0

    energy_actual = float(actual.sum()) * interval_hours
    energy_expected = float(expected.sum()) * interval_hours
    energy_error_pct = (
        round(_safe_ratio(energy_expected - energy_actual, energy_actual) * 100.0, 2)
        if energy_actual > 0
        else 0.0
    )

    skill: float | None = None
    reference = np.array(
        [p.reference_kw for p in usable if p.reference_kw is not None], dtype=float
    )
    if reference.size == actual.size and reference.size > 0:
        reference_rmse = float(np.sqrt(np.mean((reference - actual) ** 2)))
        if reference_rmse > 0:
            skill = round(1.0 - rmse / reference_rmse, 4)

    coverage: float | None = None
    banded = [p for p in usable if p.p10_kw is not None and p.p90_kw is not None]
    if banded:
        inside = sum(
            1
            for p in banded
            # mypy: p10/p90 yukarıdaki filtrede None olmadıkları garanti
            if p.p10_kw is not None
            and p.p90_kw is not None
            and p.p10_kw <= p.actual_kw <= p.p90_kw
        )
        coverage = round(inside / len(banded) * 100.0, 2)

    return AccuracyScore(
        plant_id=plant_id,
        day=day,
        model_version=model_version,
        sample_count=len(usable),
        capacity_kw=round(capacity_kw, 2),
        mae_kw=round(mae, 3),
        rmse_kw=round(rmse, 3),
        mbe_kw=round(mbe, 3),
        nmae_pct=round(mae / capacity_kw * 100.0, 3),
        nrmse_pct=round(rmse / capacity_kw * 100.0, 3),
        nmbe_pct=round(mbe / capacity_kw * 100.0, 3),
        r2=round(r2, 4),
        energy_actual_kwh=round(energy_actual, 2),
        energy_expected_kwh=round(energy_expected, 2),
        energy_error_pct=energy_error_pct,
        skill_vs_reference=skill,
        band_coverage_pct=coverage,
        horizon_days=horizon_days,
    )


def align_series(
    actual: dict[datetime, float],
    expected: dict[datetime, float],
    reference: dict[datetime, float] | None = None,
    band: dict[datetime, tuple[float, float]] | None = None,
) -> list[AccuracyPair]:
    """İki (veya dört) seriyi ortak zaman damgaları üzerinden eşler."""
    shared = sorted(actual.keys() & expected.keys())
    pairs: list[AccuracyPair] = []
    for ts in shared:
        low_high = (band or {}).get(ts)
        pairs.append(
            AccuracyPair(
                ts=ts,
                actual_kw=actual[ts],
                expected_kw=expected[ts],
                reference_kw=(reference or {}).get(ts),
                p10_kw=low_high[0] if low_high else None,
                p90_kw=low_high[1] if low_high else None,
            )
        )
    return pairs


def persistence_reference(
    previous_day_actual: dict[datetime, float], offset_days: int = 1
) -> dict[datetime, float]:
    """Dünün gerçek üretimini bugüne kaydırarak naif referans tahmin üretir."""
    from datetime import timedelta

    shift = timedelta(days=offset_days)
    return {ts + shift: value for ts, value in previous_day_actual.items()}
