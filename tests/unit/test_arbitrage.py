from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import respx

from luminmind.analytics.arbitrage.epias import EpiasClient, PriceSlot
from luminmind.analytics.arbitrage.mock_prices import MockPriceProvider
from luminmind.analytics.arbitrage.optimizer import (
    ACTION_CHARGE,
    ACTION_DISCHARGE,
    BatterySpec,
    SiteSpec,
    optimize_day,
)

DAY = date(2026, 7, 21)
BATTERY = BatterySpec(energy_kwh=1000.0, power_kw=500.0)


def hourly_prices(values: list[float]) -> list[PriceSlot]:
    base = datetime(DAY.year, DAY.month, DAY.day, tzinfo=UTC)
    return [
        PriceSlot(start=base + timedelta(hours=h), price_try_mwh=v)
        for h, v in enumerate(values)
    ]


async def test_mock_provider_returns_24_hourly_slots():
    slots = await MockPriceProvider().fetch_day_ahead_prices(DAY)
    assert len(slots) == 24
    # TRT gece yarısı = 21:00 UTC önceki gün
    assert slots[0].start == datetime(2026, 7, 20, 21, 0, tzinfo=UTC)
    assert max(s.price_try_mwh for s in slots) > 2 * min(s.price_try_mwh for s in slots)


def test_flat_prices_yield_no_action():
    result = optimize_day(hourly_prices([1500.0] * 24), BATTERY)
    assert result.expected_revenue_try <= 0.01
    assert all(s.power_kw == 0.0 for s in result.slots)


def test_price_spread_produces_profitable_plan():
    # gece ucuz (1000), akşam pahalı (2600)
    prices = [1000.0] * 6 + [1500.0] * 12 + [2600.0] * 4 + [1500.0] * 2
    result = optimize_day(hourly_prices(prices), BATTERY)

    assert result.expected_revenue_try > 0
    charge_hours = [s.start.hour for s in result.slots if s.action == ACTION_CHARGE]
    discharge_hours = [s.start.hour for s in result.slots if s.action == ACTION_DISCHARGE]
    assert charge_hours and discharge_hours
    # ucuz gecede alım başlar, pahalı akşam tepesinde satılır; E_son ≥ E_ilk kısıtı
    # nedeniyle satış sonrası (1500 TL) saatlerde geri dolum meşrudur
    assert min(charge_hours) < min(discharge_hours)
    assert all(18 <= h < 22 for h in discharge_hours)
    # tepe fiyat saatlerinde asla şarj edilmez
    assert not any(18 <= h < 22 for h in charge_hours)


def test_constraints_respected():
    prices = [1000.0] * 6 + [1500.0] * 12 + [2600.0] * 4 + [1500.0] * 2
    battery = BatterySpec(energy_kwh=1000.0, power_kw=500.0, soc_initial=0.5)
    result = optimize_day(hourly_prices(prices), battery)

    # slot güçleri 3 hane yuvarlandığı için küçük birikimli tolerans bırakılır
    tol_kwh = 0.01
    energy = battery.soc_initial * battery.energy_kwh
    for slot in result.slots:
        assert 0.0 <= slot.power_kw <= battery.power_kw + 1e-3
        if slot.action == ACTION_CHARGE:
            energy += battery.eta_charge * slot.power_kw
        elif slot.action == ACTION_DISCHARGE:
            energy -= slot.power_kw / battery.eta_discharge
        assert battery.soc_min * battery.energy_kwh - tol_kwh <= energy
        assert energy <= battery.soc_max * battery.energy_kwh + tol_kwh
    # gün sonu enerjisi başlangıcın altında bitmemeli
    assert energy >= battery.soc_initial * battery.energy_kwh - tol_kwh


def test_cycle_limit_bounds_discharge_throughput():
    # aşırı oynak fiyat: limit olmasa sürekli al-sat yapardı
    prices = [1000.0, 3000.0] * 12
    battery = BatterySpec(energy_kwh=1000.0, power_kw=800.0, max_cycles_per_day=1.0)
    result = optimize_day(hourly_prices(prices), battery)
    discharged = sum(s.power_kw for s in result.slots if s.action == ACTION_DISCHARGE)
    usable = battery.energy_kwh * (battery.soc_max - battery.soc_min)
    assert discharged <= usable * battery.max_cycles_per_day + 1e-3


def midday_pv(peak_kw: float) -> list[float]:
    """21:00 UTC'de başlayan 24 slot için basit bir üretim profili (kW)."""
    profile = []
    for index in range(24):
        hour_utc = (21 + index) % 24
        profile.append(peak_kw if 7 <= hour_utc <= 13 else 0.0)
    return profile


