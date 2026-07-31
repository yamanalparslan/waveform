"""Fizibilite akışının orkestrasyonu: poligon → TMY → yerleşim → üretim → para.

`service.analyse` iki sınır işini üstleniyor ve testler oraya bakıyor:

1. **Koordinat sınırı** — kullanıcı WGS84 çizer, yerleşim metre ister, rapor yine
   WGS84 saklar. Dönüşüm tek yerde; panel köşelerinin çatının üstüne düşmesi bu
   dönüşümün doğruluğuna bağlı.
2. **Uyarı sınırı** — alt modüller sessizce en iyisini yapar (string'e sığmayan
   paneli düşürür, MPPT'ye sığan boyu seçer). Bu kararlar kullanıcıya
   söylenmezse rapor olduğundan emin görünür.

PVGIS ağ çağrısı sahte bir istemciyle değiştiriliyor — TMY'nin *içeriği* değil,
akışın onu doğru yere taşıdığı sınanıyor.
"""

import math
import uuid

import pytest

from luminmind.core.models.prospect import ProspectDesign, ProspectStatus
from luminmind.prospect.finance import CostModel, FinanceParams, RevenueModel
from luminmind.prospect.geometry import LatLon, LocalFrame, point_in_ring, polygon_area_m2
from luminmind.prospect.pvgis import HorizonProfile
from luminmind.prospect.service import (
    ENGINE_VERSION,
    analyse,
    build_mounting,
    to_report,
)
from luminmind.twin.plant_model import MountType

KONYA_LAT, KONYA_LON = 37.87, 32.48

# Konya'da ~40 × 20 m'lik bir çatı, WGS84 köşeler
ROOF_WGS84 = [
    [KONYA_LAT, KONYA_LON],
    [KONYA_LAT, KONYA_LON + 0.000454],  # ≈ 40 m doğu
    [KONYA_LAT + 0.000180, KONYA_LON + 0.000454],  # ≈ 20 m kuzey
    [KONYA_LAT + 0.000180, KONYA_LON],
]


class FakePvgis:
    """Ağ erişimi olmayan PVGIS ikizi. Kapatılıp kapatılmadığı da izlenir."""

    def __init__(
        self,
        build_tmy,
        horizon: HorizonProfile | None = None,
        horizon_error: bool = False,
    ):
        self._build_tmy = build_tmy
        self._horizon = horizon or HorizonProfile(
            azimuth_deg=(90.0, 180.0, 270.0), elevation_deg=(1.0, 1.5, 2.0)
        )
        self._horizon_error = horizon_error
        self.closed = False
        self.tmy_calls: list[tuple[float, float]] = []

    async def fetch_tmy(self, latitude: float, longitude: float):
        self.tmy_calls.append((latitude, longitude))
        return self._build_tmy(latitude=latitude, longitude=longitude)

    async def fetch_horizon(self, latitude: float, longitude: float) -> HorizonProfile:
        if self._horizon_error:
            raise RuntimeError("PVGIS ufuk uç noktası yanıt vermedi")
        return self._horizon

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture(scope="session")
def pvgis(synthetic_tmy):
    """Sahte istemci kurucusu — `pvgis()` ya da `pvgis(horizon=…)`."""

    def _make(**kwargs) -> FakePvgis:
        return FakePvgis(synthetic_tmy, **kwargs)

    return _make


def make_design(**overrides) -> ProspectDesign:
    fields: dict[str, object] = {
        "id": uuid.uuid4(),
        "owner_id": uuid.uuid4(),
        "name": "Konya sanayi çatısı",
        "status": ProspectStatus.DRAFT,
        "latitude": KONYA_LAT,
        "longitude": KONYA_LON,
        "polygon": ROOF_WGS84,
        "obstacles": [],
        "mount_type": "rooftop_tilted",
        "tilt_deg": 15.0,
        "azimuth_deg": 180.0,
        "setback_m": 0.6,
        "obstacle_clearance_m": 0.5,
        "row_pitch_m": None,
        "module_spec": {},
        "inverter_spec": {},
    }
    fields.update(overrides)
    return ProspectDesign(**fields)


@pytest.fixture(scope="module")
async def analysis(pvgis):
    return await analyse(make_design(), client=pvgis(), fetch_horizon=True)


# ------------------------------ Akış ------------------------------


