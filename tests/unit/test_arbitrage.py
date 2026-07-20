from datetime import UTC, date, datetime, timedelta

import httpx
import respx

from luminmind.analytics.arbitrage.epias import EpiasClient, PriceSlot
from luminmind.analytics.arbitrage.mock_prices import MockPriceProvider
from luminmind.analytics.arbitrage.optimizer import (
    ACTION_CHARGE,
    ACTION_DISCHARGE,
    BatterySpec,
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
