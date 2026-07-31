"""Kurulmamış santral simülasyonu: TMY + yerleşim → 25 yıllık enerji.

Fizik `twin/`de doğrulanıyor (bkz. `test_twin_model.py`); burada sınanan
fizibiliteye özgü katman: kayıp şelalesinin *kapanması*, bozunum projeksiyonunun
kırpmayla etkileşimi ve string boyutlandırmanın sıcaklık uçlarını sahadan alması.

TMY sentetiktir — pvlib açık gökyüzü ışınımının ölçeklenmiş hâli. Amaç PVGIS'i
taklit etmek değil, zincire 8760 satırlık tutarlı bir yıl vermek.
"""

import pytest

from luminmind.prospect.layout import (
    InverterSpec,
    ModuleSpec,
    MountingSpec,
    pack_panels,
)
from luminmind.prospect.pvgis import TmyDataset
from luminmind.prospect.simulate import (
    DEFAULT_ANNUAL_DEGRADATION,
    DEFAULT_LIFETIME_YEARS,
    G_STC_WM2,
    YieldUncertainty,
    compare_tilts,
    design_temperatures,
    estimate_max_cell_temp,
    simulate,
)
from luminmind.twin.components import LossChain
from luminmind.twin.plant_model import MountType

KONYA_LAT, KONYA_LON = 37.87, 32.48
ROOF = ((0.0, 0.0), (40.0, 0.0), (40.0, 20.0), (0.0, 20.0))


@pytest.fixture(scope="module")
def tmy(synthetic_tmy) -> TmyDataset:
    return synthetic_tmy(latitude=KONYA_LAT, longitude=KONYA_LON)


@pytest.fixture(scope="module")
def layout(tmy):
    min_ambient, max_cell = design_temperatures(tmy, MountType.ROOFTOP_TILTED)
    return pack_panels(
        ROOF,
        latitude=KONYA_LAT,
        longitude=KONYA_LON,
        min_ambient_c=min_ambient,
        max_cell_c=max_cell,
        module=ModuleSpec(),
        inverter=InverterSpec(ac_kw=60.0),
        mounting=MountingSpec(mount=MountType.ROOFTOP_TILTED, tilt_deg=15.0, setback_m=0.6),
    )


@pytest.fixture(scope="module")
def result(tmy, layout):
    return simulate(tmy, layout)


# ------------------------------ Tasarım sıcaklıkları ------------------------------


def test_design_temperatures_come_from_the_site_tmy(tmy):
    """Sabit bir "−10 °C" varsayımı Antalya'da kısa, Ağrı'da tehlikeli string üretirdi."""
    min_ambient, max_cell = design_temperatures(tmy, MountType.ROOFTOP_TILTED)

    assert min_ambient == pytest.approx(float(tmy.weather["temp_air"].min()))
    assert min_ambient == pytest.approx(-12.0, abs=0.1)
    assert max_cell > float(tmy.weather["temp_air"].max())


def test_close_mounted_roof_runs_hotter_than_a_tilted_array(tmy):
    """Çatıya paralel montajda arka yüz havalanmıyor; Ross katsayısı daha yüksek."""
    _, parallel_cell = design_temperatures(tmy, MountType.ROOFTOP)
    _, tilted_cell = design_temperatures(tmy, MountType.ROOFTOP_TILTED)

    assert parallel_cell > tilted_cell


def test_max_cell_temp_includes_a_safety_margin(tmy):
    """Ross yaklaşımı yatay ışınım kullandığı için sistematik olarak alçak kalıyor.

    Pay eklenmezse MPPT alt sınırı olduğundan rahat görünür ve gereğinden kısa
    string'e izin verilir.
    """
    estimate = estimate_max_cell_temp(tmy, MountType.ROOFTOP_TILTED)
    bare = float((tmy.weather["temp_air"] + 0.030 * tmy.weather["ghi"]).max())

    assert estimate == pytest.approx(bare + 5.0)


def test_design_temperatures_reach_the_string_plan(tmy, layout):
    min_ambient, max_cell = design_temperatures(tmy, MountType.ROOFTOP_TILTED)
    plan = layout.string_plan

    assert plan.design_min_ambient_c == pytest.approx(min_ambient)
    assert plan.design_max_cell_c == pytest.approx(max_cell)


# ------------------------------ Üretim büyüklükleri ------------------------------


def test_annual_yield_is_plausible_for_central_anatolia(result):
    """Ölçeklenmiş açık gökyüzü ile 1300–1800 kWh/kWp mertebesi beklenir."""
    assert 1_300.0 < result.specific_yield_kwh_kwp < 1_800.0