async def test_analyse_runs_the_whole_chain(analysis):
    assert analysis.layout.module_count > 0
    assert analysis.simulation.year_one_kwh > 0.0
    assert analysis.finance.capex_try > 0.0
    assert analysis.tmy is not None


async def test_analyse_passes_the_design_location_to_pvgis(pvgis):
    client = pvgis()
    await analyse(make_design(), client=client)

    assert client.tmy_calls == [(KONYA_LAT, KONYA_LON)]


async def test_analyse_does_not_close_a_borrowed_client(pvgis):
    """Toplu analizde istemci yeniden kullanılıyor; kapatmak sonraki çağrıyı kırardı."""
    client = pvgis()
    await analyse(make_design(), client=client)

    assert not client.closed


async def test_analyse_can_skip_the_horizon_fetch(pvgis):
    analysed = await analyse(make_design(), client=pvgis(), fetch_horizon=False)
    assert analysed.horizon is None


async def test_analyse_survives_a_horizon_failure(pvgis):
    """Ufuk profili yalnızca gösterim süsü; alınamazsa analiz durmamalı."""
    analysed = await analyse(
        make_design(), client=pvgis(horizon_error=True), fetch_horizon=True
    )

    assert analysed.horizon is None
    assert analysed.simulation.year_one_kwh > 0.0


async def test_analyse_forwards_finance_overrides(pvgis):
    analysed = await analyse(
        make_design(),
        client=pvgis(),
        costs=CostModel(capex_per_kwp_try=25_000.0),
        revenue=RevenueModel(retail_tariff_try_kwh=5.0, self_consumption_share=1.0),
        params=FinanceParams(discount_rate_real=0.08),
    )

    assert analysed.finance.specific_capex_try_kwp == pytest.approx(25_000.0)
    assert analysed.finance.blended_tariff_try_kwh == pytest.approx(5.0)
    assert analysed.finance.discount_rate_real == pytest.approx(0.08)


# ------------------------------ Girdi doğrulama ------------------------------


async def test_analyse_rejects_a_polygon_with_too_few_corners(pvgis):
    design = make_design(polygon=[[KONYA_LAT, KONYA_LON], [KONYA_LAT, KONYA_LON + 0.001]])

    with pytest.raises(ValueError, match="en az 3 köşe"):
        await analyse(design, client=pvgis())


async def test_analyse_rejects_a_polygon_that_is_too_small(pvgis):
    """20 m²'nin altına panel sığmaz; kullanıcıya anlaşılır hata dönmeli."""
    tiny = [
        [KONYA_LAT, KONYA_LON],
        [KONYA_LAT, KONYA_LON + 0.00003],
        [KONYA_LAT + 0.00003, KONYA_LON + 0.00003],
    ]

    with pytest.raises(ValueError, match="alan çok küçük"):
        await analyse(make_design(polygon=tiny), client=pvgis())


async def test_analyse_rejects_a_self_intersecting_polygon(pvgis):
    bowtie = [
        [KONYA_LAT, KONYA_LON],
        [KONYA_LAT + 0.00018, KONYA_LON + 0.00045],
        [KONYA_LAT, KONYA_LON + 0.00045],
        [KONYA_LAT + 0.00018, KONYA_LON],
    ]

    with pytest.raises(ValueError, match="kesişiyor"):
        await analyse(make_design(polygon=bowtie), client=pvgis())


# ------------------------------ Koordinat sınırı ------------------------------


async def test_local_frame_is_centred_on_the_polygon(analysis):
    """Teğet düzlem poligonun kendi merkezine oturmalı; ölçekler o enlemde geçerli."""
    expected_lat = sum(p[0] for p in ROOF_WGS84) / 4.0
    expected_lon = sum(p[1] for p in ROOF_WGS84) / 4.0

    assert analysis.frame.origin_lat == pytest.approx(expected_lat)
    assert analysis.frame.origin_lon == pytest.approx(expected_lon)


async def test_polygon_area_matches_its_ground_dimensions(analysis):
    """Köşeler ≈40 × 20 m seçildi; alan oraya düşmeli."""
    assert analysis.layout.area_m2 == pytest.approx(800.0, rel=0.03)


