"""Open-Meteo hava durumu / ışınım istemcisi (PLAN.md kararı: ücretsiz, anahtarsız).

15 dakikalık çözünürlükte GHI (shortwave_radiation), DNI, DHI, sıcaklık ve rüzgar
verisi çeker. `timezone=UTC` istendiği için dönen zaman damgaları UTC kabul edilir.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Protocol

import httpx

from luminmind.adapters.retry import request_with_retry

OPEN_METEO_BASE_URL = "https://api.open-meteo.com"

_MINUTELY_FIELDS = (
    "shortwave_radiation",
    "direct_normal_irradiance",
    "diffuse_radiation",
    "temperature_2m",
    "wind_speed_10m",
)


@dataclass(frozen=True)
class WeatherSample:
    ts: datetime
    ghi_wm2: float
    dni_wm2: float
    dhi_wm2: float
    temp_c: float
    wind_ms: float


class WeatherProvider(Protocol):
    """Twin görevinin hava verisi kaynağı (gerçek istemci veya test fake'i)."""

    async def fetch_day_15m(
        self, latitude: float, longitude: float, day: date
    ) -> list[WeatherSample]: ...


def _as_float(value: Any) -> float:
    return 0.0 if value is None else float(value)


def parse_minutely_15(payload: dict[str, Any]) -> list[WeatherSample]:
    block = payload.get("minutely_15") or {}
    times: list[str] = block.get("time") or []
    samples: list[WeatherSample] = []
    for index, time_str in enumerate(times):
        # timezone=UTC istendiğinden damgalar tz'siz ISO gelir; UTC olarak işaretle
        ts = datetime.fromisoformat(time_str).replace(tzinfo=UTC)
        values = {name: _as_float((block.get(name) or [None] * len(times))[index])
                  for name in _MINUTELY_FIELDS}
        samples.append(
            WeatherSample(
                ts=ts,
                ghi_wm2=values["shortwave_radiation"],
                dni_wm2=values["direct_normal_irradiance"],
                dhi_wm2=values["diffuse_radiation"],
                temp_c=values["temperature_2m"],
                wind_ms=values["wind_speed_10m"],
            )
        )
    return samples


class OpenMeteoClient:
    def __init__(self, base_url: str = OPEN_METEO_BASE_URL, timeout_s: float = 30.0) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout_s)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch_day_15m(
        self, latitude: float, longitude: float, day: date
    ) -> list[WeatherSample]:
        response = await request_with_retry(
            self._client,
            "GET",
            "/v1/forecast",
            params={
                "latitude": latitude,
                "longitude": longitude,
                "minutely_15": ",".join(_MINUTELY_FIELDS),
                "start_date": day.isoformat(),
                "end_date": day.isoformat(),
                "timezone": "UTC",
                "wind_speed_unit": "ms",
            },
        )
        payload: dict[str, Any] = response.json()
        return parse_minutely_15(payload)
