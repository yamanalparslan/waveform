"""Fizibilite akışının orkestrasyonu: poligon → TMY → yerleşim → üretim → para.

Alt modüller birbirini tanımaz; bu dosya onları sıraya dizer ve iki sınır işini
üstlenir:

**1. Koordinat sınırı.** Kullanıcı WGS84 çizer, yerleşim metre ister, harita
yine WGS84 bekler. Dönüşüm tek yerde (`geometry.LocalFrame`) ve tek yönde
kurulur; panel dikdörtgenleri hesaptan sonra bir kez WGS84'e çevrilip raporda
saklanır, böylece rapor sayfası yerleşimi yeniden koşturmaz.

**2. Uyarı sınırı.** Alt modüller sessizce en iyisini yapar (MPPT'ye sığan
string boyunu seçer, artık paneli düşürür). Bu kararların *kullanıcıya
söylenmesi* gerekir, yoksa rapor olduğundan emin görünür. Uyarılar burada
toplanıp raporla birlikte donar.
"""

import logging
from dataclasses import asdict, dataclass
from typing import Any

from luminmind.core.models.prospect import ProspectDesign, ProspectReport
from luminmind.prospect.finance import (
    CostModel,
    FinanceParams,
    FinanceResult,
    RevenueModel,
    evaluate,
)
from luminmind.prospect.geometry import (
    LatLon,
    LocalFrame,
    normalize_ring,
    polygon_area_m2,
    ring_is_simple,
)
from luminmind.prospect.layout import (
    InverterSpec,
    LayoutResult,
    ModuleSpec,
    MountingSpec,
    pack_panels,
)
from luminmind.prospect.pvgis import HorizonProfile, PvgisClient, TmyDataset
from luminmind.prospect.simulate import SimulationResult, design_temperatures, simulate
from luminmind.twin.components import LossChain
from luminmind.twin.plant_model import MountType

logger = logging.getLogger(__name__)

ENGINE_VERSION = "prospect-v1"

# Bu değerlerin altında/üstünde kullanıcıya uyarı gösterilir.
_MIN_USEFUL_AREA_M2 = 20.0
_AZIMUTH_WARNING_DEG = 60.0  # güneyden bu kadar sapma verimi belirgin düşürür
_LOW_UTILISATION = 0.25


@dataclass(frozen=True)
class ProspectAnalysis:
    """Tam analiz — rapora yazılacak her şey burada."""

    tmy: TmyDataset
    layout: LayoutResult
    simulation: SimulationResult
    finance: FinanceResult
    horizon: HorizonProfile | None
    frame: LocalFrame
    panels_wgs84: tuple[tuple[tuple[float, float], ...], ...]
    warnings: tuple[str, ...]

    @property
    def assumptions(self) -> dict[str, Any]:
        """Hesabın tüm girdileri — raporu yeniden üretilebilir kılan anlık görüntü."""
        return {
            "losses": asdict(self.simulation.losses),
            "uncertainty": asdict(self.simulation.uncertainty),
            "module": asdict(self.layout.module),
            "inverter": asdict(self.layout.inverter),
            "mounting": {
                **{
                    k: v
                    for k, v in asdict(self.layout.mounting).items()
                    if k != "obstacles"
                },
                "obstacle_count": len(self.layout.mounting.obstacles),
            },
            "finance": {
                "discount_rate_real": self.finance.discount_rate_real,
                "exceedance": self.finance.exceedance,
                "blended_tariff_try_kwh": self.finance.blended_tariff_try_kwh,
                "capex_try": self.finance.capex_try,
            },
        }


