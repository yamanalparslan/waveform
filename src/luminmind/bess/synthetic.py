"""Sentetik şarj/deşarj profili üreteci ve 1-RC hücre simülatörü.

Gerçek 8S 21700 CSV'leri gelene kadar (PLAN.md kararı) EKF/Coulomb doğrulaması
bu simülatörle yapılır: gerçek SoC bilindiği için kestirim hatası ölçülebilir.
"""

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from luminmind.bess.models import CellParams

FloatArray = NDArray[np.float64]


@dataclass(frozen=True)
class SimulationResult:
    dt_s: float
    current_a: FloatArray  # deşarj pozitif
    true_soc: FloatArray
    terminal_voltage_v: FloatArray


def step_profile(segments: list[tuple[float, float]], dt_s: float = 1.0) -> FloatArray:
    """(süre_s, akım_A) segmentlerinden akım profili üretir. Deşarj pozitif."""
    parts = [
        np.full(max(1, int(duration_s / dt_s)), current_a)
        for duration_s, current_a in segments
    ]
    return np.concatenate(parts).astype(np.float64)


def simulate_cell(
    cell: CellParams,
    current_a: FloatArray,
    dt_s: float = 1.0,
    soc0: float = 1.0,
    voltage_noise_std_v: float = 0.0,
    rng: np.random.Generator | None = None,
) -> SimulationResult:
    """1-RC modeliyle gerçek SoC ve terminal gerilimini üretir (isteğe bağlı ölçüm gürültüsü)."""
    rng = rng or np.random.default_rng(42)
    steps = len(current_a)
    soc = np.empty(steps, dtype=np.float64)
    v_term = np.empty(steps, dtype=np.float64)

    alpha = float(np.exp(-dt_s / cell.tau_s))
    current_soc = soc0
    v1 = 0.0
    for k in range(steps):
        i = float(current_a[k])
        effective = i if i >= 0 else i * cell.coulomb_efficiency
        current_soc = float(
            np.clip(current_soc - effective * dt_s / (3600.0 * cell.capacity_ah), 0.0, 1.0)
        )
        v1 = alpha * v1 + cell.r1_ohm * (1.0 - alpha) * i
        soc[k] = current_soc
        v_term[k] = float(cell.ocv.voltage(current_soc)) - cell.r0_ohm * i - v1

    if voltage_noise_std_v > 0:
        v_term = v_term + rng.normal(0.0, voltage_noise_std_v, size=steps)
    return SimulationResult(
        dt_s=dt_s, current_a=current_a, true_soc=soc, terminal_voltage_v=v_term
    )


def default_validation_profile(dt_s: float = 1.0) -> FloatArray:
    """Doğrulama profili: deşarj, dinlenme, şarj, dinlenme, derin deşarj (~2 saat)."""
    return step_profile(
        [
            (1800.0, 2.5),   # 0.5C deşarj
            (600.0, 0.0),    # dinlenme
            (1200.0, -1.5),  # 0.3C şarj
            (600.0, 0.0),    # dinlenme
            (2400.0, 4.0),   # 0.8C derin deşarj
            (600.0, 0.0),    # dinlenme
        ],
        dt_s=dt_s,
    )
