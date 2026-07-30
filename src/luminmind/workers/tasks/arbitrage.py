"""Günlük arbitraj planlama görevi.

GÖP sonuçlarının açıklanmasından sonra (≈14:00 TRT) ertesi günün fiyatlarıyla
her batarya için şarj/deşarj planı üretilir ve PostgreSQL'e yazılır. Aynı
(batarya, gün, piyasa) planı yeniden çalıştırmada silinip yeniden yazılır
(idempotent).

Plan artık **PV tahminiyle birlikte** çözülür: dijital ikizin D+1 üretim
tahmini (`twin_forecast`), tesisin şebeke bağlantı limiti ve varsa sabit alım
tarifesi LP'ye girdi olur. Böylece batarya yalnızca fiyat farkını değil,
bağlantı limiti yüzünden kırpılacak enerjiyi de değerlendirir — bir GES+BESS
tesisinde asıl kazanç çoğu zaman oradadır.
"""

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine

from luminmind.analytics.arbitrage.epias import MARKET_DAM, EpiasClient, PriceProvider, PriceSlot
from luminmind.analytics.arbitrage.mock_prices import MockPriceProvider
from luminmind.analytics.arbitrage.optimizer import BatterySpec, SiteSpec, optimize_day
from luminmind.config import Settings, get_settings
from luminmind.core.influx import InfluxStore
from luminmind.workers.celery_app import app

logger = logging.getLogger(__name__)


class ForecastSource(Protocol):
    """PV üretim tahmininin kaynağı (InfluxStore veya test fake'i)."""

    async def query_forecast_window(
        self, start: datetime, stop: datetime, horizon_days: int
    ) -> dict[str, dict[datetime, float]]: ...


def pv_for_slots(
    forecast: dict[datetime, float], slots: Sequence[PriceSlot], slot_hours: float
) -> list[float]:
    """15 dk'lık tahmin serisini fiyat slotlarının çözünürlüğüne ortalar.

    GÖP slotları saatlik, ikiz serisi 15 dakikalıktır. Enerji korunumu için
    slot içindeki ortalama güç alınır (maksimum değil) — maksimum almak
    kırpılan enerjiyi sistematik olarak abartırdı.
    """
    values: list[float] = []
    window = timedelta(hours=slot_hours)
    if not forecast:
        return [0.0] * len(slots)
    for slot in slots:
        inside = [
            kw for ts, kw in forecast.items() if slot.start <= ts < slot.start + window
        ]
        values.append(sum(inside) / len(inside) if inside else 0.0)
    return values


def _slot_hours(slots: Sequence[PriceSlot]) -> float:
    """Fiyat slotlarının süresi; tek slotta saatlik varsayılır."""
    if len(slots) < 2:
        return 1.0
    deltas = sorted(
        (b.start - a.start).total_seconds() for a, b in zip(slots, slots[1:], strict=False)
    )
    return float(deltas[len(deltas) // 2]) / 3600.0


async def run_arbitrage(
    settings: Settings | None = None,
    day: date | None = None,
    engine: AsyncEngine | None = None,
    prices: PriceProvider | None = None,
    forecast: ForecastSource | None = None,
) -> int:
    """Verilen gün (varsayılan: yarın) için plan üretir; yazılan plan sayısını döndürür."""
    from luminmind.core.db import session_scope
    from luminmind.core.models import ArbitragePlan, ArbitrageSlot, BatterySystem

    settings = settings or get_settings()
    target_day = day or (datetime.now(tz=UTC).date() + timedelta(days=1))
    start = datetime(target_day.year, target_day.month, target_day.day, tzinfo=UTC)
    stop = start + timedelta(days=1)

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

    own_store: InfluxStore | None = None
    if forecast is None and settings.influx_url:
        own_store = InfluxStore(
            url=settings.influx_url, org=settings.influx_org, token=settings.influx_token
        )
        forecast = own_store

    plans = 0
    try:
        price_slots = await prices.fetch_day_ahead_prices(target_day)
        if not price_slots:
            logger.warning("no prices for %s; arbitrage skipped", target_day.isoformat())
            return 0
        slot_hours = _slot_hours(price_slots)

        pv_by_plant: dict[str, dict[datetime, float]] = {}
        if forecast is not None:
            horizon = max(0, (target_day - datetime.now(tz=UTC).date()).days)
            pv_by_plant = await forecast.query_forecast_window(start, stop, horizon)

        async with session_scope(engine) as session:
            batteries = (await session.scalars(select(BatterySystem))).all()
            for battery in batteries:
                spec = BatterySpec(
                    energy_kwh=battery.rated_energy_kwh, power_kw=battery.rated_power_kw
                )
                plant = await battery.awaitable_attrs.plant
                pv_forecast = pv_by_plant.get(plant.vendor_plant_id, {})
                site = SiteSpec(
                    pv_forecast_kw=(
                        pv_for_slots(pv_forecast, price_slots, slot_hours)
                        if pv_forecast
                        else None
                    ),
                    grid_limit_kw=plant.grid_export_limit_kw or plant.ac_capacity_kw,
                    feed_in_try_mwh=(
                        plant.feed_in_tariff_try_kwh * 1000.0
                        if plant.feed_in_tariff_try_kwh
                        else None
                    ),
                )
                result = optimize_day(price_slots, spec, slot_hours=slot_hours, site=site)

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
                    battery_revenue_try=result.battery_revenue_try,
                    pv_revenue_try=result.pv_revenue_try,
                    curtailed_kwh=result.curtailed_kwh,
                    recovered_kwh=result.recovered_kwh,
                )
                plan.slots = [
                    ArbitrageSlot(
                        slot_start=slot.start,
                        action=slot.action,
                        power_kw=slot.power_kw,
                        price_try_mwh=slot.price_try_mwh,
                        pv_to_battery_kw=slot.pv_to_battery_kw,
                        pv_export_kw=slot.pv_export_kw,
                        grid_charge_kw=slot.grid_charge_kw,
                        curtailed_kw=slot.curtailed_kw,
                    )
                    for slot in result.slots
                ]
                session.add(plan)
                plans += 1
                logger.info(
                    "arbitrage plan battery=%s day=%s revenue=%.2f TRY "
                    "(batarya %.2f + PV %.2f) kurtarılan=%.1f kWh kırpılan=%.1f kWh",
                    battery.id,
                    target_day.isoformat(),
                    result.expected_revenue_try,
                    result.battery_revenue_try,
                    result.pv_revenue_try,
                    result.recovered_kwh,
                    result.curtailed_kwh,
                )
    finally:
        if own_client is not None:
            await own_client.aclose()
        if own_store is not None:
            await own_store.__aexit__(None, None, None)
        if own_engine:
            await engine.dispose()
    return plans


@app.task(name="luminmind.plan_arbitrage")  # type: ignore[untyped-decorator]
def plan_arbitrage() -> int:
    return asyncio.run(run_arbitrage())
