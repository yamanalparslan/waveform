"""Coulomb Counting SoC kestirimi.

Basit ve hızlıdır ama akım sensörü bias'ı ve başlangıç SoC hatası zamanla
birikir (EKF'nin çözdüğü problem). İşaret kuralı: deşarj pozitif.
"""

import numpy as np
from numpy.typing import NDArray

FloatArray = NDArray[np.float64]


def coulomb_soc(
    current_a: FloatArray,
    dt_s: float,
    capacity_ah: float,
    soc0: float,
    coulomb_efficiency: float = 1.0,
) -> FloatArray:
    """Akım profilinden SoC serisi üretir (şarj yönünde verim uygulanır)."""
    effective = np.where(current_a >= 0, current_a, current_a * coulomb_efficiency)
    delta = np.cumsum(effective) * dt_s / (3600.0 * capacity_ah)
    return np.clip(soc0 - delta, 0.0, 1.0).astype(np.float64)
