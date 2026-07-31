"""Fizibilite finansı: NPV, IRR, LCOE ve geri ödeme.

Bu sayılar yatırım kararına giriyor, dolayısıyla testler "makul aralıkta mı"
demiyor; çoğu doğrudan *özdeşlik* sınıyor. En güçlüsü IRR'ın kendi tanımıyla
tutarlılığı: IRR oranından iskontolanan NPV sıfır olmak zorunda.

Simülasyon burada taklit ediliyor — `SimulationResult`'ın finansa giren tek
alanları `projection`, `dc_capacity_kwp` ve `uncertainty`, dolayısıyla tam
üretim zincirini koşturmak gereksiz (ve yavaş) olurdu.
"""

import math
from dataclasses import dataclass

import pytest

from luminmind.prospect.finance import (
    CostModel,
    FinanceParams,
    RevenueModel,
    evaluate,
    internal_rate_of_return,
    levelised_cost,
    net_present_value,
    sensitivity,
)
from luminmind.prospect.simulate import YearProjection, YieldUncertainty

LIFETIME = 25
CAPACITY_KWP = 500.0
YEAR_ONE_KWH = 800_000.0  # 1600 kWh/kWp — Konya mertebesi
DEGRADATION = 0.005


@dataclass(frozen=True)
class FakeSimulation:
    """`evaluate`'in dokunduğu üç alan. Tam zincir finans testine girmez."""

    dc_capacity_kwp: float
    projection: tuple[YearProjection, ...]
    uncertainty: YieldUncertainty = YieldUncertainty()


def make_simulation(
    capacity_kwp: float = CAPACITY_KWP,
    year_one_kwh: float = YEAR_ONE_KWH,
    lifetime: int = LIFETIME,
    degradation: float = DEGRADATION,
) -> FakeSimulation:
    projection = tuple(
        YearProjection(
            year=year,
            energy_kwh=year_one_kwh * (1.0 - degradation) ** (year - 1),
            degradation_factor=(1.0 - degradation) ** (year - 1),
        )
        for year in range(1, lifetime + 1)
    )
    return FakeSimulation(dc_capacity_kwp=capacity_kwp, projection=projection)


SIMULATION = make_simulation()


# ------------------------------ RevenueModel ------------------------------


def test_blended_tariff_weights_the_two_prices():
    revenue = RevenueModel(
        retail_tariff_try_kwh=4.0, export_tariff_try_kwh=1.0, self_consumption_share=0.75
    )
    # 0,75 × 4,00 + 0,25 × 1,00 = 3,25
    assert revenue.blended_tariff_try_kwh() == pytest.approx(3.25)


def test_blended_tariff_at_share_extremes():
    retail = RevenueModel(retail_tariff_try_kwh=4.0, export_tariff_try_kwh=1.0,
                          self_consumption_share=1.0)
    export = RevenueModel(retail_tariff_try_kwh=4.0, export_tariff_try_kwh=1.0,
                          self_consumption_share=0.0)

    assert retail.blended_tariff_try_kwh() == pytest.approx(4.0)
    assert export.blended_tariff_try_kwh() == pytest.approx(1.0)


def test_self_consumption_share_must_be_a_fraction():
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        RevenueModel(self_consumption_share=1.4)
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        RevenueModel(self_consumption_share=-0.1)


def test_first_year_revenue_has_no_escalation():
    """Reel artış yıl 1'de uygulanmaz; yıl indeksi 1 tabanlı."""
    revenue = RevenueModel(real_price_escalation=0.03)
    blended = revenue.blended_tariff_try_kwh()

    assert revenue.revenue_try(1000.0, 1) == pytest.approx(1000.0 * blended)
    assert revenue.revenue_try(1000.0, 3) == pytest.approx(1000.0 * blended * 1.03**2)


def test_zero_escalation_keeps_real_price_flat():
    revenue = RevenueModel(real_price_escalation=0.0)
    assert revenue.revenue_try(1000.0, 25) == pytest.approx(revenue.revenue_try(1000.0, 1))


# ------------------------------ CostModel ------------------------------


