"""Beklenen (teorik) üretim hesabı: hava verisi + pvlib modeli → TwinPoint serisi."""

from dataclasses import dataclass, field

import pandas as pd

from luminmind.core.schemas import TwinPoint
from luminmind.twin.components import LossChain
from luminmind.twin.plant_model import ArrayConfig, build_model_chain
from luminmind.twin.weather import WeatherSample


@dataclass(frozen=True)
class TwinPlantConfig:
    """Bir tesisin dijital ikiz girdileri (Postgres plants + pv_arrays'ten kurulur)."""

    plant_id: str
    latitude: float
    longitude: float
    arrays: list[ArrayConfig]
    losses: LossChain = field(default_factory=LossChain)


def weather_to_frame(samples: list[WeatherSample]) -> pd.DataFrame:
    """WeatherSample listesini pvlib'in beklediği DataFrame'e çevirir."""
    index = pd.DatetimeIndex([s.ts for s in samples], tz="UTC")
    return pd.DataFrame(
        {
            "ghi": [s.ghi_wm2 for s in samples],
            "dni": [s.dni_wm2 for s in samples],
            "dhi": [s.dhi_wm2 for s in samples],
            "temp_air": [s.temp_c for s in samples],
            "wind_speed": [s.wind_ms for s in samples],
        },
        index=index,
    )


def expected_generation(config: TwinPlantConfig, weather: pd.DataFrame) -> list[TwinPoint]:
    """Tesisin tüm dizileri için beklenen AC üretimi (sayaç noktasında, kW)."""
    if weather.empty:
        return []
    total_ac_kw = pd.Series(0.0, index=weather.index)
    poa_sum = pd.Series(0.0, index=weather.index)
    cell_temp_sum = pd.Series(0.0, index=weather.index)

    for array in config.arrays:
        chain = build_model_chain(array, config.latitude, config.longitude)
        chain.run_model(weather)
        ac_w = chain.results.ac.fillna(0.0).clip(lower=0.0)
        total_ac_kw = total_ac_kw + ac_w / 1000.0
        poa_sum = poa_sum + chain.results.total_irrad["poa_global"].fillna(0.0)
        cell_temp = chain.results.cell_temperature
        cell_temp_sum = cell_temp_sum + cell_temp.fillna(weather["temp_air"])

    net_ac_kw = config.losses.apply(total_ac_kw)
    array_count = len(config.arrays)
    return [
        TwinPoint(
            plant_id=config.plant_id,
            ts=ts.to_pydatetime(),
            expected_ac_kw=round(float(net_ac_kw.loc[ts]), 3),
            poa_irradiance_wm2=round(float(poa_sum.loc[ts]) / array_count, 1),
            cell_temp_c=round(float(cell_temp_sum.loc[ts]) / array_count, 1),
        )
        for ts in weather.index
    ]
