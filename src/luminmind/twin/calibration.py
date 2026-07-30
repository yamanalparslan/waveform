"""Dijital ikizin tesis bazlı kalibrasyonu — açık döngüden kapalı döngüye.

Fizik modeli jeneriktir: gerçek tesisin yönelim hatası, ufuk engeli, gerçek
kablo kesiti, trafo verimi, modül yaşı ve kalıcı kirliliği bilinmez. Bu
bilinmeyenler toplamda birkaç ila on puanlık **sistematik** sapma üretir ve
sistematik sapma, sapma tabanlı anomali tespitinde doğrudan sahte alarma
dönüşür. Kalibrasyon bu sistematik bileşeni ölçüp modele geri besler.

Üç katman, artan çözünürlük sırasıyla:

1. **Ölçek (`scale`)** — tüm seriyi çarpan tek katsayı. Kapasite hatası,
   kalıcı kayıp farkı, etiket toleransı burada toplanır.
2. **Saatlik bias (`hour_bias`)** — ölçek düzeltmesinden sonra günün saatine
   göre kalan desen. Yönelim hatası ve ufuk/sıra gölgesi burada görünür.
3. **Kirlilik tabanı (`soiling_base_ratio`)** — yağış modeliyle açıklanamayan
   kalıcı kirlilik seviyesi.

**Kalibrasyon arızayı yutmamalıdır.** Üç koruma var: (a) katsayılar dar
bantlara sıkıştırılır, (b) açık anomali pencereleri fit'ten dışlanabilir,
(c) yeni tahmin eskisiyle üstel olarak harmanlanır (`learning_rate`), yani tek
bir kötü gün durumu savuramaz.
"""

import json
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

CALIBRATION_VERSION = "calib-v1"


@dataclass(frozen=True)
class CalibrationConfig:
    """Fit'in kabul kriterleri ve güvenlik bantları."""

    min_expected_fraction: float = 0.15  # kapasitenin %15'i altındaki noktalar elenir
    min_samples: int = 96  # ~1 tam günlük gündüz verisi
    min_samples_per_hour: int = 8
    ratio_hard_limits: tuple[float, float] = (0.30, 1.60)  # ham oran ön elemesi
    scale_limits: tuple[float, float] = (0.70, 1.30)
    hour_bias_limits: tuple[float, float] = (0.80, 1.20)
    mad_sigma: float = 3.0  # medyan ± σ·MAD dışındaki oranlar atılır
    learning_rate: float = 0.35  # yeni fit'in ağırlığı (0 = donuk, 1 = hafızasız)


@dataclass(frozen=True)
class CalibrationState:
    """Bir tesisin geçerli kalibrasyonu; JSON olarak Postgres'te saklanır."""

    plant_id: str
    scale: float = 1.0
    hour_bias: dict[int, float] = field(default_factory=dict)
    soiling_base_ratio: float = 1.0
    sample_count: int = 0
    fitted_at: datetime | None = None
    quality: dict[str, float] = field(default_factory=dict)
    version: str = CALIBRATION_VERSION

    @property
    def is_identity(self) -> bool:
        return self.scale == 1.0 and not self.hour_bias

    def factor_at(self, ts: datetime) -> float:
        """Tek zaman damgası için toplam düzeltme katsayısı."""
        return self.scale * self.hour_bias.get(ts.astimezone(UTC).hour, 1.0)

    def apply(self, series: pd.Series) -> pd.Series:
        """Beklenen üretim serisine ölçek + saatlik bias uygular."""
        if self.is_identity:
            return series
        index = pd.DatetimeIndex(series.index)
        hours = index.tz_convert("UTC").hour if index.tz is not None else index.hour
        bias = np.array([self.hour_bias.get(int(h), 1.0) for h in hours], dtype=float)
        return series * self.scale * bias

    def to_json(self) -> dict[str, Any]:
        return {
            "scale": self.scale,
            "hour_bias": {str(h): v for h, v in sorted(self.hour_bias.items())},
            "soiling_base_ratio": self.soiling_base_ratio,
            "sample_count": self.sample_count,
            "fitted_at": self.fitted_at.isoformat() if self.fitted_at else None,
            "quality": self.quality,
            "version": self.version,
        }

    @classmethod
    def from_json(cls, plant_id: str, payload: dict[str, Any] | str | None) -> "CalibrationState":
        if not payload:
            return cls(plant_id=plant_id)
        data = json.loads(payload) if isinstance(payload, str) else payload
        fitted_raw = data.get("fitted_at")
        return cls(
            plant_id=plant_id,
            scale=float(data.get("scale", 1.0)),
            hour_bias={int(h): float(v) for h, v in (data.get("hour_bias") or {}).items()},
            soiling_base_ratio=float(data.get("soiling_base_ratio", 1.0)),
            sample_count=int(data.get("sample_count", 0)),
            fitted_at=datetime.fromisoformat(fitted_raw) if fitted_raw else None,
            quality={k: float(v) for k, v in (data.get("quality") or {}).items()},
            version=str(data.get("version", CALIBRATION_VERSION)),
        )


@dataclass(frozen=True)
class CalibrationSample:
    ts: datetime
    actual_kw: float
    expected_kw: float


def _clamp(value: float, limits: tuple[float, float]) -> float:
    return float(min(max(value, limits[0]), limits[1]))


