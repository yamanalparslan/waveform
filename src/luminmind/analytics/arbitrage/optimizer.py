"""LP tabanlı şarj/deşarj ve PV yönlendirme optimizasyonu.

Önceki sürüm yalnızca fiyat ile bataryayı görüyordu: PV üretimi, şebeke
bağlantı limiti ve kırpılan enerji modelde yoktu. Oysa bir GES+BESS tesisinde
paranın büyük kısmı tam da oradadır — öğlen bağlantı limitini aşan üretim
bedavaya kırpılırken, akşam pahalı saatte satılabilecek enerji kaybedilir.
Bataryayı yalnızca fiyat farkına göre çalıştırmak, tesisin en kârlı hamlesini
görmemek demektir.

Karar değişkenleri (her t slotu için, kW, hepsi ≥ 0):

- `c_t` — şebekeden batarya şarjı
- `s_t` — PV'den batarya şarjı (kırpılacak enerjinin kurtarılması)
- `d_t` — batarya deşarjı (şebekeye)
- `g_t` — PV'den doğrudan şebekeye

Amaç:  max Σ (p_t·d_t + q_t·g_t − p_t·c_t)·h / 1000   [TRY]
  `p_t` piyasa (GÖP) fiyatı; `q_t` PV satış fiyatı — sabit alım garantisi
  (feed-in) varsa o, yoksa piyasa fiyatı.

Kısıtlar:
- Batarya gücü: c_t + s_t ≤ P_max,  d_t ≤ P_max
- PV dengesi: s_t + g_t ≤ pv_t  (fark = kırpılan enerji)
- Şebeke bağlantı limiti: d_t + g_t ≤ P_grid  ve  c_t ≤ P_grid
- SoC sınırları: E_min ≤ E_0 + Σ_{τ≤t}(η_c·(c_τ+s_τ) − d_τ/η_d)·h ≤ E_max
- Gün sonu enerjisi başlangıcın altına düşemez (E_T ≥ E_0)
- Günlük çevrim limiti: Σ d_t·h ≤ max_cycles·(E_max − E_min)

Verim < 1 olduğu için eşzamanlı şarj+deşarj LP optimumunda kendiliğinden
oluşmaz. `scipy.optimize.linprog` (HiGHS) kullanılır.
"""

from collections.abc import Sequence
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
class SiteSpec:
    """Bataryanın bağlı olduğu tesisin enerji ortamı.

    `pv_forecast_kw` dijital ikizin D+1 tahminidir. Boşsa saf fiyat arbitrajı
    yapılır (eski davranış).
    """

    pv_forecast_kw: Sequence[float] | None = None
    grid_limit_kw: float | None = None  # bağlantı anlaşması sınırı (ihracat/ithalat)
    feed_in_try_mwh: float | None = None  # PV için sabit alım fiyatı; yoksa piyasa
    allow_grid_charge: bool = True  # şebekeden şarja izin var mı

    def prices_for_pv(self, market: np.ndarray) -> np.ndarray:
        if self.feed_in_try_mwh is None:
            return market
        return np.full_like(market, float(self.feed_in_try_mwh))


@dataclass(frozen=True)
class ScheduleSlot:
    start: datetime
    action: str
    power_kw: float  # net batarya gücü (deşarj +, şarj −'in mutlak değeri)
    price_try_mwh: float
    pv_to_battery_kw: float = 0.0
    pv_export_kw: float = 0.0
    grid_charge_kw: float = 0.0
    curtailed_kw: float = 0.0


@dataclass(frozen=True)
class ArbitrageResult:
    slots: list[ScheduleSlot]
    expected_revenue_try: float
    battery_revenue_try: float = 0.0
    pv_revenue_try: float = 0.0
    curtailed_kwh: float = 0.0
    recovered_kwh: float = 0.0  # kırpılmaktan kurtarılıp bataryaya alınan enerji


def _pv_array(site: SiteSpec, steps: int) -> np.ndarray:
    if site.pv_forecast_kw is None:
        return np.zeros(steps)
    values = np.array(list(site.pv_forecast_kw)[:steps], dtype=float)
    if values.size < steps:
        values = np.concatenate([values, np.zeros(steps - values.size)])
    cleaned: np.ndarray = np.clip(np.nan_to_num(values, nan=0.0), 0.0, None)
    return cleaned


