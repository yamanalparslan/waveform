"""Kurulum öncesi fizibilite arayüzü (/ui/fizibilite).

`web/routes.py` 2500 satırı aştığı için yeni akış ayrı bir router'a alındı;
oturum ve şablon altyapısı oradan yeniden kullanılıyor (tek yerde durması
gerekiyor, kopyalanırsa güvenlik davranışı ikiye ayrılır).

**Görselleştirme 2D'dir.** Panel yerleşimi uydu görüntüsü üzerine Leaflet
poligonları olarak çizilir — üstten görünüş, 2D dikdörtgenler. 3D sahne, çatı
modeli veya izometrik render yok; gölgeleme matematiği arka planda 3B geometri
kullanır ama arayüze hiç sızmaz.

Poligon çizimi elle yazıldı, `leaflet-draw` eklenmedi: ihtiyaç "tıkla-köşe-ekle,
çift tıkla-kapat" kadar; ek bir CDN bağımlılığı yüklenemediğinde sayfanın
çalışmaya devam etmesi için ayrıca yedek yol yazmak gerekirdi.
"""

import json
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from luminmind.api.deps import get_session
from luminmind.config import Settings, get_settings
from luminmind.core.models.auth import User
from luminmind.core.models.prospect import ProspectDesign, ProspectReport, ProspectStatus
from luminmind.prospect.finance import CostModel, FinanceParams, RevenueModel
from luminmind.prospect.catalog import MODULE_CATALOG, INVERTER_CATALOG
from luminmind.prospect.layout import InverterSpec, ModuleSpec
from luminmind.prospect.service import analyse, to_report
from luminmind.web.routes import (
    get_web_user,
    sidebar_plants_context,
    templates,
)
from luminmind.web.theme import PALETTE

router = APIRouter(prefix="/ui/fizibilite", tags=["web-prospect"])

# Ekipman varsayılanları formda gösterilirken kullanılan seçenekler.
MOUNT_CHOICES = (
    ("rooftop_tilted", "Düz çatı — açılı diziler"),
    ("rooftop", "Eğimli çatı — panel çatıya paralel"),
    ("fixed_ground", "Arazi — sabit açılı"),
)

AZIMUTH_CHOICES = (
    (180.0, "Güney"),
    (135.0, "Güneydoğu"),
    (225.0, "Güneybatı"),
    (90.0, "Doğu"),
    (270.0, "Batı"),
)

MONTH_LABELS = (
    "Oca", "Şub", "Mar", "Nis", "May", "Haz",
    "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara",
)


def _equipment_defaults() -> dict[str, Any]:
    """Formda gösterilecek ekipman varsayılanları — kullanıcı neyle hesaplandığını görsün."""
    module, inverter = ModuleSpec(), InverterSpec()
    return {
        "module_name": module.name,
        "module_w": module.pdc0_w,
        "module_size": f"{module.width_m * 1000:.0f} × {module.height_m * 1000:.0f} mm",
        "inverter_name": inverter.name,
        "inverter_kw": inverter.ac_kw,
    }


def _map_context(settings: Settings) -> dict[str, Any]:
    """Uydu katmanı ayarları — şablonlar bunu doğrudan gömer."""
    return {
        "satellite_url": settings.lm_satellite_tile_url,
        "satellite_attribution": settings.lm_satellite_attribution,
        "satellite_max_zoom": settings.lm_satellite_max_zoom,
        "palette": PALETTE,
    }


async def _load_design(
    session: AsyncSession, design_id: uuid.UUID, user: User
) -> ProspectDesign:
    """Tasarımı yükler ve sahiplik denetler.

    Admin olmayan kullanıcı başkasının tasarımını göremez; 403 yerine 404
    dönülüyor ki kimliğin var olup olmadığı da sızmasın.
    """
    result = await session.execute(
        select(ProspectDesign)
        .where(ProspectDesign.id == design_id)
        .options(selectinload(ProspectDesign.reports))
    )
    design = result.scalar_one_or_none()
    if design is None:
        raise HTTPException(status_code=404, detail="design not found")
    if design.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=404, detail="design not found")
    return design


