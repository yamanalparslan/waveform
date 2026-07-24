"""IEC 61724 performans KPI ve kayıp şelalesi testleri."""

from datetime import UTC, datetime, timedelta

from luminmind.analytics.performance import (
    compute_kpis,
    compute_loss_waterfall,
)

DAY = datetime(2026, 7, 21, 6, 0, tzinfo=UTC)


def _series(values: list[float], step_min: int = 15) -> dict[datetime, float]:
    return {DAY + timedelta(minutes=step_min * i): v for i, v in enumerate(values)}


def test_specific_yield_and_capacity_factor():
    # 4 × 15dk = 1 saat; gerçek 500 kW sabit → 500 kWh
    actual = _series([500, 500, 500, 500])
    expected = _series([520, 520, 520, 520])
    kpis = compute_kpis(
        actual, expected,
        dc_capacity_kwp=1000.0, ac_capacity_kw=1000.0,
        period_hours=1.0,
        actual_interval_h=0.25, expected_interval_h=0.25,
    )
    assert kpis.actual_kwh == 500.0
    assert kpis.specific_yield == 0.5          # 500 kWh / 1000 kWp
    assert kpis.capacity_factor_pct == 50.0     # 500 / (1000 × 1h)
    # POA yok → PR twin tabanlı: 500/520
    assert round(kpis.pr_pct, 1) == 96.2


def test_pr_uses_poa_when_available():
    actual = _series([800, 800])
    expected = _series([850, 850])
    # POA 1000 W/m² sabit → Yr = (1000/1000)×0.25×2 = 0.5 h
    poa = _series([1000.0, 1000.0])
    kpis = compute_kpis(
        actual, expected,
        dc_capacity_kwp=2000.0, ac_capacity_kw=2000.0,
        period_hours=0.5,
        actual_interval_h=0.25, expected_interval_h=0.25,
        poa=poa,
    )
    # actual = 400 kWh; theoretical = 2000 × 0.5 = 1000 kWh → PR 40%
    assert kpis.actual_kwh == 400.0
    assert round(kpis.pr_pct, 1) == 40.0


def test_temperature_corrected_pr_higher_when_hot():
    actual = _series([800, 800])
    expected = _series([850, 850])
    poa = _series([1000.0, 1000.0])
    cell = _series([45.0, 45.0])  # 20°C STC üstü → Ck = 1 - 0.0035×20 = 0.93
    kpis = compute_kpis(
        actual, expected,
        dc_capacity_kwp=2000.0, ac_capacity_kw=2000.0,
        period_hours=0.5,
        actual_interval_h=0.25, expected_interval_h=0.25,
        poa=poa, cell_temp=cell,
    )
    # Sıcaklık düzeltmeli teorik daha küçük → PR_temp > PR
    assert kpis.pr_temp_pct is not None
    assert kpis.pr_temp_pct > kpis.pr_pct


def test_availability_counts_producing_daylight_intervals():
    # 4 gündüz aralığı (beklenen yüksek); gerçekte 3'ünde üretim var
    actual = _series([100, 0, 120, 130])
    expected = _series([150, 150, 150, 150])
    kpis = compute_kpis(
        actual, expected,
        dc_capacity_kwp=1000.0, ac_capacity_kw=1000.0,
        period_hours=1.0,
        actual_interval_h=0.25, expected_interval_h=0.25,
        min_expected_kw=1.0,
    )
    assert kpis.availability_pct == 75.0


def test_loss_waterfall_without_poa_two_stages():
    actual = _series([400, 400])       # 200 kWh
    expected = _series([500, 500])     # 250 kWh
    stages = compute_loss_waterfall(
        actual, expected,
        dc_capacity_kwp=1000.0,
        actual_interval_h=0.25, expected_interval_h=0.25,
    )
    # Beklenen(base) → Saha kaybı(loss) → Gerçek(final)
    assert [s.label for s in stages] == ["Beklenen", "Saha kaybı", "Gerçek"]
    assert [s.kind for s in stages] == ["base", "loss", "final"]
    assert stages[0].kwh == 250.0
    assert stages[1].kwh == 200.0
    assert stages[1].loss_kwh == 50.0
    assert stages[1].loss_pct == 20.0  # 50 / 250
    assert stages[2].kwh == 200.0  # Gerçek final sütunu


def test_loss_waterfall_with_poa_five_stages():
    actual = _series([700, 700])       # 350 kWh
    expected = _series([850, 850])     # 425 kWh
    poa = _series([1000.0, 1000.0])
    cell = _series([45.0, 45.0])
    stages = compute_loss_waterfall(
        actual, expected,
        dc_capacity_kwp=1000.0,
        actual_interval_h=0.25, expected_interval_h=0.25,
        poa=poa, cell_temp=cell,
    )
    assert [s.label for s in stages] == [
        "Teorik", "Sıcaklık kaybı", "Sistem kaybı", "Saha kaybı", "Gerçek",
    ]
    assert [s.kind for s in stages] == ["base", "loss", "loss", "loss", "final"]
    # Teorik = 1000 kWp × 0.5 h = 500 kWh
    assert stages[0].kwh == 500.0
    # Kümülatif azalan (final = son loss ile aynı = gerçek)
    assert stages[0].kwh > stages[1].kwh > stages[2].kwh >= stages[3].kwh
    assert round(stages[-1].kwh, 1) == 350.0  # Gerçek final
    assert stages[-1].kind == "final"
