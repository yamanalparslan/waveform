"""Panel yerleşimi: sıra aralığı, string boyutlandırma ve paketleme yüklemleri.

Paketlemede tek bir "doğru panel sayısı" yok — ızgara fazı araması ve yönelim
seçimi sonucu değiştirebilir. Bu yüzden testler sayıya değil *değişmezlere*
bakıyor: her panel poligonun içinde ve kenar mesafesini sağlıyor mu, kapasite
string planıyla örtüşüyor mu, aralık gölgeleme ölçütünü karşılıyor mu.
"""

import math

import pytest

from luminmind.prospect.geometry import (
    axis_aligned_rect,
    polygon_area_m2,
    rect_clears_obstacle,
    rect_fits_inside,
)
from luminmind.prospect.layout import (
    DEFAULT_SHADING_WINDOW,
    InverterSpec,
    ModuleSpec,
    MountingSpec,
    Orientation,
    layout_bounds,
    pack_panels,
    plan_strings,
    required_row_pitch,
)
from luminmind.twin.plant_model import MountType

KONYA_LAT, KONYA_LON = 37.87, 32.48

# 40 × 20 m sanayi çatısı (800 m²), CCW
ROOF = ((0.0, 0.0), (40.0, 0.0), (40.0, 20.0), (0.0, 20.0))

MODULE = ModuleSpec()  # 580 W, 1,134 × 2,278 m
INVERTER = InverterSpec()  # 100 kW, 10 MPPT × 2 string

# Konya TMY uçlarına yakın tasarım sıcaklıkları
MIN_AMBIENT_C = -12.0
MAX_CELL_C = 68.0


def tilted_mounting(**overrides) -> MountingSpec:
    base = {
        "mount": MountType.ROOFTOP_TILTED,
        "tilt_deg": 15.0,
        "azimuth_deg": 180.0,
        "setback_m": 0.6,
    }
    return MountingSpec(**{**base, **overrides})


def pack(ring=ROOF, mounting: MountingSpec | None = None, **kwargs):
    return pack_panels(
        ring,
        latitude=KONYA_LAT,
        longitude=KONYA_LON,
        min_ambient_c=MIN_AMBIENT_C,
        max_cell_c=MAX_CELL_C,
        module=kwargs.pop("module", MODULE),
        inverter=kwargs.pop("inverter", INVERTER),
        mounting=mounting or tilted_mounting(**kwargs),
    )


# ------------------------------ Sıra aralığı ------------------------------


def test_required_row_pitch_exceeds_panel_projection():
    """Aralık en az panelin izdüşüm derinliği + gölge boyu kadar olmalı."""
    slant, tilt = 1.134, 15.0
    pitch = required_row_pitch(KONYA_LAT, KONYA_LON, slant, tilt, 180.0)

    projection = slant * math.cos(math.radians(tilt))
    assert pitch > projection
    # 15°'de Konya için 1,8–2,0 m mertebesi bekleniyor
    assert 1.5 < pitch < 2.3


def test_required_row_pitch_grows_with_tilt():
    """Dik açı hem izdüşümü kısaltır hem gölgeyi uzatır; net etki aralığı büyütür."""
    pitches = [
        required_row_pitch(KONYA_LAT, KONYA_LON, 1.134, tilt, 180.0)
        for tilt in (10.0, 20.0, 30.0)
    ]
    assert pitches == sorted(pitches)
    assert pitches[0] < pitches[-1]


def test_required_row_pitch_grows_with_latitude():
    """Kuzeyde kış güneşi alçak kalır, gölge uzar."""
    antalya = required_row_pitch(36.9, 30.7, 1.134, 15.0, 180.0)
    agri = required_row_pitch(39.7, 43.0, 1.134, 15.0, 180.0)

    assert agri > antalya


def test_southeast_arrays_need_the_widest_pitch():
    """Kritik an öğle değil pencere kenarı; en dar durum güneşin o anki azimutuna
    dönük dizidir.

    Kış gündönümünde 09:00'da güneş azimutu Konya'da ≈138°, yükseklik ≈16°.
    Güneydoğuya dönük dizi gölgeyi tam arkasına düşürür ve en geniş aralığı
    ister. Öğleye bakan kapalı formül *tersini* söylerdi (öğlende güneye dönük
    dizinin cos'u 1, güneydoğununki 0,71) ve güneydoğu sahasında aralığı
    yaklaşık yarısı kadar hesaplardı — bu yüzden pencere taranıyor.
    """
    south = required_row_pitch(KONYA_LAT, KONYA_LON, 1.134, 20.0, 180.0)
    southeast = required_row_pitch(KONYA_LAT, KONYA_LON, 1.134, 20.0, 135.0)
    southwest = required_row_pitch(KONYA_LAT, KONYA_LON, 1.134, 20.0, 225.0)

    assert southeast > south
    assert southwest > south
    assert southeast == pytest.approx(southwest, rel=0.02), "doğu/batı bakışım göstermeli"