async def test_every_panel_is_returned_in_wgs84(analysis):
    assert len(analysis.panels_wgs84) == analysis.layout.module_count

    for rect in analysis.panels_wgs84:
        assert len(rect) == 4
        for lat, lon in rect:
            assert KONYA_LAT - 0.001 < lat < KONYA_LAT + 0.002
            assert KONYA_LON - 0.001 < lon < KONYA_LON + 0.002


async def test_wgs84_panels_land_inside_the_roof_polygon(analysis):
    """Dönüşüm yönü ters çevrilse paneller çatının dışına düşerdi — haritada
    görünür ama sessiz bir hata olurdu."""
    frame = LocalFrame.centered_on([LatLon(lat=p[0], lon=p[1]) for p in ROOF_WGS84])
    roof_local = frame.ring_to_local([LatLon(lat=p[0], lon=p[1]) for p in ROOF_WGS84])

    for rect in analysis.panels_wgs84:
        centre_lat = sum(lat for lat, _ in rect) / 4.0
        centre_lon = sum(lon for _, lon in rect) / 4.0
        centre = frame.to_local(LatLon(lat=centre_lat, lon=centre_lon))
        assert point_in_ring(centre, roof_local)


async def test_wgs84_panels_are_drawn_as_horizontal_projections(analysis):
    """Haritadaki dikdörtgen panelin *izdüşümü*, gerçek yüzeyi değil.

    Üstten görünüşte 15°'ye yatırılmış panel eğim yönünde cos β kadar kısa
    görünür. Datasheet alanı çizilseydi paneller çatıda olduğundan büyük durur ve
    sıralar üst üste binmiş görünürdü. Dönüşümde ölçek hatası varsa bu oran sapar.
    """
    frame = analysis.frame
    rect = analysis.panels_wgs84[0]
    local = frame.ring_to_local([LatLon(lat=lat, lon=lon) for lat, lon in rect])

    cos_tilt = math.cos(math.radians(analysis.layout.mounting.tilt_deg))
    assert polygon_area_m2(local) == pytest.approx(
        analysis.layout.module.area_m2 * cos_tilt, rel=1e-6
    )


async def test_obstacles_are_converted_into_the_local_frame():
    """Engeller de WGS84 girer, metre olarak yerleşime verilir."""
    chimney = [
        [KONYA_LAT + 0.00008, KONYA_LON + 0.00020],
        [KONYA_LAT + 0.00008, KONYA_LON + 0.00025],
        [KONYA_LAT + 0.00012, KONYA_LON + 0.00025],
        [KONYA_LAT + 0.00012, KONYA_LON + 0.00020],
    ]
    design = make_design(obstacles=[chimney])
    frame = LocalFrame.centered_on([LatLon(lat=p[0], lon=p[1]) for p in ROOF_WGS84])

    mounting = build_mounting(design, frame)

    assert len(mounting.obstacles) == 1
    assert polygon_area_m2(mounting.obstacles[0]) == pytest.approx(4.4 * 4.4, rel=0.1)


async def test_obstacles_reduce_the_module_count(pvgis):
    big_unit = [
        [KONYA_LAT + 0.00005, KONYA_LON + 0.00015],
        [KONYA_LAT + 0.00005, KONYA_LON + 0.00030],
        [KONYA_LAT + 0.00013, KONYA_LON + 0.00030],
        [KONYA_LAT + 0.00013, KONYA_LON + 0.00015],
    ]
    clear = await analyse(make_design(), client=pvgis())
    blocked = await analyse(make_design(obstacles=[big_unit]), client=pvgis())

    assert blocked.layout.module_count < clear.layout.module_count


# ------------------------------ build_mounting ------------------------------


async def test_build_mounting_reads_the_design_fields():
    design = make_design(
        mount_type="fixed_ground",
        tilt_deg=28.0,
        azimuth_deg=195.0,
        setback_m=1.4,
        obstacle_clearance_m=0.8,
        row_pitch_m=5.0,
    )
    frame = LocalFrame.at(KONYA_LAT, KONYA_LON)

    mounting = build_mounting(design, frame)

    assert mounting.mount is MountType.FIXED_GROUND
    assert mounting.tilt_deg == 28.0
    assert mounting.azimuth_deg == 195.0
    assert mounting.setback_m == 1.4
    assert mounting.obstacle_clearance_m == 0.8
    assert mounting.row_pitch_m == 5.0


