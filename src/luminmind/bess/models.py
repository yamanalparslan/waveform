"""BESS hücre modeli veri sınıfları.

Eşdeğer devre: 1-RC Thevenin modeli — OCV(SoC) kaynağı + seri R0 + (R1‖C1).
EKF durumu [SoC, V_rc1], ölçüm V_terminal (PLAN.md Faz 4). Varsayılan parametreler
tipik bir NMC 21700 hücreyi temsil eder; Ar-Ge düzeneğinden gelen gerçek CSV'lerle
`calibration.py` bu parametreleri tesise özel değerlerle günceller.

İşaret kuralı: deşarj akımı pozitif.
"""

from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray


@dataclass(frozen=True)
class OcvCurve:
    """SoC → açık devre gerilimi (OCV) eğrisi; lineer interpolasyonla değerlendirilir."""

    soc_points: tuple[float, ...]
    voltage_points: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.soc_points) != len(self.voltage_points):
            raise ValueError("soc_points and voltage_points must have equal length")
        if list(self.soc_points) != sorted(self.soc_points):
            raise ValueError("soc_points must be increasing")

    def voltage(self, soc: float | NDArray[np.float64]) -> float | NDArray[np.float64]:
        result = np.interp(soc, self.soc_points, self.voltage_points)
        return float(result) if np.isscalar(soc) else result

    def derivative(self, soc: float) -> float:
        """dOCV/dSoC (EKF ölçüm Jacobian'ı için), merkezi fark."""
        eps = 1e-3
        low = float(np.interp(max(soc - eps, 0.0), self.soc_points, self.voltage_points))
        high = float(np.interp(min(soc + eps, 1.0), self.soc_points, self.voltage_points))
        return (high - low) / (min(soc + eps, 1.0) - max(soc - eps, 0.0))


def default_nmc_21700_ocv() -> OcvCurve:
    """Tipik NMC 21700 OCV eğrisi (datasheet yaklaşıklaması; kalibrasyonla değişir)."""
    return OcvCurve(
        soc_points=(0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
        voltage_points=(3.00, 3.30, 3.43, 3.52, 3.58, 3.63, 3.69, 3.76, 3.86, 3.99, 4.15),
    )


@dataclass(frozen=True)
class CellParams:
    """1-RC Thevenin hücre parametreleri."""

    capacity_ah: float = 5.0  # tipik 21700 (ör. 5000 mAh)
    r0_ohm: float = 0.020  # seri iç direnç
    r1_ohm: float = 0.015  # RC kolu direnci
    c1_farad: float = 2000.0  # RC kolu kapasitansı
    coulomb_efficiency: float = 0.995  # şarj yönünde
    ocv: OcvCurve = field(default_factory=default_nmc_21700_ocv)

    @property
    def tau_s(self) -> float:
        return self.r1_ohm * self.c1_farad
