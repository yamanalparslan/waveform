"""Piyasa fiyatları ve arbitraj planı endpoint'leri."""

from datetime import date
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from luminmind.analytics.arbitrage.epias import EpiasClient
from luminmind.analytics.arbitrage.mock_prices import MockPriceProvider
from luminmind.api.deps import get_current_user, get_session, get_tz, in_tz
from luminmind.api.routers.plants import get_plant_or_404
from luminmind.api.schemas import ArbitragePlanOut, ArbitrageSlotOut, PriceOut
from luminmind.config import Settings
from luminmind.core.models import ArbitragePlan, BatterySystem, Plant, User

router = APIRouter(tags=["market"])


@router.get("/prices", response_model=list[PriceOut])
async def prices(
    request: Request,
    _user: Annotated[User, Depends(get_current_user)],
    zone: Annotated[ZoneInfo | None, Depends(get_tz)],
    day: Annotated[date, Query(alias="date")],
    market: Annotated[Literal["DAM"], Query()] = "DAM",
) -> list[PriceOut]:
    settings: Settings = request.app.state.settings
    if settings.lm_use_mock_prices or not settings.epias_base_url:
        slots = await MockPriceProvider().fetch_day_ahead_prices(day)
    else:
        client = EpiasClient(base_url=settings.epias_base_url)
        try:
            slots = await client.fetch_day_ahead_prices(day)
        finally:
            await client.aclose()
    return [PriceOut(ts=in_tz(s.start, zone), price_try_mwh=s.price_try_mwh) for s in slots]


@router.get("/plants/{plant_id}/arbitrage/plan", response_model=list[ArbitragePlanOut])
async def arbitrage_plan(
    plant: Annotated[Plant, Depends(get_plant_or_404)],
    session: Annotated[AsyncSession, Depends(get_session)],
    _user: Annotated[User, Depends(get_current_user)],
    zone: Annotated[ZoneInfo | None, Depends(get_tz)],
    day: Annotated[date, Query(alias="date")],
) -> list[ArbitragePlanOut]:
    battery_ids = (
        await session.scalars(
            select(BatterySystem.id).where(BatterySystem.plant_id == plant.id)
        )
    ).all()
    plans = (
        await session.scalars(
            select(ArbitragePlan).where(
                ArbitragePlan.battery_id.in_(battery_ids),
                ArbitragePlan.plan_date == day,
            )
        )
    ).all()
    if not plans:
        raise HTTPException(status_code=404, detail="no arbitrage plan for this date")
    out: list[ArbitragePlanOut] = []
    for plan in plans:
        slots = await plan.awaitable_attrs.slots
        out.append(
            ArbitragePlanOut(
                id=plan.id,
                battery_id=plan.battery_id,
                plan_date=plan.plan_date,
                market=plan.market,
                expected_revenue_try=plan.expected_revenue_try,
                slots=[
                    ArbitrageSlotOut(
                        slot_start=in_tz(s.slot_start, zone),
                        action=s.action,
                        power_kw=s.power_kw,
                        price_try_mwh=s.price_try_mwh,
                    )
                    for s in slots
                ],
            )
        )
    return out


@router.post("/plants/{plant_id}/arbitrage/replan", status_code=202)
async def replan(
    plant: Annotated[Plant, Depends(get_plant_or_404)],
    _user: Annotated[User, Depends(get_current_user)],
) -> dict[str, str]:
    from luminmind.workers.celery_app import app as celery_app

    try:
        result = celery_app.send_task("luminmind.plan_arbitrage")
    except Exception as exc:  # broker erişilemez (ör. Redis kapalı)
        raise HTTPException(status_code=503, detail="task queue unavailable") from exc
    return {"task_id": result.id, "status": "queued"}