def _blend(previous: float, current: float, rate: float) -> float:
    return float(previous * (1.0 - rate) + current * rate)


def _robust_ratios(
    samples: Sequence[CalibrationSample], config: CalibrationConfig
) -> tuple[np.ndarray, np.ndarray]:
    """Oran dizisi ve saat dizisi; aykırı değerler MAD ile elenmiş."""
    ratios = np.array([s.actual_kw / s.expected_kw for s in samples], dtype=float)
    hours = np.array([s.ts.astimezone(UTC).hour for s in samples], dtype=int)

    low, high = config.ratio_hard_limits
    keep = np.isfinite(ratios) & (ratios >= low) & (ratios <= high)
    ratios, hours = ratios[keep], hours[keep]
    if ratios.size == 0:
        return ratios, hours

    median = float(np.median(ratios))
    mad = float(np.median(np.abs(ratios - median)))
    if mad > 0.0:
        keep = np.abs(ratios - median) <= config.mad_sigma * mad * 1.4826
        ratios, hours = ratios[keep], hours[keep]
    return ratios, hours


def fit_calibration(
    plant_id: str,
    samples: Sequence[CalibrationSample],
    capacity_kw: float,
    previous: CalibrationState | None = None,
    config: CalibrationConfig | None = None,
    now: datetime | None = None,
) -> CalibrationState:
    """Gerçek/beklenen çiftlerinden yeni kalibrasyon durumu üretir.

    **Artımlıdır.** `expected_kw` değerlerinin `previous` uygulanmış haliyle
    geldiği varsayılır (Influx'taki beklenen seri kalibre edilmiştir). Bu yüzden
    ölçülen oran bir *kalan* hatadır ve mevcut katsayılarla çarpılarak
    birleştirilir. `previous=None` durumunda birim durumla çarpım, doğrudan
    mutlak fit'e indirgenir.

    Yeterli veri yoksa `previous` (yoksa birim durum) değiştirilmeden döner —
    az veriyle kalibre etmek, hiç kalibre etmemekten kötüdür.
    """
    config = config or CalibrationConfig()
    current = previous or CalibrationState(plant_id=plant_id)
    floor_kw = max(capacity_kw, 0.0) * config.min_expected_fraction

    usable = [
        s
        for s in samples
        if s.expected_kw >= floor_kw
        and s.expected_kw > 0.0
        and math.isfinite(s.actual_kw)
        and s.actual_kw >= 0.0
    ]
    if len(usable) < config.min_samples:
        logger.info(
            "calibration skipped plant=%s usable=%d < %d",
            plant_id,
            len(usable),
            config.min_samples,
        )
        return current

    ratios, hours = _robust_ratios(usable, config)
    if ratios.size < config.min_samples:
        logger.info(
            "calibration skipped plant=%s after outlier removal (%d)", plant_id, ratios.size
        )
        return current

    # Kalan (residual) oran: beklenen zaten kalibre olduğu için ideal değer 1,0
    residual_scale = float(np.median(ratios))
    scale = _clamp(
        _blend(current.scale, current.scale * residual_scale, config.learning_rate),
        config.scale_limits,
    )

    hourly_residual = ratios / residual_scale
    hour_medians: dict[int, float] = {}
    hour_bias: dict[int, float] = {}
    for hour in np.unique(hours):
        subset = hourly_residual[hours == hour]
        if subset.size < config.min_samples_per_hour:
            continue
        median_residual = float(np.median(subset))
        hour_medians[int(hour)] = median_residual
        previous_bias = current.hour_bias.get(int(hour), 1.0)
        hour_bias[int(hour)] = _clamp(
            _blend(previous_bias, previous_bias * median_residual, config.learning_rate),
            config.hour_bias_limits,
        )
    # Değişmeyen saatler önceki değerlerini korumalı (veri az olduğu için
    # düşen bir saat, bilgiyi kaybetmek anlamına gelmez)
    for hour, value in current.hour_bias.items():
        hour_bias.setdefault(hour, value)

    # Bu fit'in kapatabileceği hata: kalan oranın 1'e ne kadar yaklaştığı
    achievable = residual_scale * np.array(
        [hour_medians.get(int(h), 1.0) for h in hours], dtype=float
    )
    quality = {
        "residual_ratio_median": round(residual_scale, 4),
        "residual_ratio_mad": round(float(np.median(np.abs(ratios - residual_scale))), 4),
        # Kalibrasyon öncesi/sonrası ortalama mutlak oransal hata (%)
        "mape_before_pct": round(float(np.mean(np.abs(ratios - 1.0))) * 100.0, 2),
        "mape_after_pct": round(float(np.mean(np.abs(ratios / achievable - 1.0))) * 100.0, 2),
    }

    state = CalibrationState(
        plant_id=plant_id,
        scale=round(scale, 5),
        hour_bias={h: round(v, 5) for h, v in sorted(hour_bias.items())},
        soiling_base_ratio=current.soiling_base_ratio,
        sample_count=int(ratios.size),
        fitted_at=now or datetime.now(tz=UTC),
        quality=quality,
    )
    logger.info(
        "calibration fitted plant=%s scale=%.4f hours=%d mape %.2f%% → %.2f%%",
        plant_id,
        state.scale,
        len(state.hour_bias),
        quality["mape_before_pct"],
        quality["mape_after_pct"],
    )
    return state
