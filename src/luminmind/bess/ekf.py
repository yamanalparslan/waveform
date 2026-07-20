"""Genişletilmiş Kalman Filtresi (EKF) ile SoC kestirimi.

Durum:  x = [SoC, V_rc1]  (1-RC Thevenin modeli)
Ölçüm:  V_terminal = OCV(SoC) - R0·I - V_rc1
Öngörü: SoC_k+1 = SoC_k - η·I·dt/(3600·Q)
        V1_k+1  = α·V1_k + R1·(1-α)·I,  α = exp(-dt/τ)

Coulomb Counting'in aksine gerilim ölçümü üzerinden geri besleme aldığı için
başlangıç SoC hatasını ve akım sensörü bias'ını telafi eder. Deşarj pozitif.
"""

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from luminmind.bess.models import CellParams

FloatArray = NDArray[np.float64]


@dataclass(frozen=True)
class EkfConfig:
    process_noise_soc: float = 1e-10  # SoC süreç gürültüsü (adım başına varyans)
    process_noise_v1: float = 1e-6  # RC gerilimi süreç gürültüsü
    measurement_noise_v: float = 1e-4  # gerilim ölçüm varyansı (~10 mV std)
    initial_soc_variance: float = 0.05  # başlangıç SoC belirsizliği
    initial_v1_variance: float = 1e-4


def run_ekf(
    cell: CellParams,
    current_a: FloatArray,
    voltage_v: FloatArray,
    dt_s: float,
    soc0_guess: float,
    config: EkfConfig | None = None,
) -> FloatArray:
    """Akım + terminal gerilimi ölçümlerinden SoC kestirim serisi üretir."""
    if len(current_a) != len(voltage_v):
        raise ValueError("current and voltage series must have equal length")
    config = config or EkfConfig()

    alpha = float(np.exp(-dt_s / cell.tau_s))
    transition = np.array([[1.0, 0.0], [0.0, alpha]])
    process_noise = np.diag([config.process_noise_soc, config.process_noise_v1])

    state = np.array([soc0_guess, 0.0])
    covariance = np.diag([config.initial_soc_variance, config.initial_v1_variance])
    identity = np.eye(2)

    estimates = np.empty(len(current_a), dtype=np.float64)
    for k in range(len(current_a)):
        i = float(current_a[k])
        # Öngörü
        effective = i if i >= 0 else i * cell.coulomb_efficiency
        state = np.array(
            [
                state[0] - effective * dt_s / (3600.0 * cell.capacity_ah),
                alpha * state[1] + cell.r1_ohm * (1.0 - alpha) * i,
            ]
        )
        covariance = transition @ covariance @ transition.T + process_noise

        # Güncelleme
        soc_pred = float(np.clip(state[0], 0.0, 1.0))
        predicted_v = float(cell.ocv.voltage(soc_pred)) - cell.r0_ohm * i - state[1]
        innovation = float(voltage_v[k]) - predicted_v
        jacobian = np.array([cell.ocv.derivative(soc_pred), -1.0])
        innovation_var = float(
            jacobian @ covariance @ jacobian + config.measurement_noise_v
        )
        gain = (covariance @ jacobian) / innovation_var
        state = state + gain * innovation
        covariance = (identity - np.outer(gain, jacobian)) @ covariance

        state[0] = float(np.clip(state[0], 0.0, 1.0))
        estimates[k] = state[0]
    return estimates