async def test_build_mounting_tolerates_null_obstacles():
    """Eski kayıtlarda `obstacles` NULL olabilir; boş listeye düşmeli."""
    frame = LocalFrame.at(KONYA_LAT, KONYA_LON)
    assert build_mounting(make_design(obstacles=None), frame).obstacles == ()


async def test_equipment_specs_override_the_defaults(pvgis):
    design = make_design(
        module_spec={"name": "Test 400 W", "pdc0_w": 400.0, "width_m": 1.0, "height_m": 1.7},
        inverter_spec={"name": "Test 50 kW", "ac_kw": 50.0},
    )
    analysed = await analyse(design, client=pvgis())

    assert analysed.layout.module.pdc0_w == 400.0
    assert analysed.layout.module.name == "Test 400 W"
    assert analysed.layout.inverter.ac_kw == 50.0


# ------------------------------ Uyarılar ------------------------------


async def test_a_healthy_design_reports_no_alarming_warnings(analysis):
    joined = " ".join(analysis.warnings)

    assert "hiç panel sığmadı" not in joined
    assert "string fazla" not in joined


async def test_mppt_shortfall_is_reported_to_the_user(pvgis):
    """İnvertör eklenmiyor, eksiklik söyleniyor — karar EPC'nin."""
    design = make_design(inverter_spec={"mppt_inputs": 1, "strings_per_mppt": 1})
    analysed = await analyse(design, client=pvgis())

    assert analysed.layout.string_plan.mppt_shortfall > 0
    assert any("string fazla" in warning for warning in analysed.warnings)


async def test_trimmed_modules_are_reported(pvgis):
    """Tam string'e sığmayan paneller düşürülüyor; kapasite onları içermiyor."""
    analysed = await analyse(make_design(), client=pvgis())

    if analysed.layout.string_plan.trimmed_modules > 0:
        assert any("tasarımdan" in warning for warning in analysed.warnings)


async def test_off_south_azimuth_is_reported(pvgis):
    analysed = await analyse(make_design(azimuth_deg=90.0), client=pvgis())

    assert any("güneyden" in warning for warning in analysed.warnings)


async def test_south_facing_azimuth_is_not_flagged(analysis):
    assert not any("güneyden" in warning for warning in analysis.warnings)


async def test_low_area_utilisation_is_reported(pvgis):
    """Geniş kenar mesafesi alanı yiyor; kullanıcı nedenini bilmeli."""
    analysed = await analyse(make_design(setback_m=5.5), client=pvgis())

    assert analysed.layout.area_utilisation < 0.25
    assert any("Yüzey doluluğu düşük" in warning for warning in analysed.warnings)


async def test_raised_horizon_is_reported_without_double_counting(pvgis):
    """TMY ışınımı arazi ufkunu zaten içeriyor; uyarı bunu açıkça söylemeli."""
    steep = HorizonProfile(azimuth_deg=(90.0, 180.0), elevation_deg=(4.0, 12.0))
    analysed = await analyse(make_design(), client=pvgis(horizon=steep))

    warning = next(w for w in analysed.warnings if "ufku" in w)
    assert "zaten içeriyor" in warning


async def test_flat_horizon_is_not_flagged(analysis):
    assert not any("ufku" in warning for warning in analysis.warnings)


async def test_wide_row_pitch_avoids_the_shading_warning(pvgis):
    analysed = await analyse(make_design(row_pitch_m=8.0), client=pvgis())

    assert analysed.simulation.mean_shaded_fraction < 0.08
    assert not any("gölgelenme" in warning for warning in analysed.warnings)


# ------------------------------ Varsayım anlık görüntüsü ------------------------------


async def test_assumptions_capture_every_model_input(analysis):
    """Rapor yeniden üretilebilir olmalı: hangi kayıp, tarife, modül kullanıldı."""
    assumptions = analysis.assumptions

    assert set(assumptions) == {"losses", "uncertainty", "module", "inverter",
                                "mounting", "finance"}
    assert assumptions["module"]["pdc0_w"] == analysis.layout.module.pdc0_w
    assert assumptions["finance"]["capex_try"] == pytest.approx(analysis.finance.capex_try)
    assert assumptions["losses"], "kayıp zinciri boş olmamalı"


