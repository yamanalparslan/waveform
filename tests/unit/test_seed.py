import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from luminmind.core.db import session_scope
from luminmind.core.models import Base, Inverter, Plant, User
from luminmind.scripts.seed import seed


@pytest.fixture
async def engine():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


async def test_seed_creates_admin_plant_and_devices(engine):
    async with session_scope(engine) as session:
        await seed(session)

    async with session_scope(engine) as session:
        user = (await session.scalars(select(User))).one()
        assert user.email == "admin@luminmind.local"
        assert user.role == "admin"
        plant = (await session.scalars(select(Plant))).one()
        assert plant.vendor_plant_id == "mock-plant-1"
        inverters = (await session.scalars(select(Inverter))).all()
        assert len(inverters) == 4


async def test_seed_is_idempotent(engine):
    for _ in range(2):
        async with session_scope(engine) as session:
            await seed(session)

    async with session_scope(engine) as session:
        assert len((await session.scalars(select(User))).all()) == 1
        assert len((await session.scalars(select(Plant))).all()) == 1