def test_performance_ratio_is_in_the_expected_band(result):
    """IEC 61724 PR'ı: iyi tasarlanmış bir çatı sisteminde 0,75–0,88."""
    assert 0.75 < result.performance_ratio < 0.88


def test_performance_ratio_matches_its_definition(result):
    """PR = Y_f / Y_r; referans verim düzlem üstü ışınımdan türer."""
    reference_yield = result.poa_kwh_m2 / (G_STC_WM2 / 1000.0)
    expected = (result.year_one_kwh / result.dc_capacity_kwp) / reference_yield

    assert result.performance_ratio == pytest.approx(expected)


def test_plane_of_array_exceeds_horizontal_for_a_tilted_south_array(result):
    """Güneye 15° yatırmak yıllık ışınımı yatay düzlemin üstüne çıkarır."""
    assert result.poa_kwh_m2 > result.ghi_kwh_m2


def test_monthly_energy_covers_twelve_months_and_sums_to_the_year(result):
    assert len(result.monthly_kwh) == 12
    assert sum(result.monthly_kwh) == pytest.approx(result.year_one_kwh, rel=1e-9)


def test_summer_months_outproduce_winter_months(result):
    january, december = result.monthly_kwh[0], result.monthly_kwh[11]
    june, july = result.monthly_kwh[5], result.monthly_kwh[6]

    assert min(june, july) > max(january, december)


def test_cell_temperature_summary_is_ordered(result):
    assert result.max_cell_temp_c > result.mean_cell_temp_c
    assert 20.0 < result.mean_cell_temp_c < 45.0


def test_provenance_is_carried_through_from_the_tmy(result, tmy):
    """Rapor hangi veriyle hesaplandığını göstermek zorunda."""
    assert result.provenance == tmy.provenance


# ------------------------------ Kayıp şelalesi ------------------------------


def test_waterfall_closes(result):
    """Adımların toplam kaybı referans ile net enerjinin farkına *eşit* olmalı.

    Kapanmazsa "kayıp nerede" sorusu cevapsız kalır ve şelale gösterim olmaktan
    çıkıp yanlış bilgi verir.
    """
    stages = result.waterfall
    reference = stages[0].energy_kwh
    net = stages[-1].energy_kwh
    total_loss = sum(stage.loss_kwh for stage in stages)

    assert total_loss == pytest.approx(reference - net, rel=1e-9)


def test_waterfall_stages_are_monotonically_decreasing(result):
    """Her adım bir öncekinden enerji götürür; artış bir işaret hatası olurdu."""
    energies = [stage.energy_kwh for stage in result.waterfall]
    assert energies == sorted(energies, reverse=True)


def test_waterfall_ends_at_the_annual_energy(result):
    assert result.waterfall[-1].energy_kwh == pytest.approx(result.year_one_kwh, rel=1e-9)


def test_waterfall_first_stage_is_the_reference_with_no_loss(result):
    first = result.waterfall[0]

    assert first.loss_kwh == pytest.approx(0.0)
    assert first.loss_pct == pytest.approx(0.0)
    assert "referans" in first.label


def test_waterfall_percentages_are_relative_to_the_reference(result):
    reference = result.waterfall[0].energy_kwh

    for stage in result.waterfall:
        assert stage.loss_pct == pytest.approx(100.0 * stage.loss_kwh / reference)


def test_waterfall_labels_every_documented_stage(result):
    labels = [stage.label for stage in result.waterfall]

    assert len(labels) == 7
    assert len(set(labels)) == 7


# ------------------------------ Ömür projeksiyonu ------------------------------


def test_projection_spans_the_configured_lifetime(result):
    assert len(result.projection) == DEFAULT_LIFETIME_YEARS
    assert [p.year for p in result.projection] == list(range(1, DEFAULT_LIFETIME_YEARS + 1))


def test_projection_energy_declines_every_year(result):
    energies = [p.energy_kwh for p in result.projection]
    assert energies == sorted(energies, reverse=True)


def test_first_projection_year_is_below_the_as_new_value(result):
    """`projection[0]` yıl ortası yaşı (0,5 yıl) içeriyor, `year_one_kwh` sıfır yaşlı.

    İkisini karıştırmak 25 yıllık geliri sistematik olarak yukarı kaydırır.
    """
    first = result.projection[0].energy_kwh

    assert first < result.year_one_kwh
    assert first == pytest.approx(result.year_one_kwh, rel=0.01)