def test_capex_and_opex_scale_with_capacity():
    costs = CostModel(capex_per_kwp_try=18_000.0, opex_per_kwp_year_try=320.0)

    assert costs.capex_try(500.0) == pytest.approx(9_000_000.0)
    assert costs.opex_try(500.0) == pytest.approx(160_000.0)


# ------------------------------ IRR özdeşliği ------------------------------


def test_npv_discounted_at_irr_is_zero():
    """IRR'ın tanımı bu: kendi oranından iskontolanan NPV sıfırdır.

    Testin gücü burada — bağımsız bir "beklenen IRR" sabiti yazmak yerine
    fonksiyonun kendi tanımına uyduğu doğrulanıyor.
    """
    result = evaluate(SIMULATION)
    assert result.irr_real is not None

    at_irr = evaluate(SIMULATION, params=FinanceParams(discount_rate_real=result.irr_real))
    assert at_irr.npv_try == pytest.approx(0.0, abs=1.0)


def test_irr_is_independent_of_the_discount_rate():
    """IRR nakit akışının özelliğidir; iskonto oranı yalnızca NPV'yi etkiler."""
    low = evaluate(SIMULATION, params=FinanceParams(discount_rate_real=0.05))
    high = evaluate(SIMULATION, params=FinanceParams(discount_rate_real=0.25))

    assert low.irr_real == pytest.approx(high.irr_real, rel=1e-9)
    assert low.npv_try > high.npv_try


def test_irr_solves_a_hand_checkable_cashflow():
    """100 ₺ yatırım, 10 yıl 20 ₺: IRR ≈ %15,1."""
    irr = internal_rate_of_return(100.0, [20.0] * 10)
    assert irr == pytest.approx(0.1509, abs=1e-3)


def test_irr_is_negative_when_capex_is_not_recovered():
    """Toplam gelir yatırımın altındaysa IRR negatiftir — tanımsız değil.

    25 × 10 ₺ = 250 ₺ ile 1.000 ₺ yatırım geri gelmiyor, ama sermayenin *yılda
    %8,7 eridiği* oran anlamlı bir cevaptır ve öyle raporlanmalı.
    """
    irr = internal_rate_of_return(1_000.0, [10.0] * 25)

    assert irr is not None
    assert -1.0 < irr < 0.0


def test_irr_is_none_when_every_year_loses_money():
    """Hiçbir oranda başa baş gelmiyorsa IRR tanımsız; sessizce 0 dönmek "getirisi
    yok" gibi okunurdu, oysa "hesaplanamıyor" demek gerekiyor."""
    assert internal_rate_of_return(1_000.0, [-50.0] * 25) is None


def test_irr_is_none_when_there_is_no_investment_to_return():
    """Sıfır yatırımla pozitif akış her oranda kârlı; IRR'ın kökü yok."""
    assert internal_rate_of_return(0.0, [100.0] * 25) is None


def test_irr_survives_a_sign_change_mid_project():
    """İnvertör değişim yılında akış negatife döner; ikiye bölme buna dayanmalı."""
    flows = [200.0] * 12 + [-500.0] + [200.0] * 12
    irr = internal_rate_of_return(1_000.0, flows)

    assert irr is not None
    npv_at_irr = sum(f / (1.0 + irr) ** y for y, f in enumerate(flows, start=1)) - 1_000.0
    assert npv_at_irr == pytest.approx(0.0, abs=1e-3)


def test_npv_is_monotonically_decreasing_in_the_discount_rate():
    rates = (0.05, 0.10, 0.15, 0.20, 0.30)
    npvs = [evaluate(SIMULATION, params=FinanceParams(discount_rate_real=r)).npv_try
            for r in rates]

    assert npvs == sorted(npvs, reverse=True)


# ------------------------------ NPV ------------------------------


def test_net_present_value_subtracts_capex_from_discounted_flows():
    result = evaluate(SIMULATION)
    discounted = sum(r.discounted_net_try for r in result.rows)

    assert result.npv_try == pytest.approx(discounted - result.capex_try)
    assert net_present_value(result.capex_try, result.rows) == pytest.approx(result.npv_try)


