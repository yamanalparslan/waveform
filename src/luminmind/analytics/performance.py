"""IEC 61724-1 performans göstergeleri ve kayıp şelalesi (loss waterfall).

Endüstriyel PV izleme standardı IEC 61724-1'in temel metrikleri:
- **Spesifik verim (Yf)** — kWh/kWp; farklı boyuttaki tesisleri kıyaslar.
- **Kapasite faktörü (CF)** — üretim / (AC gücü × süre); %.
- **Performans oranı (PR)** — gerçek üretim / teorik (POA'ya göre nameplate) üretim.
- **Sıcaklık-düzeltmeli PR** — modül sıcaklık katsayısıyla 25 °C'ye normalize.
- **İzlenen erişilebilirlik (availability)** — beklenen üretim varken cihazın
  gerçekten ürettiği aralıkların oranı.

Kayıp şelalesi teorikten gerçeğe düşüşü kategorilere ayırır (DeepSolar/Power
Factors tarzı): Teorik → Sıcaklık kaybı → Sistem/dönüşüm kaybı → Saha kaybı
(gölgelenme/kirlilik/duruş) → Gerçek. POA ışınımı yoksa (dijital ikizsiz tesis)
yalnızca Beklenen → Saha kaybı → Gerçek katmanları hesaplanır.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

# Standart test koşulu ışınımı (W/m²) ve modül sıcaklığı (°C)
G_STC = 1000.0
T_STC = 25.0
# Tipik kristal silikon güç sıcaklık katsayısı (1/°C) — modül datasheet'i ezmezse
DEFAULT_GAMMA = -0.0035


def _energy_kwh(series: Mapping[datetime, float], interval_h: float) -> float:
    """kW serisini enerjiye (kWh) çevirir: Σ güç × aralık."""
    return sum(series.values()) * interval_h


@dataclass(frozen=True)
class PerformanceKPIs:
    """IEC 61724-1 tesis performans özeti (bir gün ya da dönem için)."""

    actual_kwh: float
    expected_kwh: float
    specific_yield: float | None       # kWh/kWp (Yf)
    capacity_factor_pct: float | None  # %
    pr_pct: float | None               # performans oranı %
    pr_temp_pct: float | None          # sıcaklık-düzeltmeli PR %
    availability_pct: float | None     # izlenen erişilebilirlik %


@dataclass(frozen=True)
class LossStage:
    """Kayıp şelalesinin tek basamağı."""

    label: str
    kwh: float          # bu basamaktaki kümülatif enerji (teorikten gerçeğe azalan)
    loss_kwh: float     # bir önceki basamaktan bu basamağa düşüş (pozitif = kayıp)
    loss_pct: float     # teorik (ilk basamak) üzerinden yüzde


def _ck(cell_temp_c: float, gamma: float) -> float:
    """Sıcaklık düzeltme faktörü Ck = 1 + γ(Tcell − 25). Sıcakta <1 (kayıp)."""
    return 1.0 + gamma * (cell_temp_c - T_STC)


def compute_kpis(
    actual: Mapping[datetime, float],
    expected: Mapping[datetime, float],
    dc_capacity_kwp: float | None,
    ac_capacity_kw: float | None,
    period_hours: float,
    actual_interval_h: float,
    expected_interval_h: float,
    poa: Mapping[datetime, float] | None = None,
    cell_temp: Mapping[datetime, float] | None = None,
    gamma: float = DEFAULT_GAMMA,
    min_expected_kw: float = 1.0,
) -> PerformanceKPIs:
    """IEC 61724-1 KPI setini hesaplar. POA/sıcaklık yoksa PR twin tabanlıdır."""
    actual_kwh = _energy_kwh(actual, actual_interval_h)
    expected_kwh = _energy_kwh(expected, expected_interval_h)

    specific_yield = (
        actual_kwh / dc_capacity_kwp if dc_capacity_kwp and dc_capacity_kwp > 0 else None
    )
    capacity_factor = None
    if ac_capacity_kw and ac_capacity_kw > 0 and period_hours > 0:
        capacity_factor = actual_kwh / (ac_capacity_kw * period_hours) * 100.0

    # PR: POA varsa enerji-tabanlı IEC PR; yoksa twin (beklenen) tabanlı.
    pr = None
    pr_temp = None
    if poa and dc_capacity_kwp and dc_capacity_kwp > 0:
        # Yr = Σ (POA/1000) × aralık  [ekiv. saat @ 1000 W/m²]
        ref_yield = sum(v / G_STC for v in poa.values()) * expected_interval_h
        theoretical_kwh = dc_capacity_kwp * ref_yield
        if theoretical_kwh > 0:
            pr = actual_kwh / theoretical_kwh * 100.0
        if cell_temp:
            # Sıcaklık-düzeltmeli teorik: Σ (POA/1000)·Ck·aralık × kWp
            corr = 0.0
            for ts, g in poa.items():
                tc = cell_temp.get(ts)
                ck = _ck(tc, gamma) if tc is not None else 1.0
                corr += (g / G_STC) * ck
            theo_temp_kwh = dc_capacity_kwp * corr * expected_interval_h
            if theo_temp_kwh > 0:
                pr_temp = actual_kwh / theo_temp_kwh * 100.0
    elif expected_kwh > 0:
        pr = actual_kwh / expected_kwh * 100.0

    # İzlenen erişilebilirlik: beklenen > eşik olan aralıklarda gerçek üretim var mı
    daylight = [ts for ts, v in expected.items() if v >= min_expected_kw]
    availability = None
    if daylight:
        produced = sum(1 for ts in daylight if actual.get(ts, 0.0) > 0.1)
        availability = produced / len(daylight) * 100.0

    return PerformanceKPIs(
        actual_kwh=actual_kwh,
        expected_kwh=expected_kwh,
        specific_yield=specific_yield,
        capacity_factor_pct=capacity_factor,
        pr_pct=min(pr, 200.0) if pr is not None else None,
        pr_temp_pct=min(pr_temp, 200.0) if pr_temp is not None else None,
        availability_pct=availability,
    )


def compute_loss_waterfall(
    actual: Mapping[datetime, float],
    expected: Mapping[datetime, float],
    dc_capacity_kwp: float | None,
    actual_interval_h: float,
    expected_interval_h: float,
    poa: Mapping[datetime, float] | None = None,
    cell_temp: Mapping[datetime, float] | None = None,
    gamma: float = DEFAULT_GAMMA,
) -> list[LossStage]:
    """Teorikten gerçeğe kayıp şelalesi. POA yoksa Beklenen→Saha→Gerçek üç basamak.

    Basamaklar azalan kümülatif enerji taşır; her basamağın `loss_kwh`'ı bir
    öncekinden düşüşü, `loss_pct`'i ilk (teorik) basamağa oranı verir.
    """
    actual_kwh = _energy_kwh(actual, actual_interval_h)
    expected_kwh = _energy_kwh(expected, expected_interval_h)

    stages: list[tuple[str, float]] = []  # (label, cumulative kwh)
    if poa and dc_capacity_kwp and dc_capacity_kwp > 0:
        theoretical = dc_capacity_kwp * (
            sum(v / G_STC for v in poa.values()) * expected_interval_h
        )
        theo_temp = theoretical
        if cell_temp:
            corr = 0.0
            for ts, g in poa.items():
                tc = cell_temp.get(ts)
                corr += (g / G_STC) * (_ck(tc, gamma) if tc is not None else 1.0)
            theo_temp = dc_capacity_kwp * corr * expected_interval_h
        stages = [
            ("Teorik (POA)", theoretical),
            ("Sıcaklık sonrası", theo_temp),
            ("Sistem sonrası", expected_kwh),
            ("Gerçek", actual_kwh),
        ]
    else:
        stages = [
            ("Beklenen (ikiz)", expected_kwh),
            ("Gerçek", actual_kwh),
        ]

    base = stages[0][1] if stages else 0.0
    labels_for_loss = {
        "Sıcaklık sonrası": "Sıcaklık kaybı",
        "Sistem sonrası": "Sistem/dönüşüm kaybı",
        "Gerçek": "Saha kaybı (gölge/kirlilik/duruş)",
    }
    result: list[LossStage] = []
    prev = None
    for label, kwh in stages:
        if prev is None:
            result.append(LossStage(label=label, kwh=kwh, loss_kwh=0.0, loss_pct=0.0))
        else:
            loss = prev - kwh
            result.append(LossStage(
                label=labels_for_loss.get(label, label),
                kwh=kwh,
                loss_kwh=loss,
                loss_pct=(loss / base * 100.0) if base > 0 else 0.0,
            ))
        prev = kwh
    return result