def optimize_day(
    prices: list[PriceSlot],
    battery: BatterySpec,
    slot_hours: float = 1.0,
    site: SiteSpec | None = None,
) -> ArbitrageResult:
    """Verilen fiyat ve PV tahminine göre günlük plan üretir."""
    if not prices:
        return ArbitrageResult(slots=[], expected_revenue_try=0.0)

    site = site or SiteSpec()
    steps = len(prices)
    market = np.array([p.price_try_mwh for p in prices], dtype=float)
    pv_price = site.prices_for_pv(market)
    pv = _pv_array(site, steps)
    grid_limit = site.grid_limit_kw

    zeros = np.zeros((steps, steps))
    eye = np.eye(steps)
    lower_tri = np.tril(np.ones((steps, steps)))

    # x = [c (şebekeden şarj), s (PV'den şarj), d (deşarj), g (PV ihracatı)]
    # linprog minimize ettiği için gelir negatif yazılır
    unit = slot_hours / 1000.0
    objective = np.concatenate([market * unit, np.zeros(steps), -market * unit, -pv_price * unit])

    charge_effect = battery.eta_charge * slot_hours * lower_tri
    discharge_effect = -slot_hours / battery.eta_discharge * lower_tri
    soc_block = np.hstack([charge_effect, charge_effect, discharge_effect, zeros])

    e0 = battery.soc_initial * battery.energy_kwh
    e_min = battery.soc_min * battery.energy_kwh
    e_max = battery.soc_max * battery.energy_kwh

    rows = [
        soc_block,  # E_t ≤ E_max
        -soc_block,  # E_t ≥ E_min
        -soc_block[-1:],  # E_T ≥ E_0
        # çevrim limiti: Σ d_t·h ≤ max_cycles·(E_max − E_min)
        np.concatenate(
            [np.zeros(steps), np.zeros(steps), np.full(steps, slot_hours), np.zeros(steps)]
        )[None, :],
        np.hstack([eye, eye, zeros, zeros]),  # c_t + s_t ≤ P_max
        np.hstack([zeros, eye, zeros, eye]),  # s_t + g_t ≤ pv_t
    ]
    rhs = [
        np.full(steps, e_max - e0),
        np.full(steps, e0 - e_min),
        np.array([0.0]),
        np.array([battery.max_cycles_per_day * (e_max - e_min)]),
        np.full(steps, battery.power_kw),
        pv,
    ]

    if grid_limit is not None:
        rows.append(np.hstack([zeros, zeros, eye, eye]))  # d_t + g_t ≤ P_grid
        rhs.append(np.full(steps, grid_limit))
        rows.append(np.hstack([eye, zeros, zeros, zeros]))  # c_t ≤ P_grid
        rhs.append(np.full(steps, grid_limit))

    charge_bound = battery.power_kw if site.allow_grid_charge else 0.0
    export_bound = grid_limit if grid_limit is not None else None
    bounds = (
        [(0.0, charge_bound)] * steps
        + [(0.0, battery.power_kw)] * steps
        + [(0.0, battery.power_kw)] * steps
        + [(0.0, export_bound)] * steps
    )

    result = linprog(
        c=objective, A_ub=np.vstack(rows), b_ub=np.concatenate(rhs), bounds=bounds, method="highs"
    )
    if not result.success:
        raise RuntimeError(f"arbitrage LP failed: {result.message}")

    grid_charge = result.x[:steps]
    pv_charge = result.x[steps : 2 * steps]
    discharge = result.x[2 * steps : 3 * steps]
    pv_export = result.x[3 * steps :]
    curtailed = np.clip(pv - pv_charge - pv_export, 0.0, None)

    slots: list[ScheduleSlot] = []
    for k, price in enumerate(prices):
        net = float(discharge[k] - grid_charge[k] - pv_charge[k])
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
                pv_to_battery_kw=round(float(pv_charge[k]), 3),
                pv_export_kw=round(float(pv_export[k]), 3),
                grid_charge_kw=round(float(grid_charge[k]), 3),
                curtailed_kw=round(float(curtailed[k]), 3),
            )
        )

    battery_revenue = float(np.dot(market, discharge - grid_charge) * unit)
    pv_revenue = float(np.dot(pv_price, pv_export) * unit)
    return ArbitrageResult(
        slots=slots,
        expected_revenue_try=round(float(-result.fun), 2),
        battery_revenue_try=round(battery_revenue, 2),
        pv_revenue_try=round(pv_revenue, 2),
        curtailed_kwh=round(float(curtailed.sum()) * slot_hours, 2),
        recovered_kwh=round(float(pv_charge.sum()) * slot_hours, 2),
    )
