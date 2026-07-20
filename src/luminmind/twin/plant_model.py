"""pvlib tabanlı tesis elektrik modeli.

Panel datasheet parametreleri (`pv_arrays.module_params`) PVWatts DC modeline
eşlenir — jenerik ve az parametreli olduğundan datasheet'i henüz elimizde
olmayan tesisler için de çalışır. Gerçek modül/invertör parametreleri geldiğinde
yalnızca `ArrayConfig` alanları doldurulur (PLAN.md açık soru #3).
"""

import math
from dataclasses import dataclass

from pvlib.location import Location
from pvlib.modelchain import ModelChain
from pvlib.pvsystem import PVSystem
from pvlib.temperature import TEMPERATURE_MODEL_PARAMETERS


@dataclass(frozen=True)
class ArrayConfig:
    """Bir PV dizisinin modele giren parametreleri (pv_arrays satırıyla eşleşir)."""

    tilt_deg: float
    azimuth_deg: float  # 180 = güney
    modules_per_string: int
    strings: int
    module_pdc0_w: float = 550.0  # STC modül gücü
    gamma_pdc: float = -0.0035  # sıcaklık katsayısı (1/°C)
    inverter_eta_nom: float = 0.96

    @property
    def dc_capacity_w(self) -> float:
        return self.module_pdc0_w * self.modules_per_string * self.strings


def default_array_for_capacity(
    dc_capacity_kwp: float, tilt_deg: float = 25.0, azimuth_deg: float = 180.0
) -> ArrayConfig:
    """Datasheet'i bilinmeyen tesis için kapasiteden jenerik dizi türetir."""
    total_modules = max(1, round(dc_capacity_kwp * 1000 / 550.0))
    modules_per_string = min(25, total_modules)
    strings = max(1, math.ceil(total_modules / modules_per_string))
    return ArrayConfig(
        tilt_deg=tilt_deg,
        azimuth_deg=azimuth_deg,
        modules_per_string=modules_per_string,
        strings=strings,
    )


def build_model_chain(config: ArrayConfig, latitude: float, longitude: float) -> ModelChain:
    temperature_params = TEMPERATURE_MODEL_PARAMETERS["sapm"]["open_rack_glass_glass"]
    system = PVSystem(
        surface_tilt=config.tilt_deg,
        surface_azimuth=config.azimuth_deg,
        module_parameters={"pdc0": config.module_pdc0_w, "gamma_pdc": config.gamma_pdc},
        inverter_parameters={
            "pdc0": config.dc_capacity_w,
            "eta_inv_nom": config.inverter_eta_nom,
        },
        modules_per_string=config.modules_per_string,
        strings_per_inverter=config.strings,
        temperature_model_parameters=temperature_params,
    )
    location = Location(latitude=latitude, longitude=longitude, tz="UTC")
    return ModelChain(
        system,
        location,
        aoi_model="physical",
        spectral_model="no_loss",
    )
