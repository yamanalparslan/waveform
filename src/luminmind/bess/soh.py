"""SoH (State of Health) kestirimi.

İki bağımsız gösterge:
- Kapasite SoH'u: iki dinlenme noktası arasındaki aktarılan yük / SoC farkından
  efektif kapasite; nominale oranı.
- Direnç SoH'u: akım basamağındaki ani gerilim düşümünden R0 kestirimi; iç direnç
  artışı yaşlanma göstergesidir (SoH_r = R0_nom / R0_est).
"""

import numpy as np
from numpy.typing import NDArray

FloatArray = NDArray[np.float64]


def estimate_capacity_ah(
    current_a: FloatArray, dt_s: float, soc_start: float, soc_end: float
) -> float:
    """İki (dinlenmede OCV'den okunan) SoC noktası arasındaki yükten kapasite kestirir."""
    delta_soc = soc_start - soc_end
    if abs(delta_soc) < 1e-6:
        raise ValueError("SoC window too small for capacity estimation")
    charge_ah = float(np.sum(current_a)) * dt_s / 3600.0
    return charge_ah / delta_soc


def capacity_soh(estimated_capacity_ah: float, nominal_capacity_ah: float) -> float:
    return max(0.0, min(1.5, estimated_capacity_ah / nominal_capacity_ah))


def estimate_r0_from_step(
    voltage_v: FloatArray, current_a: FloatArray, step_index: int
) -> float:
    """Akım basamağı anındaki ΔV/ΔI oranından seri direnci kestirir."""
    delta_i = float(current_a[step_index] - current_a[step_index - 1])
    if abs(delta_i) < 1e-6:
        raise ValueError("no current step at given index")
    delta_v = float(voltage_v[step_index] - voltage_v[step_index - 1])
    return -delta_v / delta_i


def resistance_soh(estimated_r0_ohm: float, nominal_r0_ohm: float) -> float:
    if estimated_r0_ohm <= 0:
        raise ValueError("estimated resistance must be positive")
    return max(0.0, min(1.5, nominal_r0_ohm / estimated_r0_ohm))
