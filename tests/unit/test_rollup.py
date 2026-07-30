"""Saha → tesis toplama kuralları."""

from datetime import UTC, datetime, timedelta

import pytest

from luminmind.analytics.rollup import (
    SiteRollup,
    contributors,
    counter_energy_kwh,
    energy_kwh,
    peak_kw,
    performance_ratio,
    roll_up,
    sum_series,
)

T0 = datetime(2026, 7, 29, 9, 0, tzinfo=UTC)


def series(*values: float, start: datetime = T0, step_min: int = 15) -> dict[datetime, float]:
    return {start + timedelta(minutes=step_min * i): v for i, v in enumerate(values)}


def test_sum_series_adds_by_timestamp():
    total = sum_series([series(10.0, 20.0), series(1.0, 2.0)])
    assert list(total.values()) == [11.0, 22.0]


def test_sum_series_keeps_timestamps_present_in_only_one_site():
    a = series(10.0, 20.0)
    b = series(5.0, start=T0 + timedelta(minutes=30))
    total = sum_series([a, b])
    assert len(total) == 3
    assert total[T0 + timedelta(minutes=30)] == 5.0


def test_sum_series_output_is_time_ordered():
    late = series(3.0, start=T0 + timedelta(hours=2))
    total = sum_series([late, series(1.0)])
    assert list(total) == sorted(total)


def test_contributors_exposes_missing_data():
    a = series(10.0, 20.0)
    b = series(5.0)  # ikinci damgada veri yok
    counts = contributors([a, b])
    assert counts[T0] == 2
    assert counts[T0 + timedelta(minutes=15)] == 1


def test_energy_uses_interval_length():
    assert energy_kwh(series(100.0, 100.0, 100.0, 100.0)) == pytest.approx(100.0)
    assert energy_kwh(series(60.0), interval_hours=1.0) == pytest.approx(60.0)


def test_peak_and_pr_edge_cases():
    assert peak_kw({}) == 0.0
    assert peak_kw(series(5.0, 9.0, 2.0)) == 9.0
    # Beklenen anlamsızken oran uydurulmaz
    assert performance_ratio(100.0, 0.0) == 0.0
    assert performance_ratio(90.0, 100.0) == pytest.approx(90.0)


def make_site(key: str, actual: float, expected: float, peak: float, cap: float) -> SiteRollup:
    return SiteRollup(
        series_key=key,
        name=key,
        capacity_kwp=cap,
        actual_kwh=actual,
        expected_kwh=expected,
        peak_kw=peak,
        last_power_kw=peak / 2,
        open_anomalies=1,
    )


def test_roll_up_sums_energy_and_capacity():
    plant = roll_up(
        [
            make_site("uretim", 1200.0, 1500.0, 300.0, 400.0),
            make_site("mekanik", 600.0, 700.0, 180.0, 250.0),
        ]
    )
    assert plant.actual_kwh == 1800.0
    assert plant.expected_kwh == 2200.0
    assert plant.capacity_kwp == 650.0
    assert plant.open_anomalies == 2
    assert plant.site_count == 2


def test_plant_pr_is_energy_weighted_not_averaged():
    """Küçük sahanın kötü PR'ı tesis PR'ını büyük saha kadar etkilememeli."""
    big = make_site("uretim", 1900.0, 2000.0, 300.0, 400.0)  # PR %95
    small = make_site("mekanik", 50.0, 100.0, 30.0, 250.0)  # PR %50
    plant = roll_up([big, small])
    # Ağırlıklı: 1950/2100 ≈ %92,9 — basit ortalama (%72,5) değil
    assert plant.pr_pct == pytest.approx(92.86, abs=0.05)
    assert big.pr_pct == pytest.approx(95.0)
    assert small.pr_pct == pytest.approx(50.0)


def test_plant_peak_uses_simultaneous_sum_when_series_given():
    """Sahaların tepeleri farklı saatlerdeyse toplamak gerçekleşmemiş tepe üretir."""
    uretim = {T0: 300.0, T0 + timedelta(hours=3): 100.0}
    mekanik = {T0: 50.0, T0 + timedelta(hours=3): 180.0}
    sites = [
        make_site("uretim", 1200.0, 1500.0, 300.0, 400.0),
        make_site("mekanik", 600.0, 700.0, 180.0, 250.0),
    ]

    naive = roll_up(sites)
    assert naive.peak_kw == 480.0  # 300 + 180, ikisi aynı anda olmadı

    exact = roll_up(sites, actual_by_site={"uretim": uretim, "mekanik": mekanik})
    assert exact.peak_kw == 350.0  # gerçek eş zamanlı tepe
    assert exact.peak_kw < naive.peak_kw


def test_roll_up_of_no_sites_is_zero():
    plant = roll_up([])
    assert plant.actual_kwh == 0.0
    assert plant.peak_kw == 0.0
    assert plant.pr_pct == 0.0
    assert plant.site_count == 0


# --- Enerji sayacı ---------------------------------------------------------


def _readings(*values: float) -> list[tuple[datetime, float]]:
    base = datetime(2026, 7, 30, tzinfo=UTC)
    return [(base + timedelta(minutes=5 * i), v) for i, v in enumerate(values)]


def test_counter_energy_reads_daily_counter_from_zero() -> None:
    assert counter_energy_kwh(_readings(0.0, 0.0, 12.0, 31.0)) == pytest.approx(31.0)


def test_counter_energy_survives_a_polling_gap() -> None:
    """Boşluk enerjiyi silmez: sayaç aradaki üretimi kendisi taşır."""
    gapless = counter_energy_kwh(_readings(0.0, 10.0, 20.0, 31.0))
    with_gap = counter_energy_kwh(_readings(0.0, 31.0))
    assert gapless == with_gap == pytest.approx(31.0)


def test_counter_energy_ignores_midnight_reset() -> None:
    """Gün dönümünde sıfırlanan sayaçta düşüş üretim sayılmaz."""
    assert counter_energy_kwh(_readings(1137.0, 0.0, 28.0, 31.0)) == pytest.approx(31.0)


def test_counter_energy_ignores_a_spurious_zero() -> None:
    """Cihaz anlık ulaşılamayınca gelen sahte 0, üretimi ikinci kez saydırmaz."""
    assert counter_energy_kwh(_readings(900.0, 0.0, 914.0, 920.0)) == pytest.approx(20.0)


def test_counter_energy_ignores_repeated_spurious_zeros() -> None:
    """Arka arkaya iki sahte okuma da seviyeyi geri getirdiği sürece yutulur."""
    readings = _readings(900.0, 0.0, 0.0, 905.0, 1085.0)
    assert counter_energy_kwh(readings) == pytest.approx(185.0)


def test_counter_energy_still_accepts_a_sustained_reset() -> None:
    """Düşük seviye kalıcıysa gerçek sıfırlanmadır — yeni tabandan sayılır."""
    readings = _readings(1085.0, 0.0, 0.0, 0.0, 12.0, 26.0)
    assert counter_energy_kwh(readings) == pytest.approx(26.0)


def test_counter_energy_handles_lifetime_counter() -> None:
    """Hiç sıfırlanmayan ömürlük sayaçta gün içi fark döner."""
    assert counter_energy_kwh(_readings(15230.0, 15245.0, 15261.0)) == pytest.approx(31.0)


def test_counter_energy_is_zero_without_readings() -> None:
    assert counter_energy_kwh([]) == 0.0
