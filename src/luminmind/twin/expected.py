"""Beklenen (teorik) üretim hesabı: hava verisi + fizik zinciri → TwinPoint serisi.

Bu modül orkestrasyondur; fizik `twin/pipeline.py` içindedir. Sorumlulukları:

- Tesis konfigürasyonunu (diziler, kayıplar, kalibrasyon, kirlilik) bir arada
  tutmak,
- `WeatherSample` listesini pvlib'in beklediği çerçeveye çevirmek,
- Dizileri toplayıp kalibrasyonu uygulamak,
- Çok modelli (ensemble) çalıştırmada P10/P50/P90 bandını üretmek.

**Belirsizlik neden önemli:** tek modelli tahminde "beklenenin %12 altındayız"
cümlesi bir arıza iddiasıdır. Ensemble bandıyla aynı cümle "modeller zaten
%±15 aralığında ayrışıyordu" olabilir. Band, anomali motorunun yanlış alarm
üretmemesi için gereken tek bilgidir.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta

import numpy as np
import pandas as pd

from luminmind.core.schemas import TwinPoint
from luminmind.twin.calibration import CalibrationState
from luminmind.twin.components import LossChain
from luminmind.twin.pipeline import ChainResult, Site, run_chain
from luminmind.twin.plant_model import ArrayConfig
from luminmind.twin.soiling import SoilingConfig, constant_ratio, soiling_ratio
from luminmind.twin.weather import IrradianceStamp, WeatherSample

logger = logging.getLogger(__name__)

_WEATHER_COLUMNS = (
    "ghi",
    "dni",
    "dhi",
    "temp_air",
    "wind_speed",
    "relative_humidity",
    "precipitation",
    "pressure",
)


@dataclass(frozen=True)
class TwinPlantConfig:
    """Bir tesisin dijital ikiz girdileri (Postgres plants + pv_arrays'ten kurulur)."""

    plant_id: str
    latitude: float
    longitude: float
    arrays: list[ArrayConfig]
    losses: LossChain = field(default_factory=LossChain)
    altitude_m: float = 0.0
    calibration: CalibrationState | None = None
    soiling: SoilingConfig | None = None
    irradiance_stamp: IrradianceStamp = IrradianceStamp.INTERVAL_END

    @property
    def site(self) -> Site:
        return Site(self.latitude, self.longitude, self.altitude_m)

    @property
    def dc_capacity_kw(self) -> float:
        return sum(a.dc_capacity_w for a in self.arrays) / 1000.0

    @property
    def ac_capacity_kw(self) -> float:
        """Sayaç noktasındaki teorik tavan (kırpma seviyesi × AC kayıpları)."""
        inverter_ac = sum(a.inverter_ac_capacity_w for a in self.arrays) / 1000.0
        return inverter_ac * self.losses.ac_factor


def weather_to_frame(samples: Sequence[WeatherSample]) -> pd.DataFrame:
    """WeatherSample listesini pvlib zincirinin beklediği DataFrame'e çevirir."""
    if not samples:
        return pd.DataFrame(columns=list(_WEATHER_COLUMNS), index=pd.DatetimeIndex([], tz="UTC"))
    index = pd.DatetimeIndex([s.ts for s in samples], tz="UTC")
    return pd.DataFrame(
        {
            "ghi": [s.ghi_wm2 for s in samples],
            "dni": [s.dni_wm2 for s in samples],
            "dhi": [s.dhi_wm2 for s in samples],
            "temp_air": [s.temp_c for s in samples],
            "wind_speed": [s.wind_ms for s in samples],
            "relative_humidity": [s.relative_humidity_pct for s in samples],
            "precipitation": [s.precip_mm for s in samples],
            "pressure": [s.pressure_hpa for s in samples],
        },
        index=index,
    )


def _soiling_series(config: TwinPlantConfig, weather: pd.DataFrame) -> pd.Series | None:
    """Dinamik kirlilik serisi; yağış verisi yoksa None (statik terim kullanılır)."""
    if config.soiling is None:
        return None
    precipitation = weather.get("precipitation")
    if precipitation is None:
        return constant_ratio(weather.index, config.losses.soiling)
    return soiling_ratio(pd.to_numeric(precipitation, errors="coerce"), config.soiling)


def run_plant_chain(config: TwinPlantConfig, weather: pd.DataFrame) -> dict[str, pd.Series]:
    """Tesisin tüm dizilerini çalıştırıp toplar; kalibre edilmiş serileri döndürür."""
    interval = timedelta(minutes=15)
    if len(weather.index) > 1:
        deltas = pd.Series(weather.index).diff().dropna()
        if not deltas.empty:
            interval = deltas.median().to_pytimedelta()

    soiling = _soiling_series(config, weather)
    results: list[ChainResult] = [
        run_chain(
            array,
            config.site,
            weather,
            config.losses,
            soiling=soiling,
            stamp=config.irradiance_stamp,
            interval=interval,
        )
        for array in config.arrays
    ]

    zero = pd.Series(0.0, index=weather.index, dtype=float)
    ac_kw = sum((r.ac_w for r in results), start=zero) / 1000.0
    clipping_kw = sum((r.clipping_loss_w for r in results), start=zero.copy()) / 1000.0
    array_count = max(len(results), 1)
    poa = sum((r.poa_global for r in results), start=zero.copy()) / array_count
    cell_temp = sum((r.cell_temp_c for r in results), start=zero.copy()) / array_count

    if config.calibration is not None:
        ac_kw = config.calibration.apply(ac_kw)

    reference = results[0]
    return {
        "ac_kw": ac_kw.clip(lower=0.0),
        "clipping_kw": clipping_kw,
        "poa": poa,
        "cell_temp": cell_temp,
        "soiling": reference.soiling_ratio,
        "daytime": reference.daytime,
        "valid": reference.irradiance_valid,
    }


def expected_generation(
    config: TwinPlantConfig, weather: pd.DataFrame, horizon_days: int = 0
) -> list[TwinPoint]:
    """Tesisin tüm dizileri için beklenen AC üretimi (sayaç noktasında, kW)."""
    if weather.empty or not config.arrays:
        return []
    series = run_plant_chain(config, weather)
    return _to_points(config, weather.index, series, horizon_days=horizon_days)


def expected_generation_ensemble(
    config: TwinPlantConfig,
    members: Mapping[str, pd.DataFrame],
    horizon_days: int = 0,
) -> list[TwinPoint]:
    """Çok modelli hava tahminini çalıştırıp medyan + P10/P90 bandı üretir.

    Tek üye varsa band boş bırakılır — iki noktadan yüzdelik üretmek sahte bir
    kesinlik iddiasıdır.
    """
    frames = {name: frame for name, frame in members.items() if not frame.empty}
    if not frames:
        return []
    if len(frames) == 1:
        only = next(iter(frames.values()))
        return expected_generation(config, only, horizon_days=horizon_days)

    runs = {name: run_plant_chain(config, frame) for name, frame in frames.items()}
    index = pd.DatetimeIndex(
        sorted(set().union(*(pd.DatetimeIndex(f.index) for f in frames.values())))
    )
    matrix = pd.DataFrame(
        {name: result["ac_kw"].reindex(index) for name, result in runs.items()}, index=index
    )

    median = matrix.median(axis=1, skipna=True).fillna(0.0)
    p10 = matrix.quantile(0.10, axis=1, numeric_only=True).fillna(median)
    p90 = matrix.quantile(0.90, axis=1, numeric_only=True).fillna(median)

    reference = next(iter(runs.values()))
    aggregate = {
        "ac_kw": median,
        "clipping_kw": reference["clipping_kw"].reindex(index).fillna(0.0),
        "poa": reference["poa"].reindex(index).fillna(0.0),
        "cell_temp": reference["cell_temp"].reindex(index).ffill().bfill(),
        "soiling": reference["soiling"].reindex(index).ffill().bfill(),
        "daytime": reference["daytime"].reindex(index).fillna(False).astype(bool),
        # Bir üyede bile ışınım varsa nokta raporlanabilir
        "valid": pd.concat(
            [r["valid"].reindex(index).fillna(False) for r in runs.values()], axis=1
        ).any(axis=1),
    }
    return _to_points(config, index, aggregate, horizon_days=horizon_days, p10=p10, p90=p90)


def _round_or_none(value: float, digits: int) -> float | None:
    return None if not np.isfinite(value) else round(float(value), digits)


def _to_points(
    config: TwinPlantConfig,
    index: pd.Index,
    series: Mapping[str, pd.Series],
    horizon_days: int,
    p10: pd.Series | None = None,
    p90: pd.Series | None = None,
) -> list[TwinPoint]:
    ac_kw = series["ac_kw"]
    valid = series["valid"].astype(bool)
    points: list[TwinPoint] = []
    for ts in index:
        if not bool(valid.loc[ts]):
            # Işınım verisi yok → beklenen üretim bilinmiyor. Sıfır yazmak,
            # gerçek üretimi sınırsız pozitif sapma gibi gösterirdi.
            continue
        low = _round_or_none(float(p10.loc[ts]), 3) if p10 is not None else None
        high = _round_or_none(float(p90.loc[ts]), 3) if p90 is not None else None
        center = round(float(ac_kw.loc[ts]), 3)
        if low is not None and high is not None:
            # Medyan bandın dışına düşemez (yuvarlama kaynaklı kenar durumu)
            low, high = min(low, center), max(high, center)
        points.append(
            TwinPoint(
                plant_id=config.plant_id,
                ts=ts.to_pydatetime(),
                expected_ac_kw=center,
                expected_ac_kw_p10=low,
                expected_ac_kw_p90=high,
                poa_irradiance_wm2=_round_or_none(float(series["poa"].loc[ts]), 1),
                cell_temp_c=_round_or_none(float(series["cell_temp"].loc[ts]), 1),
                clipping_loss_kw=_round_or_none(float(series["clipping_kw"].loc[ts]), 3),
                soiling_ratio=_round_or_none(float(series["soiling"].loc[ts]), 4),
                horizon_days=horizon_days,
            )
        )
    return points
