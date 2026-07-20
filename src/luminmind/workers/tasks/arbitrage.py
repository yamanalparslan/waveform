"""Günlük arbitraj planlama görevi.

GÖP sonuçlarının açıklanmasından sonra (≈14:00 TRT) ertesi günün fiyatlarıyla
her batarya için şarj/deşarj planı üretilir ve PostgreSQL'e yazılır. Aynı
(batarya, gün, piyasa) planı yeniden çalıştırmada silinip yeniden yazılır
(idempotent).
"""

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine

from luminmind.analytics.arbitrage.epias import MARKET_DAM, EpiasClient, PriceProvider
from luminmind.analytics.arbitrage.mock_prices import MockPriceProvider
from luminmind.analytics.arbitrage.optimizer import BatterySpec, optimize_day
from luminmind.config import Settings, get_settings
from luminmind.workers.celery_app import app

logger = logging.getLogger(__name__)


async def run_arbitrage(
    settings: Settings | None = None,
    day: date | None = None,
    engine: AsyncEngine | None = None,
    prices: PriceProvider | None = None,
) -> int:
    """Verilen gün (varsayılan: yarın) için plan üretir; yazılan plan sayısını döndürür."""
    from luminmind.core.db import session_scope
    from luminmind.core.models import ArbitragePlan, ArbitrageSlot, BatterySystem

    settings = settings or get_settings()
    target_day = day or (datetime.now(tz=UTC).date() + timedelta(days=1))

    own_engine = engine is None
    if engine is None:
        from luminmind.core.db import create_engine

        engine = create_engine(settings.postgres_dsn)

    own_client: EpiasClient | None = None
    if prices is None:
        if settings.lm_use_mock_prices or not settings.epias_base_url:
            prices = MockPriceProvider()
        else:
            own_client = EpiasClient(base_url=settings.epias_base_url)
            prices = own_client

    plans = 0
    try:
        price_slots = await prices.fetch_day_ahead_prices(target_day)
        if not price_slots:
            logger.warning("no prices for %s; arbitrage skipped", target_day.isoformat())
            return 0

        async with session_scope(engine) as session:
            batteries = (await session.scalars(select(BatterySystem))).all()
            for battery in batteries:
                spec = BatterySpec(
                    energy_kwh=battery.rated_energy_kwh, power_kw=battery.rated_power_kw
                )
                result = optimize_day(price_slots, spec)

                existing_ids = (
                    await session.scalars(
                        select(ArbitragePlan.id).where(
                            ArbitragePlan.battery_id == battery.id,
                            ArbitragePlan.plan_date == target_day,
                            ArbitragePlan.market == MARKET_DAM,
                        )
                    )
                ).all()
                if existing_ids:
                    await session.execute(
                        delete(ArbitrageSlot).where(ArbitrageSlot.plan_id.in_(existing_ids))
                    )
                    await session.execute(
                        delete(ArbitragePlan).where(ArbitragePlan.id.in_(existing_ids))
                    )

                plan = ArbitragePlan(
                    battery_id=battery.id,
                    plan_date=target_day,
                    market=MARKET_DAM,
                    expected_revenue_try=result.expected_revenue_try,
                )
                plan.slots = [
                    ArbitrageSlot(
                        slot_start=slot.start,
                        action=slot.action,
                        power_kw=slot.power_kw,
                        price_try_mwh=slot.price_try_mwh,
                    )
                    for slot in result.slots
                ]
                session.add(plan)
                plans += 1
                logger.info(
                    "arbitrage plan battery=%s day=%s revenue=%.2f TRY",
                    battery.id,
                    target_day.isoformat(),
                    result.expected_revenue_try,
                )
    finally:
        if own_client is not None:
            await own_client.aclose()
        if own_engine:
            await engine.dispose()
    return plans


@app.task(name="luminmind.plan_arbitrage")  # type: ignore[untyped-decorator]
def plan_arbitrage() -> int:
    return asyncio.run(run_arbitrage())
