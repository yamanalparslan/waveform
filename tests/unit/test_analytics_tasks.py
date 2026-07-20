from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from luminmind.config import Settings
from luminmind.core.aggregate import RawSample
from luminmind.core.db import session_scope
from luminmind.core.models import AnomalyEvent, ArbitragePlan, ArbitrageSlot, Base
from luminmind.scripts.seed import seed
from luminmind.workers.tasks.arbitrage import run_arbitrage
from luminmind.workers.tasks.comparison import run_comparison

DAY = date(2026, 7, 19)
SETTINGS = Settings(lm_use_mock_vendors=True, lm_use_mock_prices=True)


@pytest.fixture
async def engine():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with session_scope(engine) as session:
        await seed(session)
    yield engine
    await engine.dispose()


class FakeSource:
    """Influx yerine geçen kaynak: mock tesise üniform %12 kayıp enjekte eder."""

    def __init__(self, loss_factor: float = 0.88):
        self.loss_factor = loss_factor

    def _times(self):
        start = datetime(DAY.year, DAY.month, DAY.day, 5, 0, tzinfo=UTC)
        return [start + timedelta(minutes=15 * i) for i in range(48)]  # 05:00–17:00

    async def query_raw_window(self, start, stop):
        return [
            RawSample(
                ts=ts,
                plant_id="mock-plant-1",
                inverter_id=f"inv-{d}",
                fields={"ac_power_kw": 200.0 * self.loss_factor},
            )
            for ts in self._times()
            for d in range(4)
        ]

    async def query_twin_window(self, start, stop):
        return {"mock-plant-1": {ts: 800.0 for ts in self._times()}}


async def test_comparison_task_creates_soiling_event(engine):
    count = await run_comparison(SETTINGS, day=DAY, engine=engine, source=FakeSource())
    assert count == 1

    async with session_scope(engine) as session:
        event = (await session.scalars(select(AnomalyEvent))).one()
        assert event.kind == "soiling"
        assert event.status == "open"
        assert event.deviation_pct == pytest.approx(-12.0, abs=0.1)


async def test_comparison_task_dedupes_open_events(engine):
    for _ in range(2):
        await run_comparison(SETTINGS, day=DAY, engine=engine, source=FakeSource())
    async with session_scope(engine) as session:
        events = (await session.scalars(select(AnomalyEvent))).all()
        assert len(events) == 1  # aynı tür ikinci kez açılmaz, güncellenir


async def test_comparison_task_resolves_when_healthy(engine):
    await run_comparison(SETTINGS, day=DAY, engine=engine, source=FakeSource())
    await run_comparison(SETTINGS, day=DAY, engine=engine, source=FakeSource(loss_factor=0.99))
    async with session_scope(engine) as session:
        event = (await session.scalars(select(AnomalyEvent))).one()
        assert event.status == "resolved"
        assert event.ended_at is not None


async def test_arbitrage_task_writes_plan_with_slots(engine):
    plans = await run_arbitrage(SETTINGS, day=DAY, engine=engine)
    assert plans == 1  # seed'deki tek batarya

    async with session_scope(engine) as session:
        plan = (await session.scalars(select(ArbitragePlan))).one()
        assert plan.market == "DAM"
        assert plan.plan_date == DAY
        assert plan.expected_revenue_try > 0
        slots = (await session.scalars(select(ArbitrageSlot))).all()
        assert len(slots) == 24


async def test_arbitrage_task_is_idempotent(engine):
    for _ in range(2):
        await run_arbitrage(SETTINGS, day=DAY, engine=engine)
    async with session_scope(engine) as session:
        assert len((await session.scalars(select(ArbitragePlan))).all()) == 1
        assert len((await session.scalars(select(ArbitrageSlot))).all()) == 24