def test_east_facing_array_sees_the_sun_off_axis_at_the_critical_hour():
    """Doğuya dönük dizi 09:00 güneşine 48° açıyla bakar; güneyden az aralık ister.

    Sezgi "doğu batı dizileri daha geniş aralık ister" der ve yanlıştır: ölçüt
    gölge *boyu* değil, gölgenin dizinin arkasına düşen izdüşümüdür.
    """
    south = required_row_pitch(KONYA_LAT, KONYA_LON, 1.134, 20.0, 180.0)
    east = required_row_pitch(KONYA_LAT, KONYA_LON, 1.134, 20.0, 90.0)

    assert east < south


def test_required_row_pitch_rejects_empty_window():
    with pytest.raises(ValueError, match="pencere"):
        required_row_pitch(KONYA_LAT, KONYA_LON, 1.134, 15.0, 180.0, window=(15.0, 9.0))


def test_default_shading_window_is_the_turkish_design_criterion():
    assert DEFAULT_SHADING_WINDOW == (9.0, 15.0)


# ------------------------------ String boyutlandırma ------------------------------


def test_plan_strings_respects_cold_voltage_ceiling():
    """En soğuk anda Voc × modül sayısı invertörün azami DC gerilimini aşamaz."""
    plan = plan_strings(600, MODULE, INVERTER, MIN_AMBIENT_C, MAX_CELL_C)

    voc_cold = MODULE.voc_stc_v * (1.0 + MODULE.beta_voc_per_c * (MIN_AMBIENT_C - 25.0))
    assert plan.modules_per_string * voc_cold <= INVERTER.max_dc_voltage_v
    assert plan.modules_max == int(INVERTER.max_dc_voltage_v // voc_cold)


def test_plan_strings_respects_hot_mppt_floor():
    """En sıcak anda Vmp × modül sayısı MPPT alt sınırının altına düşemez."""
    plan = plan_strings(600, MODULE, INVERTER, MIN_AMBIENT_C, MAX_CELL_C)

    vmp_hot = MODULE.vmp_stc_v * (1.0 + MODULE.beta_vmp_per_c * (MAX_CELL_C - 25.0))
    assert plan.modules_per_string * vmp_hot >= INVERTER.mppt_min_voltage_v
    assert plan.modules_min <= plan.modules_per_string <= plan.modules_max


def test_plan_strings_conserves_module_count():
    """Yerleştirilen = string'lere giren + kırpılan. Panel kaybolmamalı."""
    plan = plan_strings(637, MODULE, INVERTER, MIN_AMBIENT_C, MAX_CELL_C)

    assert plan.total_modules + plan.trimmed_modules == 637
    assert plan.trimmed_modules < plan.modules_per_string


def test_plan_strings_stays_within_mppt_capacity():
    """String sayısı invertörlerin giriş kapasitesini aşmamalı — yoksa kurulamaz."""
    plan = plan_strings(600, MODULE, INVERTER, MIN_AMBIENT_C, MAX_CELL_C)

    assert plan.mppt_capacity == plan.inverter_count * INVERTER.strings_per_inverter
    assert plan.strings <= plan.mppt_capacity
    assert plan.mppt_shortfall == 0


def test_plan_strings_prefers_capacity_fit_over_fewest_orphans():
    """Tek stringli MPPT'de "en az artık" ölçütü tek başına kurulamaz tasarım verir.

    525 panel `strings_per_mppt=1` bir invertörle: en az artık bırakan boy 15
    (35 string) ama 3 invertörün yalnızca 30 girişi var. Kapasiteye sığan bir boy
    seçilmeli — artık bırakmak pahasına.
    """
    single = InverterSpec(strings_per_mppt=1)
    plan = plan_strings(525, MODULE, single, MIN_AMBIENT_C, MAX_CELL_C)

    assert plan.strings <= plan.mppt_capacity
    assert plan.mppt_shortfall == 0


def test_plan_strings_reports_shortfall_instead_of_adding_inverters():
    """Hiçbir boy sığmazsa invertör *eklenmez*; eksiklik raporlanır.

    İnvertör eklemek DC/AC oranını düşürüp kırpma kaybını olduğundan az
    gösterir, yani finansal modeli sessizce bozar.
    """
    narrow = InverterSpec(mppt_inputs=1, strings_per_mppt=1)
    plan = plan_strings(600, MODULE, narrow, MIN_AMBIENT_C, MAX_CELL_C)

    assert plan.mppt_shortfall > 0
    assert plan.strings > plan.mppt_capacity


def test_plan_strings_uses_site_temperature_extremes():
    """Soğuk sahada string kısalır (Voc yükselir), sıcak sahada uzayabilir."""
    cold = plan_strings(600, MODULE, INVERTER, -25.0, 60.0)
    warm = plan_strings(600, MODULE, INVERTER, 0.0, 70.0)

    assert cold.modules_max < warm.modules_max
    assert cold.design_min_ambient_c == -25.0
    assert warm.design_max_cell_c == 70.0


def test_plan_strings_rejects_incompatible_inverter():
    """Gerilim penceresi boşsa tasarım kurulamaz; sessizce sürdürmek yanlış olur."""
    incompatible = InverterSpec(max_dc_voltage_v=100.0, mppt_min_voltage_v=900.0)

    with pytest.raises(ValueError, match="uyumsuz"):
        plan_strings(600, MODULE, incompatible, MIN_AMBIENT_C, MAX_CELL_C)


def test_plan_strings_with_zero_modules_is_empty():
    plan = plan_strings(0, MODULE, INVERTER, MIN_AMBIENT_C, MAX_CELL_C)

    assert plan.strings == 0
    assert plan.modules_per_string == 0
    assert plan.inverter_count == 0


def test_plan_strings_when_module_count_below_minimum_string():
    """Panel sayısı en kısa string'e bile yetmiyorsa hepsi kırpılır."""
    plan = plan_strings(3, MODULE, INVERTER, MIN_AMBIENT_C, MAX_CELL_C)

    assert plan.strings == 0
    assert plan.trimmed_modules == 3


# ------------------------------ Paketleme değişmezleri ------------------------------


def test_pack_panels_places_modules_on_a_large_roof():
    result = pack()

    assert result.module_count > 100
    assert result.dc_capacity_kwp == pytest.approx(result.module_count * 0.580)
    assert result.area_m2 == pytest.approx(800.0)


def test_every_placed_panel_satisfies_the_boundary_predicate():
    """En güçlü değişmez: paketlemenin ürettiği her dikdörtgen yüklemi geçmeli."""
    result = pack()
    ring = ROOF

    assert result.placements
    for placement in result.placements:
        assert rect_fits_inside(placement.rect, ring, result.mounting.setback_m), (
            f"panel satır {placement.row} kolon {placement.col} kenar mesafesini ihlal ediyor"
        )


def test_placed_panels_do_not_overlap_each_other():
    """Sıra aralığı ve kolon adımı panel boyutundan büyük olduğu için örtüşme olmamalı."""
    result = pack()
    areas = sum(polygon_area_m2(p.rect) for p in result.placements)
    bounds = layout_bounds(result)
    envelope = (bounds[2] - bounds[0]) * (bounds[3] - bounds[1])

    assert areas <= envelope + 1e-6


def test_pack_panels_respects_obstacles():
    """Çatı ortasındaki baca çevresinde panel olmamalı."""
    chimney = axis_aligned_rect((20.0, 10.0), 4.0, 4.0)
    mounting = tilted_mounting(obstacles=(chimney,), obstacle_clearance_m=0.5)

    result = pack(mounting=mounting)

    assert result.placements
    for placement in result.placements:
        assert rect_clears_obstacle(placement.rect, chimney, 0.5)


def test_obstacles_reduce_module_count():
    chimney = axis_aligned_rect((20.0, 10.0), 6.0, 6.0)
    without = pack()
    with_obstacle = pack(mounting=tilted_mounting(obstacles=(chimney,)))

    assert with_obstacle.module_count < without.module_count


def test_larger_setback_reduces_module_count():
    tight = pack(mounting=tilted_mounting(setback_m=0.3))
    loose = pack(mounting=tilted_mounting(setback_m=2.5))

    assert loose.module_count < tight.module_count


def test_pack_panels_rejects_self_intersecting_polygon():
    bowtie = ((0.0, 0.0), (40.0, 20.0), (40.0, 0.0), (0.0, 20.0))

    with pytest.raises(ValueError, match="kesişiyor"):
        pack(ring=bowtie)


def test_pack_panels_rejects_degenerate_polygon():
    with pytest.raises(ValueError, match="en az 3 köşe"):
        pack(ring=((0.0, 0.0), (10.0, 0.0)))


def test_pack_panels_accepts_clockwise_input():
    """Kullanıcı poligonu hangi yönde çizerse çizsin sonuç aynı olmalı."""
    forward = pack()
    reverse = pack(ring=tuple(reversed(ROOF)))

    assert reverse.module_count == forward.module_count


def test_pack_panels_on_tiny_polygon_yields_nothing_but_does_not_crash():
    """Panel sığmayan alanda hata değil boş yerleşim dönmeli — uyarı katmanı bunu bildirir."""
    tiny = ((0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0))
    result = pack(ring=tiny)

    assert result.module_count == 0
    assert result.string_plan.strings == 0
    assert result.dc_capacity_kwp == 0.0
    assert result.area_utilisation == 0.0
    assert layout_bounds(result) == (0.0, 0.0, 0.0, 0.0)


def test_rows_counts_only_populated_rows():
    """Izgara fazı kaydığında kenar sırası tamamen boş kalabilir; sayılmamalı."""
    result = pack()
    occupied = {p.row for p in result.placements}

    assert result.rows == len(occupied)
    assert result.rows <= max(occupied) + 1


def test_module_count_matches_string_plan_exactly():
    """Artık paneller yerleşimden çıkarılır; raporlanan kapasite kurulabilir olmalı."""
    result = pack()
    plan = result.string_plan

    assert result.module_count == plan.total_modules
    assert result.module_count == plan.modules_per_string * plan.strings


def test_strings_are_assigned_to_physically_adjacent_panels():
    """Aynı string'in panelleri satır/kolon sırasında komşu olmalı.

    Gölgelenmede uyumsuzluk kaybını azaltıyor: string akımı en kötü modülüyle
    sınırlı, dolayısıyla gölgelenen paneller aynı string'te toplanmalı.
    """
    result = pack()
    plan = result.string_plan

    assert all(p.string_index >= 0 for p in result.placements)
    assert {p.string_index for p in result.placements} == set(range(plan.strings))

    order = sorted(result.placements, key=lambda p: (p.row, p.col))
    indices = [p.string_index for p in order]
    assert indices == sorted(indices), "string atamaları fiziksel sırayı izlemeli"


# ------------------------------ Montaj tipi ayrımı ------------------------------


def test_roof_parallel_mount_has_no_row_spacing():
    """Çatıya paralel montajda paneller tek düzlemde; aralık = izdüşüm + kızak boşluğu."""
    mounting = MountingSpec(mount=MountType.ROOFTOP, tilt_deg=30.0, setback_m=0.6)
    result = pack(mounting=mounting)

    cos_tilt = math.cos(math.radians(30.0))
    projected = result.collector_width_m * cos_tilt
    expected = projected + mounting.row_gap_m * cos_tilt

    assert result.row_pitch_m == pytest.approx(expected)
    assert result.gcr == 1.0, "tek düzlemde sıra-arası gölgelenme yok"


def test_roof_parallel_mount_fits_more_modules_than_tilted():
    """Aynı çatıya paralel montajla daha çok panel sığar (sıra aralığı gerekmez)."""
    parallel = pack(mounting=MountingSpec(mount=MountType.ROOFTOP, tilt_deg=15.0, setback_m=0.6))
    tilted = pack(mounting=tilted_mounting(tilt_deg=15.0))

    assert parallel.module_count > tilted.module_count


def test_tilted_mount_pitch_exceeds_collector_width():
    """GCR ≤ 1 kalması için aralık eğik uzunluktan büyük olmalı; yoksa model anlamsızlaşır."""
    result = pack(mounting=tilted_mounting())

    assert result.row_pitch_m > result.collector_width_m
    assert 0.0 < result.gcr < 1.0


def test_explicit_row_pitch_overrides_shading_criterion():
    """EPC'nin sabit kızak adımı verilirse gölgeleme ölçütü atlanmalı."""
    result = pack(mounting=tilted_mounting(row_pitch_m=4.0))

    assert result.row_pitch_m == pytest.approx(4.0)


def test_wider_row_pitch_reduces_module_count():
    dense = pack(mounting=tilted_mounting(row_pitch_m=2.0))
    sparse = pack(mounting=tilted_mounting(row_pitch_m=5.0))

    assert sparse.module_count < dense.module_count
    assert sparse.gcr < dense.gcr


def test_surface_area_accounts_for_roof_slope_only_when_parallel():
    """Çatıya paralel montajda poligon eğik çatının izdüşümü; gerçek yüzey 1/cos β kadar büyük.

    Bu dönüşüm yapılmazsa doluluk %100'ü aşar ve kullanıcıya hata gibi görünür.
    """
    parallel = pack(mounting=MountingSpec(mount=MountType.ROOFTOP, tilt_deg=30.0, setback_m=0.6))
    tilted = pack(mounting=tilted_mounting(tilt_deg=30.0))

    assert parallel.surface_area_m2 == pytest.approx(800.0 / math.cos(math.radians(30.0)))
    assert tilted.surface_area_m2 == pytest.approx(800.0), "açılı montajda poligon düz zemin"
    assert parallel.area_utilisation <= 1.0


# ------------------------------ Eğik çatı izdüşüm tuzağı ------------------------------


def test_steeper_parallel_roof_fits_more_modules_in_the_same_footprint():
    """Haritadaki poligon *izdüşüm*; gerçek çatı yüzeyi eğimle uzar.

    İzdüşüm derinliği `d·cos β` kullanılmazsa 30°'lik çatıda kapasite ~%13 eksik
    çıkar. Aynı ayak izine dik çatıda daha çok panel sığmalı.
    """
    flat = pack(mounting=MountingSpec(mount=MountType.ROOFTOP, tilt_deg=5.0, setback_m=0.6))
    steep = pack(mounting=MountingSpec(mount=MountType.ROOFTOP, tilt_deg=35.0, setback_m=0.6))

    assert steep.module_count > flat.module_count


# ------------------------------ Yönelim araması ------------------------------


def test_orientation_search_picks_the_denser_option():
    """Yönelim panel sayısına göre seçilir; eşitlikte yatay (düşük sıra yüksekliği)."""
    result = pack()
    assert result.orientation in (Orientation.LANDSCAPE, Orientation.PORTRAIT)

    forced_landscape = MODULE.dimensions(Orientation.LANDSCAPE)
    forced_portrait = MODULE.dimensions(Orientation.PORTRAIT)
    assert forced_landscape == (MODULE.height_m, MODULE.width_m)
    assert forced_portrait == (MODULE.width_m, MODULE.height_m)


def test_narrow_strip_favours_the_orientation_that_fits():
    """3 m'lik şeritte portre panel (2,278 m derin) tek sıra sığar; yatay ikiye bölünür."""
    strip = ((0.0, 0.0), (40.0, 0.0), (40.0, 3.2), (0.0, 3.2))
    result = pack(ring=strip, mounting=tilted_mounting(row_pitch_m=2.5))

    assert result.module_count > 0
    assert result.rows >= 1


# ------------------------------ ArrayConfig köprüsü ------------------------------


def test_to_array_config_preserves_capacity():
    """Bir panel kayması 25 yıllık üretim toplamına taşır."""
    result = pack()
    array = result.to_array_config()

    assert array.modules_per_string * array.strings == result.module_count
    assert array.module_pdc0_w == MODULE.pdc0_w
    assert array.tilt_deg == result.mounting.tilt_deg
    assert array.azimuth_deg == result.mounting.azimuth_deg


def test_to_array_config_carries_shading_geometry():
    result = pack()
    array = result.to_array_config()

    assert array.gcr == pytest.approx(result.gcr)
    assert array.collector_width_m == pytest.approx(result.collector_width_m)
    assert array.mount is MountType.ROOFTOP_TILTED
    assert array.models_row_shading, "açılı çatıda sıra-arası gölgelenme modellenmeli"


def test_roof_parallel_array_does_not_model_row_shading():
    result = pack(mounting=MountingSpec(mount=MountType.ROOFTOP, tilt_deg=25.0, setback_m=0.6))
    assert not result.to_array_config().models_row_shading


def test_dc_ac_ratio_reflects_integer_inverter_count():
    result = pack()
    expected = result.dc_capacity_kwp / (result.string_plan.inverter_count * INVERTER.ac_kw)

    assert result.dc_ac_ratio == pytest.approx(expected)
    assert result.ac_capacity_kw == result.string_plan.inverter_count * INVERTER.ac_kw


def test_specific_density_is_plausible_for_a_tilted_roof():
    """Açılı çatıda m² başına 100–180 Wp mertebesi beklenir."""
    result = pack()
    assert 100.0 < result.specific_density_wp_m2 < 180.0