def _collect_warnings(
    layout: LayoutResult,
    simulation: SimulationResult,
    horizon: HorizonProfile | None,
) -> tuple[str, ...]:
    """Kullanıcının bilmesi gereken tasarım kusurları."""
    notes: list[str] = []
    plan = layout.string_plan

    if layout.module_count == 0:
        notes.append(
            "Bu alana hiç panel sığmadı. Kenar mesafesini küçültmeyi veya daha "
            "küçük modül seçmeyi deneyin."
        )
    if plan.mppt_shortfall > 0:
        notes.append(
            f"{plan.strings} string için {plan.mppt_capacity} MPPT girişi var — "
            f"{plan.mppt_shortfall} string fazla. Daha fazla MPPT girişi olan bir "
            "invertör seçilmeli; hesap mevcut invertör sayısıyla yapıldı."
        )
    if plan.trimmed_modules > 0:
        notes.append(
            f"{plan.trimmed_modules} panel tam string'e sığmadığı için tasarımdan "
            "çıkarıldı; kapasite bu panelleri içermiyor."
        )
    if layout.area_utilisation < _LOW_UTILISATION and layout.module_count > 0:
        notes.append(
            f"Yüzey doluluğu düşük (%{layout.area_utilisation * 100:.0f}). Engeller, "
            "kenar mesafesi veya geniş sıra aralığı alanı yiyor olabilir."
        )
    azimuth_offset = abs(((layout.mounting.azimuth_deg - 180.0 + 180.0) % 360.0) - 180.0)
    if azimuth_offset > _AZIMUTH_WARNING_DEG:
        notes.append(
            f"Diziler güneyden {azimuth_offset:.0f}° sapmış; özgül üretim güneye "
            "dönük bir tasarımın belirgin altında kalır."
        )
    if horizon is not None and not horizon.is_flat:
        notes.append(
            f"Arazi ufku {horizon.max_elevation_deg:.1f}° yükseliyor (tepe/vadi "
            "etkisi). PVGIS ışınımı bunu zaten içeriyor, ek düzeltme yapılmadı."
        )
    if simulation.mean_shaded_fraction > 0.08:
        notes.append(
            f"Sıra-arası gölgelenme ortalama %{simulation.mean_shaded_fraction * 100:.1f} "
            "— sıra aralığını artırmak özgül üretimi yükseltir (ama panel sayısını düşürür)."
        )
    if layout.dc_ac_ratio > 1.35:
        notes.append(
            f"DC/AC oranı {layout.dc_ac_ratio:.2f}; öğle saatlerinde kırpma kaybı "
            f"{simulation.clipping_loss_kwh / 1000:.1f} MWh/yıl."
        )
    return tuple(notes)


def build_mounting(design: ProspectDesign, frame: LocalFrame) -> MountingSpec:
    """Tasarım kaydından montaj kısıtlarını kurar (engeller yerel çerçevede)."""
    from luminmind.prospect.layout import Obstacle
    obstacles = []
    for obs_data in (design.obstacles or []):
        if isinstance(obs_data, dict) and "polygon" in obs_data:
            points = obs_data["polygon"]
            height = obs_data.get("height", 0.0)
        else:
            points = obs_data
            height = 0.0
        local_ring = frame.ring_to_local([LatLon(lat=p[0], lon=p[1]) for p in points])
        obstacles.append(Obstacle(polygon=local_ring, height_m=height))
    obstacles = tuple(obstacles)
    return MountingSpec(
        mount=MountType(design.mount_type),
        tilt_deg=design.tilt_deg,
        azimuth_deg=design.azimuth_deg,
        setback_m=design.setback_m,
        obstacles=obstacles,
        obstacle_clearance_m=design.obstacle_clearance_m,
        row_pitch_m=design.row_pitch_m,
    )


