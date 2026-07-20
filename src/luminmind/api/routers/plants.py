import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from luminmind.api.deps import get_current_user, get_session, require_admin
from luminmind.api.schemas import (
    BatteryOut,
    InverterOut,
    PlantCreate,
    PlantDetail,
    PlantSummary,
)
from luminmind.core.models import Plant, User

router = APIRouter(prefix="/plants", tags=["plants"])


async def get_plant_or_404(
    plant_id: uuid.UUID, session: Annotated[AsyncSession, Depends(get_session)]
) -> Plant:
    plant = await session.get(Plant, plant_id)
    if plant is None:
        raise HTTPException(status_code=404, detail="plant not found")
    return plant


@router.get("", response_model=list[PlantSummary])
async def list_plants(
    session: Annotated[AsyncSession, Depends(get_session)],
    _user: Annotated[User, Depends(get_current_user)],
) -> list[PlantSummary]:
    plants = (await session.scalars(select(Plant).order_by(Plant.name))).all()
    return [PlantSummary.model_validate(p, from_attributes=True) for p in plants]


@router.get("/{plant_id}", response_model=PlantDetail)
async def plant_detail(
    plant: Annotated[Plant, Depends(get_plant_or_404)],
    _user: Annotated[User, Depends(get_current_user)],
) -> PlantDetail:
    inverters = await plant.awaitable_attrs.inverters
    batteries = await plant.awaitable_attrs.batteries
    return PlantDetail(
        **PlantSummary.model_validate(plant, from_attributes=True).model_dump(),
        latitude=plant.latitude,
        longitude=plant.longitude,
        inverters=[InverterOut.model_validate(i, from_attributes=True) for i in inverters],
        batteries=[BatteryOut.model_validate(b, from_attributes=True) for b in batteries],
    )


@router.post("", response_model=PlantSummary, status_code=201)
async def create_plant(
    payload: PlantCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    admin: Annotated[User, Depends(require_admin)],
) -> PlantSummary:
    existing = (
        await session.scalars(
            select(Plant).where(
                Plant.vendor == payload.vendor,
                Plant.vendor_plant_id == payload.vendor_plant_id,
            )
        )
    ).one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="plant already exists for this vendor id")
    plant = Plant(owner_id=admin.id, **payload.model_dump())
    session.add(plant)
    await session.flush()
    return PlantSummary.model_validate(plant, from_attributes=True)
