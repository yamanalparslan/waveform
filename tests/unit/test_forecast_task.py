"""Gün-öncesi üretim tahmini görevi testleri."""

from datetime import UTC, datetime, timedelta

from luminmind.config import Settings
from luminmind.twin.weather import WeatherSample
from luminmind.workers.tasks.forecast import run_forecast


class FakeWeather:
    def __init__(self) -> None:
        self.days: list = []

    async def fetch_day_15m(self, latitude, longitude, day):
        self.days.append(day)
        base = datetime(day.year, day.month, day.day, tzinfo=UTC)
        return [
            WeatherSample(
                ts=base + timedelta(hours=10, minutes=15 * i),
                ghi_wm2=850.0, dni_wm2=880.0, dhi_wm2=110.0,
                temp_c=30.0, wind_ms=3.0,
            )
            for i in range(4)
        ]


class CapturingSink:
    def __init__(self) -> None:
        self.points: list = []

    async def write_twin(self, points) -> None:
        self.points.extend(points)


async def test_forecast_computes_future_days_only():
    settings = Settings(lm_use_mock_vendors=True)
    weather = FakeWeather()
    sink = CapturingSink()

    total = await run_forecast(settings, days_ahead=2, sink=sink, weather=weather)

    today = datetime.now(tz=UTC).date()
    # yalnızca gelecek günler: yarın ve öbür gün
    assert weather.days == [today + timedelta(days=1), today + timedelta(days=2)]
    # her gün 4 slot → 2 gün = 8 nokta
    assert total == 8 == len(sink.points)
    # tüm damgalar gelecekte
    assert all(p.ts.date() > today for p in sink.points)


async def test_forecast_single_day_ahead():
    settings = Settings(lm_use_mock_vendors=True)
    weather = FakeWeather()
    sink = CapturingSink()

    total = await run_forecast(settings, days_ahead=1, sink=sink, weather=weather)

    assert len(weather.days) == 1
    assert total == 4