def test_discounting_uses_end_of_year_convention():
    """Yıl 1 bir dönem iskontolanır; iskontosuz saymak NPV'yi yukarı kaydırır."""
    result = evaluate(SIMULATION, params=FinanceParams(discount_rate_real=0.10))
    first = result.rows[0]

    assert first.discounted_net_try == pytest.approx(first.net_try / 1.10)
    assert first.discounted_net_try < first.net_try


def test_is_viable_tracks_npv_sign():
    cheap = evaluate(SIMULATION, costs=CostModel(capex_per_kwp_try=8_000.0))
    absurd = evaluate(SIMULATION, costs=CostModel(capex_per_kwp_try=200_000.0))

    assert cheap.is_viable and cheap.npv_try > 0
    assert not absurd.is_viable and absurd.npv_try < 0


# ------------------------------ LCOE ------------------------------


def test_lcoe_excludes_revenue():
    """LCOE üretim maliyetidir, kârlılık değil — tarife onu değiştirmemeli."""
    cheap_tariff = evaluate(SIMULATION, revenue=RevenueModel(retail_tariff_try_kwh=1.0))
    rich_tariff = evaluate(SIMULATION, revenue=RevenueModel(retail_tariff_try_kwh=9.0))

    assert cheap_tariff.lcoe_try_kwh == pytest.approx(rich_tariff.lcoe_try_kwh)
    assert rich_tariff.npv_try > cheap_tariff.npv_try


def test_lcoe_discounts_energy_as_well_as_cost():
    """Enerjiyi iskontolamamak LCOE'yi sistematik olarak düşük gösteren yaygın hata."""
    result = evaluate(SIMULATION, params=FinanceParams(discount_rate_real=0.12))
    rows = result.rows

    undiscounted_energy = sum(r.energy_kwh for r in rows)
    discounted_cost = result.capex_try + sum(
        (r.opex_try + r.replacement_try) / 1.12**r.year for r in rows
    )
    naive_lcoe = discounted_cost / undiscounted_energy

    assert result.lcoe_try_kwh > naive_lcoe
    assert result.lcoe_try_kwh == pytest.approx(
        levelised_cost(result.capex_try, rows, 0.12)
    )


def test_lcoe_rises_with_capex():
    cheap = evaluate(SIMULATION, costs=CostModel(capex_per_kwp_try=12_000.0))
    dear = evaluate(SIMULATION, costs=CostModel(capex_per_kwp_try=24_000.0))

    assert dear.lcoe_try_kwh > cheap.lcoe_try_kwh


def test_lcoe_is_plausible_for_a_turkish_commercial_roof():
    """18.000 ₺/kWp ve 1600 kWh/kWp'de 1,2–2,2 ₺/kWh mertebesi beklenir."""
    result = evaluate(SIMULATION)
    assert 1.2 < result.lcoe_try_kwh < 2.2


def test_lcoe_is_zero_when_there_is_no_energy():
    empty = make_simulation(capacity_kwp=0.0, year_one_kwh=0.0)
    assert evaluate(empty).lcoe_try_kwh == 0.0


# ------------------------------ Geri ödeme ------------------------------


def test_payback_interpolates_within_the_crossing_year():
    """Tam yıl döndürmek 3,1 yıl ile 3,9 yılı aynı göstermek olurdu."""
    result = evaluate(SIMULATION)

    assert result.payback_years is not None
    assert result.payback_years != math.floor(result.payback_years)

    crossing = math.ceil(result.payback_years)
    before = next(r for r in result.rows if r.year == crossing - 1)
    after = next(r for r in result.rows if r.year == crossing)
    assert before.cumulative_net_try < 0.0 <= after.cumulative_net_try


def test_payback_is_none_when_capex_is_never_recovered():
    result = evaluate(SIMULATION, costs=CostModel(capex_per_kwp_try=500_000.0))
    assert result.payback_years is None


def test_payback_shortens_with_a_richer_tariff():
    cheap = evaluate(SIMULATION, revenue=RevenueModel(retail_tariff_try_kwh=2.0))
    rich = evaluate(SIMULATION, revenue=RevenueModel(retail_tariff_try_kwh=6.0))

    assert rich.payback_years < cheap.payback_years