async def test_assumptions_summarise_obstacles_instead_of_embedding_them(pvgis):
    """Engel poligonları tasarımda zaten var; rapora ikinci kez kopyalanmamalı."""
    chimney = [
        [KONYA_LAT + 0.00008, KONYA_LON + 0.00020],
        [KONYA_LAT + 0.00008, KONYA_LON + 0.00025],
        [KONYA_LAT + 0.00012, KONYA_LON + 0.00025],
    ]
    analysed = await analyse(make_design(obstacles=[chimney]), client=pvgis())
    mounting = analysed.assumptions["mounting"]

    assert mounting["obstacle_count"] == 1
    assert "obstacles" not in mounting


async def test_assumptions_are_json_serialisable(analysis):
    """JSON sütununa yazılıyor; dataclass sızarsa commit anında patlardı."""
    import json

    assert json.loads(json.dumps(analysis.assumptions))


# ------------------------------ to_report ------------------------------


@pytest.fixture(scope="module")
async def report(analysis):
    return to_report(make_design(), analysis)


async def test_report_records_the_engine_and_data_version(report):
    assert report.engine_version == ENGINE_VERSION
    assert "PVGIS" in report.data_provenance
    assert len(report.data_provenance) <= 300


async def test_report_copies_the_layout_metrics(report, analysis):
    assert report.module_count == analysis.layout.module_count
    assert report.dc_capacity_kwp == pytest.approx(analysis.layout.dc_capacity_kwp)
    assert report.ac_capacity_kw == pytest.approx(analysis.layout.ac_capacity_kw)
    assert report.gcr == pytest.approx(analysis.layout.gcr)
    assert report.orientation == str(analysis.layout.orientation)
    assert report.strings == analysis.layout.string_plan.strings
    assert report.inverter_count == analysis.layout.string_plan.inverter_count


async def test_report_copies_the_production_metrics(report, analysis):
    assert report.year_one_kwh == pytest.approx(analysis.simulation.year_one_kwh)
    assert report.specific_yield_kwh_kwp == pytest.approx(
        analysis.simulation.specific_yield_kwh_kwp
    )
    assert report.performance_ratio == pytest.approx(analysis.simulation.performance_ratio)
    assert report.lifetime_kwh == pytest.approx(analysis.simulation.lifetime_kwh)


async def test_report_stores_p90_below_p50(report):
    """P90 finansman senaryosu; P50'nin altında olmak zorunda."""
    assert report.p90_year_one_kwh < report.year_one_kwh


async def test_report_copies_the_finance_metrics(report, analysis):
    assert report.capex_try == pytest.approx(analysis.finance.capex_try)
    assert report.npv_try == pytest.approx(analysis.finance.npv_try)
    assert report.lcoe_try_kwh == pytest.approx(analysis.finance.lcoe_try_kwh)
    assert report.irr_real == pytest.approx(analysis.finance.irr_real)


async def test_report_layout_is_plain_json_lists(report, analysis):
    """JSON sütunu tuple kabul etmez; dönüşüm burada yapılmalı."""
    assert isinstance(report.layout, list)
    assert len(report.layout) == analysis.layout.module_count

    first = report.layout[0]
    assert isinstance(first, list) and len(first) == 4
    assert all(isinstance(corner, list) and len(corner) == 2 for corner in first)


async def test_report_monthly_and_series_are_lists(report):
    assert isinstance(report.monthly_kwh, list) and len(report.monthly_kwh) == 12
    assert isinstance(report.waterfall, list) and len(report.waterfall) == 7
    assert isinstance(report.projection, list) and len(report.projection) == 25
    assert isinstance(report.warnings, list)


async def test_report_waterfall_rows_carry_labels_and_losses(report):
    for stage in report.waterfall:
        assert set(stage) == {"label", "energy_kwh", "loss_kwh", "loss_pct"}


async def test_report_projection_rows_carry_the_year(report):
    years = [row["year"] for row in report.projection]
    assert years == list(range(1, 26))


async def test_report_is_fully_json_serialisable(report):
    """Rapor dondurulmuş olmak zorunda; JSON'a çevrilemeyen alan commit'i kırardı."""
    import json

    payload = {
        "layout": report.layout,
        "monthly_kwh": report.monthly_kwh,
        "waterfall": report.waterfall,
        "projection": report.projection,
        "assumptions": report.assumptions,
        "warnings": report.warnings,
    }
    assert json.loads(json.dumps(payload))


async def test_report_points_at_its_design(analysis):
    design = make_design()
    built = to_report(design, analysis)

    assert built.design_id == design.id
