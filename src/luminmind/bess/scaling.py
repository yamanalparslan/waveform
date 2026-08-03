"""Hücre → paket → konteyner ölçekleme (PLAN.md Faz 4).

Elektriksel ölçekleme kuralları (S seri × P paralel):
- Gerilim: ×S, Kapasite (Ah): ×P, Enerji: ×S×P
- Direnç: R_toplam = R_hücre × S / P (RC sabiti τ korunur: C ×P/S)

NOT: Termal davranış ve hücre dengesizliği birebir ölçeklenmez (PLAN.md risk #5);
buradaki değerler mühendislik yaklaşımıdır ve saha verisiyle revize edilecektir.
Bunu görünür kılmak için ölçekli parametrelerde `derating` çarpanı taşınır.
"""

import math
from dataclasses import dataclass

from luminmind.bess.models import CellParams


@dataclass(frozen=True)
class ScaledBattery:
    """S×P ölçeklenmiş batarya (paket, rack veya konteyner)."""

    series: int
    parallel: int
    capacity_ah: float
    nominal_voltage_v: float
    energy_kwh: float
    r0_ohm: float
    r1_ohm: float
    c1_farad: float
    derating: float  # MW ölçeğinde dengesizlik/termal pay (1.0 = paysız)

    @property
    def usable_energy_kwh(self) -> float:
        return self.energy_kwh * self.derating


def scale_cell(
    cell: CellParams, series: int, parallel: int, derating: float = 1.0
) -> ScaledBattery:
    if series < 1 or parallel < 1:
        raise ValueError("series and parallel must be >= 1")
    nominal_v = float(cell.ocv.voltage(0.5)) * series
    capacity_ah = cell.capacity_ah * parallel
    return ScaledBattery(
        series=series,
        parallel=parallel,
        capacity_ah=capacity_ah,
        nominal_voltage_v=nominal_v,
        energy_kwh=nominal_v * capacity_ah / 1000.0,
        r0_ohm=cell.r0_ohm * series / parallel,
        r1_ohm=cell.r1_ohm * series / parallel,
        c1_farad=cell.c1_farad * parallel / series,
        derating=derating,
    )


def rd_bench_pack(cell: CellParams) -> ScaledBattery:
    """Masaüstü Ar-Ge düzeneği: 8S1P 21700 (kalibrasyon kaynağı)."""
    return scale_cell(cell, series=8, parallel=1)


def design_container(
    cell: CellParams,
    target_energy_kwh: float,
    target_dc_voltage_v: float,
    derating: float = 0.92,
) -> ScaledBattery:
    """Hedef enerji ve DC bara gerilimi için S×P konfigürasyonu türetir.

    `derating` MW ölçeğindeki hücre dengesizliği/termal payı temsil eder
    (varsayılan %8 — saha kalibrasyonuyla güncellenir).
    """
    if target_energy_kwh <= 0 or target_dc_voltage_v <= 0:
        raise ValueError("targets must be positive")
    cell_v = float(cell.ocv.voltage(0.5))
    series = max(1, round(target_dc_voltage_v / cell_v))
    cell_energy_kwh = cell_v * cell.capacity_ah / 1000.0
    parallel = max(1, math.ceil(target_energy_kwh / (cell_energy_kwh * series * derating)))
    return scale_cell(cell, series=series, parallel=parallel, derating=derating)