def test_cumulative_flow_starts_below_zero_by_the_capex():
    result = evaluate(SIMULATION)
    first = result.rows[0]

    assert first.cumulative_net_try == pytest.approx(first.net_try - result.capex_try)


# ------------------------------ Nakit akışı kalemleri ------------------------------


def test_cashflow_rows_cover_the_project_lifetime():
    result = evaluate(SIMULATION)

    assert len(result.rows) == LIFETIME
    assert [r.year for r in result.rows] == list(range(1, LIFETIME + 1))


def test_net_flow_is_revenue_less_opex_and_replacement():
    result = evaluate(SIMULATION)

    for row in result.rows:
        assert row.net_try == pytest.approx(row.revenue_try - row.opex_try - row.replacement_try)


def test_inverter_replacement_lands_on_the_configured_year():
    costs = CostModel(inverter_replacement_year=13, inverter_replacement_per_kwp_try=1_800.0)
    result = evaluate(SIMULATION, costs=costs)

    charged = [r for r in result.rows if r.replacement_try > 0.0]
    assert len(charged) == 1
    assert charged[0].year == 13
    assert charged[0].replacement_try == pytest.approx(1_800.0 * CAPACITY_KWP)


def test_replacement_can_be_disabled():
    costs = CostModel(inverter_replacement_year=None)
    result = evaluate(SIMULATION, costs=costs)

    assert all(r.replacement_try == 0.0 for r in result.rows)


def test_residual_value_is_credited_in_the_final_year_only():
    costs = CostModel(residual_value_per_kwp_try=500.0)
    with_residual = evaluate(SIMULATION, costs=costs)
    without = evaluate(SIMULATION, costs=CostModel())

    delta = with_residual.rows[-1].net_try - without.rows[-1].net_try
    assert delta == pytest.approx(500.0 * CAPACITY_KWP)
    # Ara yıllar etkilenmemeli
    assert with_residual.rows[0].net_try == pytest.approx(without.rows[0].net_try)


def test_energy_follows_the_projection_not_the_as_new_year():
    """Gelir `projection`'dan gelir; sıfır yaşlı değer 25 yılı yukarı kaydırırdı."""
    result = evaluate(SIMULATION)

    for row, item in zip(result.rows, SIMULATION.projection, strict=True):
        assert row.energy_kwh == pytest.approx(item.energy_kwh)


def test_lifetime_totals_sum_the_rows():
    result = evaluate(SIMULATION)

    assert result.lifetime_energy_kwh == pytest.approx(sum(r.energy_kwh for r in result.rows))
    assert result.lifetime_revenue_try == pytest.approx(sum(r.revenue_try for r in result.rows))
    assert result.year_one_revenue_try == pytest.approx(result.rows[0].revenue_try)


def test_specific_capex_reports_back_the_unit_price():
    result = evaluate(SIMULATION, costs=CostModel(capex_per_kwp_try=18_000.0))
    assert result.specific_capex_try_kwp == pytest.approx(18_000.0)


def test_blended_tariff_is_recorded_on_the_result():
    revenue = RevenueModel(
        retail_tariff_try_kwh=4.0, export_tariff_try_kwh=2.0, self_consumption_share=0.5
    )
    result = evaluate(SIMULATION, revenue=revenue)
    assert result.blended_tariff_try_kwh == pytest.approx(3.0)


# ------------------------------ P50 / P90 senaryosu ------------------------------


def test_p50_scenario_leaves_energy_untouched():
    p50 = evaluate(SIMULATION, params=FinanceParams(exceedance=0.5))
    assert p50.rows[0].energy_kwh == pytest.approx(SIMULATION.projection[0].energy_kwh)


def test_p90_scenario_reduces_energy_and_npv():
    """P90 "yılların %90'ında bu değerin üstünde" demek — P50'nin altındadır."""
    p50 = evaluate(SIMULATION, params=FinanceParams(exceedance=0.5))
    p90 = evaluate(SIMULATION, params=FinanceParams(exceedance=0.90))

    factor = SIMULATION.uncertainty.percentile_factor(0.90)
    assert 0.88 < factor < 0.95
    assert p90.rows[0].energy_kwh == pytest.approx(p50.rows[0].energy_kwh * factor)
    assert p90.npv_try < p50.npv_try
    assert p90.irr_real < p50.irr_real