def _parse_polygon(raw: str) -> list[list[float]]:
    """Formdan gelen `[[lat, lon], …]` JSON'unu doğrular.

    Tarayıcıdan gelen veriye güvenilmez: köşe sayısı, tip ve coğrafi aralık
    burada denetlenir. Geçersiz veri yerleşim algoritmasına ulaşırsa hata
    mesajı kullanıcıya anlamsız gelir.
    """
    try:
        parsed = json.loads(raw or "[]")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"poligon çözümlenemedi: {exc}") from exc
    if not isinstance(parsed, list) or len(parsed) < 3:
        raise HTTPException(status_code=400, detail="poligon en az 3 köşe içermeli")

    points: list[list[float]] = []
    for item in parsed:
        if not isinstance(item, list | tuple) or len(item) != 2:
            raise HTTPException(status_code=400, detail="poligon köşesi [enlem, boylam] olmalı")
        lat, lon = float(item[0]), float(item[1])
        if not -90.0 <= lat <= 90.0 or not -180.0 <= lon <= 180.0:
            raise HTTPException(status_code=400, detail=f"geçersiz koordinat: {lat}, {lon}")
        points.append([lat, lon])
    return points


def _centroid(points: list[list[float]]) -> tuple[float, float]:
    return (
        sum(p[0] for p in points) / len(points),
        sum(p[1] for p in points) / len(points),
    )


@router.get("", response_class=HTMLResponse)
async def prospect_list(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_web_user)],
) -> HTMLResponse:
    """Fizibilite çalışmalarının listesi."""
    sidebar_plants, _ = await sidebar_plants_context(session)
    query = select(ProspectDesign).options(selectinload(ProspectDesign.reports))
    if user.role != "admin":
        query = query.where(ProspectDesign.owner_id == user.id)
    result = await session.execute(query.order_by(ProspectDesign.updated_at.desc()))
    designs = list(result.scalars())
    return templates.TemplateResponse(
        request,
        "prospect_list.html",
        {
            "user": user,
            "section": "prospect",
            "plant": None,
            "sidebar_plants": sidebar_plants,
            "designs": designs,
            "page_title": "Fizibilite",
        },
    )


@router.post("/{design_id}/delete")
async def prospect_delete(
    design_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_web_user)],
) -> Response:
    """Fizibilite silme."""
    design = await _load_design(session, design_id, user)
    await session.delete(design)
    await session.commit()
    return RedirectResponse(url="/ui/fizibilite", status_code=303)


@router.get("/yeni", response_class=HTMLResponse)
async def prospect_new(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_web_user)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    """Çatı/arazi poligonunun çizildiği sayfa."""
    sidebar_plants, _ = await sidebar_plants_context(session)
    return templates.TemplateResponse(
        request,
        "prospect_new.html",
        {
            "user": user,
            "section": "prospect",
            "plant": None,
            "sidebar_plants": sidebar_plants,
            "mount_choices": MOUNT_CHOICES,
            "azimuth_choices": AZIMUTH_CHOICES,
            "equipment": _equipment_defaults(),
            "module_catalog": MODULE_CATALOG,
            "inverter_catalog": INVERTER_CATALOG,
            "cost": CostModel(),
            "revenue": RevenueModel(),
            "params": FinanceParams(),
            "page_title": "Yeni fizibilite",
            **_map_context(settings),
        },
    )