def test_battery_absorbs_energy_that_would_be_curtailed():
    """Bağlantı limiti üretimin altındaysa batarya kırpılacak enerjiyi kurtarmalı."""
    prices = hourly_prices([1500.0] * 24)  # düz fiyat: saf arbitraj kazancı yok
    site = SiteSpec(pv_forecast_kw=midday_pv(1000.0), grid_limit_kw=600.0)
    no_battery = BatterySpec(energy_kwh=1.0, power_kw=0.0)
    with_battery = BatterySpec(energy_kwh=2000.0, power_kw=500.0)

    baseline = optimize_day(prices, no_battery, site=site)
    result = optimize_day(prices, with_battery, site=site)

    # Bataryasız: limiti aşan her kW kırpılır (7 saat × 400 kW)
    assert baseline.recovered_kwh == 0.0
    assert baseline.curtailed_kwh == pytest.approx(7 * 400.0, abs=1.0)
    # Bataryalı: kırpılacak enerjinin bir kısmı depolanıp sonra satılır
    assert result.recovered_kwh > 0
    assert result.curtailed_kwh < baseline.curtailed_kwh
    assert result.expected_revenue_try > baseline.expected_revenue_try
    assert any(s.action == ACTION_DISCHARGE for s in result.slots)


def test_grid_limit_caps_total_export():
    site = SiteSpec(pv_forecast_kw=midday_pv(1000.0), grid_limit_kw=600.0)
    battery = BatterySpec(energy_kwh=2000.0, power_kw=500.0)
    result = optimize_day(hourly_prices([1000.0] * 12 + [2600.0] * 12), battery, site=site)
    for slot in result.slots:
        export = slot.pv_export_kw + (slot.power_kw if slot.action == ACTION_DISCHARGE else 0.0)
        assert export <= 600.0 + 1e-2


def test_pv_routing_conserves_energy():
    site = SiteSpec(pv_forecast_kw=midday_pv(1000.0), grid_limit_kw=600.0)
    battery = BatterySpec(energy_kwh=2000.0, power_kw=500.0)
    result = optimize_day(hourly_prices([1200.0] * 24), battery, site=site)
    for slot, produced in zip(result.slots, midday_pv(1000.0), strict=True):
        routed = slot.pv_to_battery_kw + slot.pv_export_kw + slot.curtailed_kw
        assert routed == pytest.approx(produced, abs=1e-2)


def test_no_pv_forecast_preserves_pure_arbitrage_behaviour():
    prices = [1000.0] * 6 + [1500.0] * 12 + [2600.0] * 4 + [1500.0] * 2
    with_site = optimize_day(hourly_prices(prices), BATTERY, site=SiteSpec())
    without_site = optimize_day(hourly_prices(prices), BATTERY)
    assert with_site.expected_revenue_try == pytest.approx(without_site.expected_revenue_try)
    assert with_site.pv_revenue_try == 0.0
    assert with_site.curtailed_kwh == 0.0


def test_grid_charging_can_be_forbidden():
    prices = [1000.0] * 6 + [1500.0] * 12 + [2600.0] * 4 + [1500.0] * 2
    site = SiteSpec(allow_grid_charge=False)
    result = optimize_day(hourly_prices(prices), BATTERY, site=site)
    assert all(s.grid_charge_kw == 0.0 for s in result.slots)


def test_feed_in_tariff_overrides_market_price_for_pv():
    """Sabit alım garantisi varsa PV geliri piyasa fiyatından bağımsızdır."""
    site = SiteSpec(pv_forecast_kw=midday_pv(400.0), feed_in_try_mwh=2900.0)
    flat = BatterySpec(energy_kwh=1.0, power_kw=0.0)  # batarya etkisiz
    result = optimize_day(hourly_prices([500.0] * 24), flat, site=site)
    # 7 saat × 400 kW × 2900 ₺/MWh = 8120 ₺
    assert result.pv_revenue_try == pytest.approx(7 * 400.0 * 2.9, rel=1e-3)


def test_revenue_splits_into_battery_and_pv_components():
    prices = [1000.0] * 6 + [1500.0] * 12 + [2600.0] * 4 + [1500.0] * 2
    site = SiteSpec(pv_forecast_kw=midday_pv(300.0), grid_limit_kw=2000.0)
    result = optimize_day(hourly_prices(prices), BATTERY, site=site)
    assert result.pv_revenue_try > 0
    assert result.expected_revenue_try == pytest.approx(
        result.battery_revenue_try + result.pv_revenue_try, rel=1e-3
    )


def test_empty_prices_return_empty_plan():
    result = optimize_day([], BATTERY)
    assert result.slots == [] and result.expected_revenue_try == 0.0


async def test_mock_double_peak_day_is_profitable():
    price_slots = await MockPriceProvider().fetch_day_ahead_prices(DAY)
    result = optimize_day(price_slots, BATTERY)
    assert result.expected_revenue_try > 0
    actions = {s.action for s in result.slots}
    assert ACTION_CHARGE in actions and ACTION_DISCHARGE in actions


@respx.mock
async def test_epias_client_parses_prices():
    route = respx.get("https://seffaflik.example/v1/markets/dam/mcp").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {"hour_start": "2026-07-21T00:00:00+03:00", "price_try_mwh": 1450.5},
                    {"hour_start": "2026-07-21T01:00:00+03:00", "price_try_mwh": 1380.0},
                ]
            },
        )
    )
    client = EpiasClient(base_url="https://seffaflik.example")
    try:
        slots = await client.fetch_day_ahead_prices(DAY)
    finally:
        await client.aclose()

    assert dict(route.calls.last.request.url.params)["date"] == "2026-07-21"
    assert len(slots) == 2
    assert slots[0].start == datetime(2026, 7, 20, 21, 0, tzinfo=UTC)  # UTC'ye çevrildi
    assert slots[0].price_try_mwh == 1450.5