def test_degradation_factor_is_relative_to_the_first_year(result):
    assert result.projection[0].degradation_factor == pytest.approx(1.0)

    last = result.projection[-1]
    naive = (1.0 - DEFAULT_ANNUAL_DEGRADATION) ** (DEFAULT_LIFETIME_YEARS - 1)
    assert last.degradation_factor == pytest.approx(naive, rel=0.02)


def test_lifetime_energy_sums_the_projection(result):
    assert result.lifetime_kwh == pytest.approx(sum(p.energy_kwh for p in result.projection))


def test_lifetime_energy_is_below_the_undegraded_total(result):
    """Bozunum yok sayılsaydı 25 × ilk yıl olurdu; fark ~%6 mertebesinde."""
    undegraded = DEFAULT_LIFETIME_YEARS * result.year_one_kwh

    assert result.lifetime_kwh < undegraded
    assert result.lifetime_kwh > 0.9 * undegraded


def _sized_layout(tmy, target_dc_ac_ratio: float):
    """Verilen DC/AC hedefiyle yerleşim.

    Gerçekleşen oran invertör *adedinin* tam sayıya yuvarlanmasından çıkıyor
    (`ac_kw` tek başına belirlemiyor): 60 kW invertörle hedef 1,6 iken tek cihaz
    seçilir ve oran 1,59'da kalır; hedef 0,8 iken iki cihaz seçilip 0,80'e iner.
    """
    min_ambient, max_cell = design_temperatures(tmy, MountType.ROOFTOP_TILTED)
    return pack_panels(
        ROOF,
        latitude=KONYA_LAT,
        longitude=KONYA_LON,
        min_ambient_c=min_ambient,
        max_cell_c=max_cell,
        inverter=InverterSpec(ac_kw=60.0, target_dc_ac_ratio=target_dc_ac_ratio),
        mounting=MountingSpec(mount=MountType.ROOFTOP_TILTED, tilt_deg=15.0, setback_m=0.6),
    )


@pytest.fixture(scope="module")
def sunny_tmy(synthetic_tmy) -> TmyDataset:
    """Ölçeklenmemiş açık gökyüzü yılı — kırpma testleri için.

    Diğer testlerdeki 0,72 ölçeği bulutluluğu temsil ediyor ama tam olarak
    kırpmaya yol açan *tepeleri* de bastırıyor: 95,7 kWp'lik dizi 60 kW
    invertörle bile ancak %0,02 kırpılıyor. Gerçek Konya yazında öğle POA'sı
    950 W/m²'yi buluyor, dolayısıyla kırpma davranışını sınamak için ölçeksiz
    yıl daha temsilîdir.
    """
    return synthetic_tmy(latitude=KONYA_LAT, longitude=KONYA_LON, scale=1.0)


@pytest.fixture(scope="module")
def clipped(sunny_tmy):
    """DC/AC ≈ 1,6 — öğle saatleri belirgin kırpılıyor."""
    return simulate(sunny_tmy, _sized_layout(sunny_tmy, 1.6))


@pytest.fixture(scope="module")
def unclipped(sunny_tmy):
    """DC/AC ≈ 0,8 — invertör hiç doymuyor."""
    return simulate(sunny_tmy, _sized_layout(sunny_tmy, 0.8))


def test_clipping_setup_produces_the_intended_dc_ac_ratios(clipped, unclipped):
    """Aşağıdaki testler bu ayrıma dayanıyor; paketleme değişirse burada patlasın."""
    assert clipped.layout.dc_ac_ratio > 1.5
    assert unclipped.layout.dc_ac_ratio < 0.9
    assert clipped.layout.module_count == unclipped.layout.module_count


def test_clipping_loss_is_reported_only_when_the_inverter_saturates(clipped, unclipped):
    assert clipped.clipping_loss_kwh > 0.0
    assert unclipped.clipping_loss_kwh == pytest.approx(0.0)


def test_clipping_shows_up_in_the_waterfall(clipped, unclipped):
    def clipping_stage(result):
        return next(s for s in result.waterfall if "kırpma" in s.label)

    assert clipping_stage(clipped).loss_pct > 3.0
    assert clipping_stage(unclipped).loss_pct == pytest.approx(0.0)