@router.post("")
async def prospect_create(
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_web_user)],
    name: Annotated[str, Form()],
    polygon: Annotated[str, Form()],
    module_id: Annotated[str, Form()] = "generic_580_topcon",
    inverter_id: Annotated[str, Form()] = "generic_100kw",
    mount_type: Annotated[str, Form()] = "rooftop_tilted",
    tilt_deg: Annotated[float, Form()] = 15.0,
    azimuth_deg: Annotated[float, Form()] = 180.0,
    setback_m: Annotated[float, Form()] = 0.6,
    customer: Annotated[str, Form()] = "",
    obstacles: Annotated[str, Form()] = "[]",
    capex_per_wp_try: Annotated[float, Form()] = 18.0,
    retail_tariff_try_kwh: Annotated[float, Form()] = 3.40,
    export_tariff_try_kwh: Annotated[float, Form()] = 1.60,
    self_consumption_share: Annotated[float, Form()] = 0.75,
    discount_rate_real: Annotated[float, Form()] = 0.12,
) -> Response:
    """Tasarımı kaydeder, analizi koşturur ve rapora yönlendirir.

    Analiz senkron çalışıyor: PVGIS çağrısı ~3 s, yerleşim ~1 s, simülasyon
    ~4 s. Celery'ye taşınması gereken bir süre değil ama kullanıcı bekliyor;
    sayfa "hesaplanıyor" göstergesiyle gönderiliyor (bkz. prospect_new.html).
    """
    from dataclasses import asdict
    
    points = _parse_polygon(polygon)
    latitude, longitude = _centroid(points)
    
    module_spec_dict = asdict(MODULE_CATALOG.get(module_id, MODULE_CATALOG["generic_580_topcon"])["spec"])
    inverter_spec_dict = asdict(INVERTER_CATALOG.get(inverter_id, INVERTER_CATALOG["generic_100kw"])["spec"])

    design = ProspectDesign(
        owner_id=user.id,
        name=name.strip() or "Adsız fizibilite",
        customer=customer.strip() or None,
        status=ProspectStatus.DRAFT,
        latitude=latitude,
        longitude=longitude,
        polygon=points,
        obstacles=json.loads(obstacles or "[]"),
        mount_type=mount_type,
        tilt_deg=tilt_deg,
        azimuth_deg=azimuth_deg,
        setback_m=setback_m,
        module_spec=module_spec_dict,
        inverter_spec=inverter_spec_dict,
    )
    session.add(design)
    await session.flush()

    try:
        analysis = await analyse(
            design,
            costs=CostModel(capex_per_kwp_try=capex_per_wp_try * 1000.0),
            revenue=RevenueModel(
                retail_tariff_try_kwh=retail_tariff_try_kwh,
                export_tariff_try_kwh=export_tariff_try_kwh,
                self_consumption_share=self_consumption_share,
            ),
            params=FinanceParams(discount_rate_real=discount_rate_real),
        )
    except ValueError as exc:
        # Geometri/ekipman uyumsuzluğu kullanıcı hatasıdır; tasarımı kaydetmiş
        # olmak iyi (kullanıcı düzeltip tekrar deneyebilir) ama rapor yazılamaz.
        await session.commit()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    session.add(to_report(design, analysis))
    design.status = ProspectStatus.ANALYSED
    await session.commit()
    return RedirectResponse(url=f"/ui/fizibilite/{design.id}", status_code=303)


@router.get("/{design_id}", response_class=HTMLResponse)
async def prospect_report(
    request: Request,
    design_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_web_user)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    """Fizibilite raporu — 2D panel yerleşimi, üretim, kayıplar ve finans."""
    sidebar_plants, _ = await sidebar_plants_context(session)
    design = await _load_design(session, design_id, user)
    report: ProspectReport | None = design.latest_report
    if report is None:
        raise HTTPException(status_code=404, detail="report not found")

    monthly_max = max(report.monthly_kwh) if report.monthly_kwh else 1.0
    monthly = [
        {
            "label": MONTH_LABELS[index],
            "kwh": value,
            "share": (value / monthly_max) if monthly_max > 0 else 0.0,
        }
        for index, value in enumerate(report.monthly_kwh)
    ]
    return templates.TemplateResponse(
        request,
        "prospect_report.html",
        {
            "user": user,
            "section": "prospect",
            "plant": None,
            "sidebar_plants": sidebar_plants,
            "design": design,
            "report": report,
            "monthly": monthly,
            "panels_json": json.dumps(report.layout),
            "polygon_json": json.dumps(design.polygon),
            "obstacles_json": json.dumps(design.obstacles or []),
            "page_title": design.name,
            **_map_context(settings),
        },
    )
