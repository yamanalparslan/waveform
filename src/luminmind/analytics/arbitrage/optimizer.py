"""LP tabanlı şarj/deşarj zamanlama optimizasyonu (PLAN.md Faz 5: basit LP ile başla).

Karar değişkenleri: her saat için şarj gücü c_t ve deşarj gücü d_t (kW, ≥0).
Amaç: gelir maksimizasyonu  max Σ p_t·(d_t − c_t)·h / 1000  (TRY)
Kısıtlar:
- 0 ≤ c_t, d_t ≤ P_max
- SoC sınırları: E_min ≤ E_0 + Σ_{τ≤t}(η_c·c_τ − d_τ/η_d)·h ≤ E_max  (her t)
- Gün sonu enerjisi başlangıcın altına düşemez (E_T ≥ E_0)
- Günlük çevrim limiti: Σ d_t·h ≤ max_cycles·(E_max − E_min)

Verim < 1 olduğu için eşzamanlı şarj+deşarj LP optimumunda kendiliğinden oluşmaz.
`scipy.optimize.linprog` (HiGHS) kullanılır.
"""

from dataclasses import dataclass
from datetime import datetime

import numpy as np
from scipy.optimize import linprog

from luminmind.analytics.arbitrage.epias import PriceSlot

ACTION_CHARGE = "charge"
ACTION_DISCHARGE = "discharge"
ACTION_IDLE = "idle"

_POWER_TOL_KW = 1e-3


@dataclass(frozen=True)
class BatterySpec:
    energy_kwh: float
    power_kw: float
    soc_min: float = 0.10
    soc_max: float = 0.90
    soc_initial: float = 0.50
    eta_charge: float = 0.95
    eta_discharge: float = 0.95
    max_cycles_per_day: float = 2.0


@dataclass(frozen=True)
class ScheduleSlot:
    start: datetime
    action: str
    power_kw: float
    price_try_mwh: float


@dataclass(frozen=True)
class ArbitrageResult:
    slots: list[ScheduleSlot]
    expected_revenue_try: float


def optimize_day(
    prices: list[PriceSlot], battery: BatterySpec, slot_hours: float = 1.0
) -> ArbitrageResult:
    """Verilen fiyat serisine göre günlük şarj/deşarj planı üretir."""
    if not prices:
        return ArbitrageResult(slots=[], expected_revenue_try=0.0)
    steps = len(prices)
    price_arr = np.array([p.price_try_mwh for p in prices])

    # x = [c_1..c_T, d_1..d_T]; linprog minimize ettiği için gelir negatif yazılır
    objective = np.concatenate([price_arr, -price_arr]) * slot_hours / 1000.0

    lower_tri = np.tril(np.ones((steps, steps)))
    charge_effect = battery.eta_charge * slot_hours * lower_tri
    discharge_effect = -slot_hours / battery.eta_discharge * lower_tri

    e0 = battery.soc_initial * battery.energy_kwh
    e_min = battery.soc_min * battery.energy_kwh
    e_max = battery.soc_max * battery.energy_kwh

    # E_t ≤ E_max  →  [A_c, A_d]·x ≤ E_max − E_0
    upper_block = np.hstack([charge_effect, discharge_effect])
    upper_rhs = np.full(steps, e_max - e0)
    # E_t ≥ E_min  →  −[A_c, A_d]·x ≤ E_0 − E_min
    lower_block = -upper_block
    lower_rhs = np.full(steps, e0 - e_min)
    # E_T ≥ E_0  →  −[A_c, A_d]·x (son satır) ≤ 0
    end_row = -upper_block[-1:]
    end_rhs = np.array([0.0])
    # çevrim limiti: Σ d_t·h ≤ max_cycles·(E_max − E_min)
    cycle_row = np.concatenate([np.zeros(steps), np.full(steps, slot_hours)])[None, :]
    cycle_rhs = np.array([battery.max_cycles_per_day * (e_max - e_min)])

    result = linprog(
        c=objective,
        A_ub=np.vstack([upper_block, lower_block, end_row, cycle_row]),
        b_ub=np.concatenate([upper_rhs, lower_rhs, end_rhs, cycle_rhs]),
        bounds=[(0.0, battery.power_kw)] * (2 * steps),
        method="highs",
    )
    if not result.success:
        raise RuntimeError(f"arbitrage LP failed: {result.message}")

    charge = result.x[:steps]
    discharge = result.x[steps:]
    slots: list[ScheduleSlot] = []
    for k, price in enumerate(prices):
        net = float(discharge[k] - charge[k])
        if net > _POWER_TOL_KW:
            action, power = ACTION_DISCHARGE, net
        elif net < -_POWER_TOL_KW:
            action, power = ACTION_CHARGE, -net
        else:
            action, power = ACTION_IDLE, 0.0
        slots.append(
            ScheduleSlot(
                start=price.start,
                action=action,
                power_kw=round(power, 3),
                price_try_mwh=price.price_try_mwh,
            )
        )
    return ArbitrageResult(slots=slots, expected_revenue_try=round(float(-result.fun), 2))
