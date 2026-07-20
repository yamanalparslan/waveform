import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from luminmind.api.deps import get_current_user, get_session
from luminmind.api.routers.plants import get_plant_or_404
from luminmind.api.schemas import AnomalyOut, AnomalyPatch
from luminmind.core.models import AnomalyEvent, Plant, User

router = APIRouter(tags=["anomalies"])


@router.get("/plants/{plant_id}/anomalies", response_model=list[AnomalyOut])
async def list_anomalies(
    plant: Annotated[Plant, Depends(get_plant_or_404)],
    session: Annotated[AsyncSession, Depends(get_session)],
    _user: Annotated[User, Depends(get_current_user)],
    status: Annotated[str | None, Query()] = None,
    kind: Annotated[str | None, Query()] = None,
) -> list[AnomalyOut]:
    query = (
        select(AnomalyEvent)
        .where(AnomalyEvent.plant_id == plant.id)
        .order_by(AnomalyEvent.started_at.desc())
    )
    if status is not None:
        query = query.where(AnomalyEvent.status == status)
    if kind is not None:
        query = query.where(AnomalyEvent.kind == kind)
    events = (await session.scalars(query)).all()
    return [AnomalyOut.model_validate(e, from_attributes=True) for e in events]


@router.patch("/anomalies/{anomaly_id}", response_model=AnomalyOut)
async def update_anomaly(
    anomaly_id: uuid.UUID,
    payload: AnomalyPatch,
    session: Annotated[AsyncSession, Depends(get_session)],
    _user: Annotated[User, Depends(get_current_user)],
) -> AnomalyOut:
    event = await session.get(AnomalyEvent, anomaly_id)
    if event is None:
        raise HTTPException(status_code=404, detail="anomaly not found")
    event.status = payload.status
    await session.flush()
    return AnomalyOut.model_validate(event, from_attributes=True)