def test_clipping_slows_the_apparent_degradation(clipped, unclipped):
    """Bozunum ve kırpma doğrusal etkileşir: yüksek DC/AC oranında ilk yıllarda
    bozunumun bir kısmı kırpılan tepeden yenir.

    Çıktıyı tek bir skalerle ölçeklemek bu etkiyi yok sayıp ilk yılların gelirini
    olduğundan az gösterirdi. Kırpılan sistemde ikinci yılın düşüşü, kırpılmayana
    göre daha yavaş olmalı.
    """
    def decline(result) -> float:
        return result.projection[1].energy_kwh / result.projection[0].energy_kwh

    assert decline(clipped) > decline(unclipped)


def test_clipping_reduces_specific_yield(clipped, unclipped):
    """Aynı panel sayısı, küçük invertör → kWp başına daha az enerji."""
    assert clipped.specific_yield_kwh_kwp < unclipped.specific_yield_kwh_kwp


# ------------------------------ Gölgelenme ------------------------------


def test_tilted_array_reports_row_shading(result):
    """Açılı çatıda sıra-arası gölgelenme modellenmeli; 0 çıkması modelin
    devre dışı kaldığını gösterirdi."""
    assert 0.0 < result.mean_shaded_fraction < 0.20


def test_roof_parallel_array_has_no_row_shading(tmy):
    min_ambient, max_cell = design_temperatures(tmy, MountType.ROOFTOP)
    parallel = pack_panels(
        ROOF,
        latitude=KONYA_LAT,
        longitude=KONYA_LON,
        min_ambient_c=min_ambient,
        max_cell_c=max_cell,
        inverter=InverterSpec(ac_kw=100.0),
        mounting=MountingSpec(mount=MountType.ROOFTOP, tilt_deg=15.0, setback_m=0.6),
    )

    assert simulate(tmy, parallel).mean_shaded_fraction == pytest.approx(0.0)


# ------------------------------ Belirsizlik ------------------------------


def test_p90_is_below_p50_and_p10_above(result):
    p50 = result.year_one_kwh

    assert result.percentile_kwh(0.90) < p50 < result.percentile_kwh(0.10)


def test_p90_factor_is_symmetric_with_p10(result):
    """Normal dağılım varsayımı: P90 ve P10 P50'ye eşit uzaklıkta olmalı."""
    p50 = result.year_one_kwh
    below = p50 - result.percentile_kwh(0.90)
    above = result.percentile_kwh(0.10) - p50

    assert below == pytest.approx(above, rel=1e-9)


def test_percentile_uses_the_combined_uncertainty(result):
    combined = result.uncertainty.combined
    assert combined == pytest.approx(YieldUncertainty().combined)
    assert 0.05 < combined < 0.08


# ------------------------------ Kayıp varsayımları ------------------------------


def test_losses_are_recorded_for_the_report(result):
    """Kurulmamış santralde kalibrasyon yok; varsayımlar raporda görünmek zorunda."""
    assert result.losses == LossChain()


def test_heavier_losses_reduce_annual_energy(tmy, layout):
    optimistic = simulate(tmy, layout, losses=LossChain())
    pessimistic = simulate(tmy, layout, losses=LossChain(soiling=0.10, dc_wiring=0.04))

    assert pessimistic.year_one_kwh < optimistic.year_one_kwh


def test_capacity_is_read_from_the_layout(result, layout):
    assert result.dc_capacity_kwp == pytest.approx(layout.dc_capacity_kwp)


# ------------------------------ Eğim karşılaştırması ------------------------------


def test_compare_tilts_returns_a_point_per_tilt(tmy, layout):
    tilts = (10.0, 20.0, 30.0)
    points = compare_tilts(tmy, layout, tilts=tilts)

    assert [tilt for tilt, _ in points] == list(tilts)
    assert all(yield_ > 0.0 for _, yield_ in points)


def test_optimal_tilt_for_konya_is_near_thirty_degrees(tmy, layout):
    """Enlem 37,9° için optimum eğim 25–35° aralığında olmalı."""
    points = compare_tilts(tmy, layout, tilts=(5.0, 15.0, 25.0, 30.0, 35.0, 45.0))
    best_tilt = max(points, key=lambda item: item[1])[0]

    assert 25.0 <= best_tilt <= 35.0


def test_compare_tilts_holds_module_count_fixed(tmy, layout):
    """Yalnızca eğim değişir; panel sayısını da değiştiren karşılaştırma NPV
    üzerinden yapılmalı, özgül üretim üzerinden değil."""
    points = compare_tilts(tmy, layout, tilts=(10.0, 40.0))

    assert len(points) == 2
    assert layout.module_count > 0  # yerleşim değişmedi
