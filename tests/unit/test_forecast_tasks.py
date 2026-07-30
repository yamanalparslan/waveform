"""Doğruluk skorlama ve kalibrasyon görevlerinin uçtan uca davranışı."""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from luminmind.config import Settings
from luminmind.core.aggregate import RawSample
from luminmind.core.db import session_scope
from luminmind.core.models import AnomalyEvent, Base, Plant, TwinCalibration
from luminmind.scripts.seed import seed
from luminmind.workers.tasks.accuracy import run_accuracy
from luminmind.workers.tasks.calibration import run_calibration

DAY = date(2026, 7, 19)
PLANT = "mock-plant-1"
SETTINGS = Settings(lm_use_mock_vendors=True, lm_twin_calibration_window_days=14)


@pytest.fixture
async def engine():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with session_scope(engine) as session:
        await seed(session)
    yield engine
    await engine.dispose()


def daytime_slots(day: date, count: int = 48) -> list[datetime]:
    start = datetime(day.year, day.month, day.day, 5, 0, tzinfo=UTC)
    return [start + timedelta(minutes=15 * i) for i in range(count)]


class FakeSeries:
    """Influx yerine geçen kaynak/hedef; gerçek = beklenen × loss_factor."""

    def __init__(
        self,
        days: int = 1,
        loss_factor: float = 0.90,
        expected_kw: float = 600.0,
        previous_day_factor: float | None = None,
    ):
        self.days = days
        self.loss_factor = loss_factor
        self.expected_kw = expected_kw
        # Persistence referansının kusursuz olmadığı senaryoları kurmak için
        self.previous_day_factor = previous_day_factor
        self.written: list = []

    def _slots(self):
        for offset in range(self.days):
            yield from daytime_slots(DAY - timedelta(days=offset))

    def _actual_kw(self, ts: datetime) -> float:
        if self.previous_day_factor is not None and ts.date() < DAY:
            return self.expected_kw * self.previous_day_factor
        return self.expected_kw * self.loss_factor

    async def query_raw_window(self, start, stop):
        return [
            RawSample(
                ts=ts,
                plant_id=PLANT,
                inverter_id="inv-1",
                fields={"ac_power_kw": self._actual_kw(ts)},
            )
            for ts in self._slots()
            if start <= ts < stop
        ]

    async def query_twin_window(self, start, stop):
        series = {ts: self.expected_kw for ts in self._slots() if start <= ts < stop}
        return {PLANT: series} if series else {}

    async def query_twin_band_window(self, start, stop):
        return {}

    async def write_accuracy(self, scores):
        self.written.extend(scores)


async def test_accuracy_task_scores_and_writes(engine):
    source = FakeSeries(days=2, loss_factor=0.90)
    scores = await run_accuracy(
        SETTINGS, day=DAY, engine=engine, source=source, sink=source
    )
    assert len(scores) == 1
    score = scores[0]
    assert score.plant_id == PLANT
    assert score.day == DAY
    # 600 kW beklenirken 540 kW üretildi → 60 kW aşırı tahmin; kapasite 800 kW AC
    assert score.mbe_kw == pytest.approx(60.0)
    assert score.nmbe_pct == pytest.approx(7.5)
    assert score.is_biased
    assert source.written == scores


async def test_accuracy_task_beats_persistence_when_yesterday_differed(engine):
    """Dün bugünden çok farklıysa fizik modeli naif referansı yenmeli."""
    source = FakeSeries(days=2, loss_factor=0.98, previous_day_factor=0.40)
    [score] = await run_accuracy(SETTINGS, day=DAY, engine=engine, source=source, sink=source)
    # Model 12 kW şaşıyor, persistence 348 kW → skill 1'e yakın
    assert score.skill_vs_reference is not None and score.skill_vs_reference > 0.9


async def test_accuracy_task_reports_negative_skill_when_model_is_worse(engine):
    source = FakeSeries(days=2, loss_factor=0.40, previous_day_factor=0.42)
    [score] = await run_accuracy(SETTINGS, day=DAY, engine=engine, source=source, sink=source)
    # Persistence 12 kW şaşıyor, model 360 kW → skill belirgin negatif
    assert score.skill_vs_reference is not None and score.skill_vs_reference < -1.0


async def test_skill_is_undefined_when_reference_is_perfect(engine):
    """Payda sıfırken skor uydurulmaz; None döner."""
    source = FakeSeries(days=2, loss_factor=0.90)
    [score] = await run_accuracy(SETTINGS, day=DAY, engine=engine, source=source, sink=source)
    assert score.skill_vs_reference is None


async def test_accuracy_task_skips_plants_without_capacity(engine):
    async with session_scope(engine) as session:
        plant = (await session.scalars(select(Plant))).one()
        plant.ac_capacity_kw = None
        plant.dc_capacity_kwp = None

    source = FakeSeries()
    assert await run_accuracy(SETTINGS, day=DAY, engine=engine, source=source, sink=source) == []


async def test_calibration_task_learns_and_persists(engine):
    source = FakeSeries(days=10, loss_factor=0.90)
    states = await run_calibration(SETTINGS, until=DAY + timedelta(days=1), engine=engine,
                                   source=source)
    assert len(states) == 1
    assert 0.90 < states[0].scale < 1.0

    async with session_scope(engine) as session:
        row = (await session.scalars(select(TwinCalibration))).one()
        assert row.scale == pytest.approx(states[0].scale)
        assert row.sample_count > 0
        assert "mape_after_pct" in row.quality


async def test_calibration_excludes_open_anomaly_windows(engine):
    """Açık arıza penceresi fit'e girerse model arızayı 'normal' diye öğrenir."""
    async with session_scope(engine) as session:
        plant = (await session.scalars(select(Plant))).one()
        session.add(
            AnomalyEvent(
                plant_id=plant.id,
                kind="soiling",
                severity="critical",
                deviation_pct=-25.0,
                started_at=datetime(2026, 7, 1, tzinfo=UTC),
                status="open",
                evidence={},
            )
        )

    source = FakeSeries(days=10, loss_factor=0.75)
    states = await run_calibration(
        SETTINGS, until=DAY + timedelta(days=1), engine=engine, source=source
    )
    # Tüm pencere açık anomaliye denk geliyor → kalibre edilecek veri kalmaz
    assert states == []
    async with session_scope(engine) as session:
        assert (await session.scalars(select(TwinCalibration))).all() == []


async def test_calibration_is_idempotent_when_model_is_already_right(engine):
    source = FakeSeries(days=10, loss_factor=1.0)
    states = await run_calibration(
        SETTINGS, until=DAY + timedelta(days=1), engine=engine, source=source
    )
    assert len(states) == 1
    assert states[0].scale == pytest.approx(1.0, abs=1e-3)


async def test_calibration_can_be_disabled(engine):
    disabled = Settings(lm_use_mock_vendors=True, lm_twin_calibration_enabled=False)
    source = FakeSeries(days=10, loss_factor=0.9)
    assert await run_calibration(disabled, until=DAY, engine=engine, source=source) == []