def test_p10_scenario_is_optimistic():
    p10 = evaluate(SIMULATION, params=FinanceParams(exceedance=0.10))
    p50 = evaluate(SIMULATION, params=FinanceParams(exceedance=0.5))

    assert p10.npv_try > p50.npv_try


def test_uncertainty_components_combine_in_quadrature():
    uncertainty = YieldUncertainty(
        interannual_variability=0.04,
        irradiance_model=0.03,
        system_model=0.03,
        soiling_availability=0.02,
    )
    expected = math.sqrt(0.04**2 + 0.03**2 + 0.03**2 + 0.02**2)

    assert uncertainty.combined == pytest.approx(expected)
    assert uncertainty.percentile_factor(0.5) == pytest.approx(1.0)


# ------------------------------ Duyarlılık ------------------------------


def test_sensitivity_covers_every_axis_and_delta():
    deltas = (-0.20, -0.10, 0.0, 0.10, 0.20)
    table = sensitivity(SIMULATION, deltas=deltas)

    assert set(table) == {
        "Yatırım maliyeti", "Perakende tarife", "Öztüketim payı", "İşletme maliyeti"
    }
    for points in table.values():
        assert [d for d, _ in points] == list(deltas)


def test_sensitivity_npv_falls_as_costs_rise():
    table = sensitivity(SIMULATION)

    for axis in ("Yatırım maliyeti", "İşletme maliyeti"):
        npvs = [npv for _, npv in table[axis]]
        assert npvs == sorted(npvs, reverse=True), f"{axis} artarken NPV düşmeli"


def test_sensitivity_npv_rises_with_tariff_and_self_consumption():
    table = sensitivity(SIMULATION)

    for axis in ("Perakende tarife", "Öztüketim payı"):
        npvs = [npv for _, npv in table[axis]]
        assert npvs == sorted(npvs), f"{axis} artarken NPV yükselmeli"


def test_sensitivity_zero_delta_matches_the_base_case():
    base = evaluate(SIMULATION).npv_try
    table = sensitivity(SIMULATION)

    for points in table.values():
        at_zero = next(npv for d, npv in points if d == 0.0)
        assert at_zero == pytest.approx(base)


def test_sensitivity_keeps_self_consumption_share_within_bounds():
    """Pay 0,75 iken +%40 kayma 1,05 yapar; `RevenueModel` bunu reddederdi."""
    revenue = RevenueModel(self_consumption_share=0.75)
    table = sensitivity(SIMULATION, revenue=revenue, deltas=(0.0, 0.40))

    assert len(table["Öztüketim payı"]) == 2


# ------------------------------ Sınır durumlar ------------------------------


def test_evaluate_on_an_empty_layout_does_not_crash():
    """Panel sığmayan tasarımda finans sıfırlanmalı, hata vermemeli."""
    empty = FakeSimulation(dc_capacity_kwp=0.0, projection=())
    result = evaluate(empty)

    assert result.capex_try == 0.0
    assert result.rows == ()
    assert result.npv_try == 0.0
    assert result.lcoe_try_kwh == 0.0
    assert result.payback_years is None
    assert result.year_one_revenue_try == 0.0
    assert result.specific_capex_try_kwp == 0.0


def test_degradation_reduces_later_year_revenue():
    result = evaluate(SIMULATION, revenue=RevenueModel(real_price_escalation=0.0))

    assert result.rows[-1].energy_kwh < result.rows[0].energy_kwh
    assert result.rows[-1].revenue_try < result.rows[0].revenue_try


def test_real_price_escalation_can_offset_degradation():
    """Reel fiyat artışı bozunumdan büyükse son yıl geliri ilk yılı aşar."""
    flat = evaluate(SIMULATION, revenue=RevenueModel(real_price_escalation=0.0))
    rising = evaluate(SIMULATION, revenue=RevenueModel(real_price_escalation=0.03))

    assert flat.rows[-1].revenue_try < flat.rows[0].revenue_try
    assert rising.rows[-1].revenue_try > rising.rows[0].revenue_try