async def analyse(
    design: ProspectDesign,
    costs: CostModel | None = None,
    revenue: RevenueModel | None = None,
    params: FinanceParams | None = None,
    losses: LossChain | None = None,
    client: PvgisClient | None = None,
    fetch_horizon: bool = True,
) -> ProspectAnalysis:
    """Tasarımı uçtan uca hesaplar.

    `client` verilirse yeniden kullanılır (toplu analizde bağlantı açmamak için);
    verilmezse bu çağrı için açılıp kapatılır.
    """
    points = [LatLon(lat=point[0], lon=point[1]) for point in design.polygon]
    if len(points) < 3:
        raise ValueError("poligon en az 3 köşe içermeli")
    frame = LocalFrame.centered_on(points)
    ring = normalize_ring(frame.ring_to_local(points))
    # Öz-kesişim *alandan önce* denetlenmeli. Ayakkabı bağı formülü kesişen
    # lobları ters işaretle sayar, dolayısıyla kelebek biçimli bir çizimde alan
    # sıfıra yakın çıkar ve alan denetimi "alan çok küçük (0.0 m²)" diyerek
    # gerçek sorunun üstünü örter. Kullanıcının haritada yaptığı hata çizimin
    # kendisidir; mesaj onu söylemek zorunda. (`pack_panels` de aynı denetimi
    # yapıyor ama oraya sıra gelmiyor.)
    if not ring_is_simple(ring):
        raise ValueError(
            "poligon kendisiyle kesişiyor; alan ve kapsama hesabı anlamsız olur — "
            "çizimi düzeltmek gerek"
        )
    area = polygon_area_m2(ring)
    if area < _MIN_USEFUL_AREA_M2:
        raise ValueError(
            f"alan çok küçük ({area:.1f} m²); en az {_MIN_USEFUL_AREA_M2:.0f} m² gerekli"
        )

    owned_client = client is None
    pvgis = client or PvgisClient()
    try:
        tmy = await pvgis.fetch_tmy(design.latitude, design.longitude)
        horizon: HorizonProfile | None = None
        if fetch_horizon:
            try:
                horizon = await pvgis.fetch_horizon(design.latitude, design.longitude)
            except Exception:  # noqa: BLE001 — ufuk profili opsiyonel süstür
                # Ufuk yalnızca gösterim içindir; alınamazsa analiz durmaz.
                logger.warning("ufuk profili alınamadı, analiz sürüyor", exc_info=True)
    finally:
        if owned_client:
            await pvgis.aclose()

    mounting = build_mounting(design, frame)
    module = ModuleSpec(**design.module_spec) if design.module_spec else ModuleSpec()
    inverter = (
        InverterSpec(**design.inverter_spec) if design.inverter_spec else InverterSpec()
    )
    min_ambient, max_cell = design_temperatures(tmy, mounting.mount)

    layout = pack_panels(
        ring,
        latitude=design.latitude,
        longitude=design.longitude,
        min_ambient_c=min_ambient,
        max_cell_c=max_cell,
        module=module,
        inverter=inverter,
        mounting=mounting,
    )
    simulation = simulate(tmy, layout, losses=losses)
    finance = evaluate(simulation, costs=costs, revenue=revenue, params=params)

    panels_wgs84 = tuple(
        tuple((point.lat, point.lon) for point in frame.ring_to_wgs84(placement.rect))
        for placement in layout.placements
    )
    return ProspectAnalysis(
        tmy=tmy,
        layout=layout,
        simulation=simulation,
        finance=finance,
        horizon=horizon,
        frame=frame,
        panels_wgs84=panels_wgs84,
        warnings=_collect_warnings(layout, simulation, horizon),
    )


def to_report(design: ProspectDesign, analysis: ProspectAnalysis) -> ProspectReport:
    """Analizi dondurulmuş rapor satırına çevirir."""
    layout = analysis.layout
    simulation = analysis.simulation
    finance = analysis.finance
    plan = layout.string_plan

    return ProspectReport(
        design_id=design.id,
        engine_version=ENGINE_VERSION,
        data_provenance=analysis.tmy.provenance[:300],
        module_count=layout.module_count,
        dc_capacity_kwp=layout.dc_capacity_kwp,
        ac_capacity_kw=layout.ac_capacity_kw,
        area_m2=layout.area_m2,
        row_pitch_m=layout.row_pitch_m,
        gcr=layout.gcr,
        orientation=str(layout.orientation),
        modules_per_string=plan.modules_per_string,
        strings=plan.strings,
        inverter_count=plan.inverter_count,
        year_one_kwh=simulation.year_one_kwh,
        specific_yield_kwh_kwp=simulation.specific_yield_kwh_kwp,
        performance_ratio=simulation.performance_ratio,
        poa_kwh_m2=simulation.poa_kwh_m2,
        ghi_kwh_m2=simulation.ghi_kwh_m2,
        lifetime_kwh=simulation.lifetime_kwh,
        p90_year_one_kwh=simulation.percentile_kwh(0.90),
        capex_try=finance.capex_try,
        npv_try=finance.npv_try,
        irr_real=finance.irr_real,
        lcoe_try_kwh=finance.lcoe_try_kwh,
        payback_years=finance.payback_years,
        layout=[[[lat, lon] for lat, lon in rect] for rect in analysis.panels_wgs84],
        monthly_kwh=list(simulation.monthly_kwh),
        waterfall=[asdict(stage) for stage in simulation.waterfall],
        projection=[asdict(year) for year in simulation.projection],
        assumptions=analysis.assumptions,
        warnings=list(analysis.warnings),
    )
