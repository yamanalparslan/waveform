"""Temsili GÖP fiyat eğrisi (EPİAŞ erişimi gelene kadar — PLAN.md kararı).

Tipik Türkiye günü: gece düşük, sabah rampası, öğlen güneş çukuru, akşam tepesi.
Deterministiktir; arbitraj optimizasyonunun testleri ve dev ortamı bunu kullanır.
Saatler TRT (UTC+3) baz alınarak UTC damgalarıyla üretilir.
"""

from datetime import UTC, date, datetime, timedelta

from luminmind.analytics.arbitrage.epias import PriceSlot

# TRT saati → TRY/MWh (temsili 2026 seviyeleri)
_HOURLY_PROFILE_TRT: dict[int, float] = {
    0: 1250, 1: 1200, 2: 1150, 3: 1100, 4: 1120, 5: 1200,
    6: 1500, 7: 1900, 8: 2200, 9: 2100, 10: 1600, 11: 1250,
    12: 1050, 13: 1000, 14: 1050, 15: 1200, 16: 1500, 17: 1900,
    18: 2300, 19: 2650, 20: 2700, 21: 2500, 22: 1900, 23: 1500,
}
_TRT_OFFSET = timedelta(hours=3)


class MockPriceProvider:
    async def fetch_day_ahead_prices(self, day: date) -> list[PriceSlot]:
        midnight_trt = datetime(day.year, day.month, day.day, tzinfo=UTC) - _TRT_OFFSET
        return [
            PriceSlot(start=midnight_trt + timedelta(hours=hour), price_try_mwh=price)
            for hour, price in sorted(_HOURLY_PROFILE_TRT.items())
        ]
