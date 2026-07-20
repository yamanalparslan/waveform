"""Ar-Ge düzeneği CSV'lerinden hücre parametresi kalibrasyonu.

CSV sözleşmesi (PLAN.md Faz 4): başlık `t_s,voltage_v,current_a,temp_c`;
`t_s` monoton artan saniye, akım işareti deşarj pozitif. Gerçek 8S 21700
ölçümleri geldiğinde bu akış değişmeden kullanılır.

Kalibrasyon: bilinen OCV eğrisi ve kapasiteyle, ölçülen gerilime karşı 1-RC
model simülasyonunun artıklarını `scipy.optimize.least_squares` ile minimize
ederek R0/R1/C1 kestirilir.
"""

import csv
import io
from dataclasses import dataclass, replace

import numpy as np
from numpy.typing import NDArray
from scipy.optimize import least_squares

from luminmind.bess.models import CellParams
from luminmind.bess.synthetic import simulate_cell

FloatArray = NDArray[np.float64]

REQUIRED_COLUMNS = ("t_s", "voltage_v", "current_a", "temp_c")


@dataclass(frozen=True)
class CycleData:
    dt_s: float
    voltage_v: FloatArray
    current_a: FloatArray
    temp_c: FloatArray


def parse_cycle_csv(content: str) -> CycleData:
    reader = csv.DictReader(io.StringIO(content))
    missing = set(REQUIRED_COLUMNS) - set(reader.fieldnames or [])
    if missing:
        raise ValueError(f"CSV missing required columns: {sorted(missing)}")
    rows = list(reader)
    if len(rows) < 3:
        raise ValueError("CSV must contain at least 3 samples")
    t = np.array([float(r["t_s"]) for r in rows])
    dt = np.diff(t)
    if not np.all(dt > 0):
        raise ValueError("t_s must be strictly increasing")
    if not np.allclose(dt, dt[0], rtol=1e-3):
        raise ValueError("t_s must be uniformly sampled")
    return CycleData(
        dt_s=float(dt[0]),
        voltage_v=np.array([float(r["voltage_v"]) for r in rows]),
        current_a=np.array([float(r["current_a"]) for r in rows]),
        temp_c=np.array([float(r["temp_c"]) for r in rows]),
    )


@dataclass(frozen=True)
class FittedParams:
    r0_ohm: float
    r1_ohm: float
    c1_farad: float
    rmse_v: float


def fit_rc_params(
    cycle: CycleData,
    base_cell: CellParams,
    soc0: float,
    initial_guess: tuple[float, float, float] | None = None,
) -> FittedParams:
    """Ölçülen gerilime karşı R0/R1/C1 fit eder (OCV eğrisi ve kapasite sabit kabul edilir)."""
    guess = initial_guess or (base_cell.r0_ohm, base_cell.r1_ohm, base_cell.c1_farad)

    def residuals(params: FloatArray) -> FloatArray:
        candidate = replace(
            base_cell,
            r0_ohm=float(params[0]),
            r1_ohm=float(params[1]),
            c1_farad=float(params[2]),
        )
        sim = simulate_cell(candidate, cycle.current_a, dt_s=cycle.dt_s, soc0=soc0)
        return sim.terminal_voltage_v - cycle.voltage_v

    result = least_squares(
        residuals,
        x0=np.array(guess),
        bounds=([1e-4, 1e-4, 10.0], [1.0, 1.0, 1e6]),
        xtol=1e-10,
    )
    rmse = float(np.sqrt(np.mean(result.fun**2)))
    return FittedParams(
        r0_ohm=float(result.x[0]),
        r1_ohm=float(result.x[1]),
        c1_farad=float(result.x[2]),
        rmse_v=rmse,
    )
