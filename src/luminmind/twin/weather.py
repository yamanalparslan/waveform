"""Open-Meteo hava durumu / ışınım istemcisi (PLAN.md kararı: ücretsiz, anahtarsız).

15 dakikalık çözünürlükte GHI (shortwave_radiation), DNI, DHI, sıcaklık, rüzgar,
bağıl nem, yağış ve yüzey basıncı çeker. `timezone=UTC` istendiği için dönen
zaman damgaları UTC kabul edilir.

İki nokta kritik ve kolayca gözden kaçar:

**1. Eksik veri ≠ sıfır.** Önceki sürüm `None` değerini 0.0'a düşürüyordu; bu,
Open-Meteo bir alanı boş döndürdüğünde dijital ikizin "gece" sanmasına ve
gerçek üretimin sınırsız pozitif sapma üretmesine yol açıyordu. Artık `NaN`
döner ve modelde açıkça elenir.

**2. Işınım değerleri aralık ortalamasıdır.** Open-Meteo radyasyon alanları
"preceding 15 minutes mean" semantiğindedir: `t` damgalı değer `[t-15dk, t)`
aralığının ortalamasıdır. Anlık kabul edilirse sabah/akşam kenarlarında
sistematik ~7,5 dakikalık faz kayması oluşur. Damga sözleşmesi
`IrradianceStamp` ile taşınır; güneş geometrisi aralık orta noktasında
hesaplanır (bkz. `twin/pipeline.py`).

Ensemble: `models` parametresiyle birden çok sayısal hava tahmini modeli aynı
istekte çekilebilir. Open-Meteo bu durumda alan adlarını `_<model>` son ekiyle
döndürür; `parse_minutely_15` her iki biçimi de anlar.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

import httpx

from luminmind.adapters.retry import request_with_retry

OPEN_METEO_BASE_URL = "https://api.open-meteo.com"

# Ensemble için varsayılan model seti: birbirinden bağımsız merkezler (DWD, NOAA,
# ECMWF, Météo-France) — yayılımları gerçek tahmin belirsizliğinin vekilidir.
DEFAULT_ENSEMBLE_MODELS: tuple[str, ...] = (
    "icon_seamless",
    "gfs_seamless",
    "ecmwf_ifs025",
    "meteofrance_seamless",
)

_MINUTELY_FIELDS = (
    "shortwave_radiation",
    "direct_normal_irradiance",
    "diffuse_radiation",
    "temperature_2m",
    "wind_speed_10m",
    "relative_humidity_2m",
    "precipitation",
    "surface_pressure",
)

# Bu alanlar aralık ortalamasıdır (Open-Meteo dokümantasyonu); geri kalanı anlık.
_INTERVAL_MEAN_FIELDS = frozenset(
    {"shortwave_radiation", "direct_normal_irradiance", "diffuse_radiation", "precipitation"}
)


class IrradianceStamp(StrEnum):
    """Işınım değerinin zaman damgasıyla ilişkisi."""

    INTERVAL_END = "interval_end"  # Open-Meteo: değer [t-Δ, t) ortalaması
    INTERVAL_START = "interval_start"  # değer [t, t+Δ) ortalaması
    INSTANT = "instant"  # değer t anındaki anlık ışınım


@dataclass(frozen=True)
class WeatherSample:
    """Tek zaman damgası için hava durumu gözlemi/tahmini.

    Eksik değerler `NaN`'dır — sıfır değil. `math.isnan` ile kontrol edilir.
    """

    ts: datetime
    ghi_wm2: float
    dni_wm2: float
    dhi_wm2: float
    temp_c: float
    wind_ms: float
    relative_humidity_pct: float = float("nan")
    precip_mm: float = float("nan")
    pressure_hpa: float = float("nan")

    @property
    def has_irradiance(self) -> bool:
        return not math.isnan(self.ghi_wm2)


class WeatherProvider(Protocol):
    """Twin görevinin hava verisi kaynağı (gerçek istemci veya test fake'i)."""

    async def fetch_day_15m(
        self, latitude: float, longitude: float, day: date
    ) -> list[WeatherSample]: ...


class EnsembleWeatherProvider(Protocol):
    """Çok modelli (belirsizlik taşıyan) hava verisi kaynağı."""

    async def fetch_range_15m(
        self,
        latitude: float,
        longitude: float,
        start_day: date,
        end_day: date,
        models: Sequence[str] | None = None,
    ) -> dict[str, list[WeatherSample]]: ...


def _as_float(value: Any) -> float:
    """None → NaN. Sıfıra düşürmek eksik veriyi geceden ayırt edilemez kılardı."""
    if value is None:
        return float("nan")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _field_key(block: dict[str, Any], name: str, model: str) -> str | None:
    """Ensemble isteklerinde alanlar `<name>_<model>` olarak döner; ikisini de dene."""
    if model:
        suffixed = f"{name}_{model}"
        if suffixed in block:
            return suffixed
    return name if name in block else None


def parse_minutely_15(payload: dict[str, Any], model: str = "") -> list[WeatherSample]:
    """`minutely_15` bloğunu WeatherSample listesine çevirir (tek model)."""
    block = payload.get("minutely_15") or {}
    times: list[str] = block.get("time") or []
    if not times:
        return []

    columns: dict[str, list[Any]] = {}
    for name in _MINUTELY_FIELDS:
        key = _field_key(block, name, model)
        raw = block.get(key) if key is not None else None
        columns[name] = list(raw) if isinstance(raw, list) else [None] * len(times)

    samples: list[WeatherSample] = []
    for index, time_str in enumerate(times):
        # timezone=UTC istendiğinden damgalar tz'siz ISO gelir; UTC olarak işaretle
        ts = datetime.fromisoformat(time_str).replace(tzinfo=UTC)

        def column(name: str, i: int = index) -> Any:
            values = columns[name]
            return values[i] if i < len(values) else None

        samples.append(
            WeatherSample(
                ts=ts,
                ghi_wm2=_as_float(column("shortwave_radiation")),
                dni_wm2=_as_float(column("direct_normal_irradiance")),
                dhi_wm2=_as_float(column("diffuse_radiation")),
                temp_c=_as_float(column("temperature_2m")),
                wind_ms=_as_float(column("wind_speed_10m")),
                relative_humidity_pct=_as_float(column("relative_humidity_2m")),
                precip_mm=_as_float(column("precipitation")),
                pressure_hpa=_as_float(column("surface_pressure")),
            )
        )
    return samples


def parse_ensemble(
    payload: dict[str, Any], models: Sequence[str]
) -> dict[str, list[WeatherSample]]:
    """Çok modelli yanıtı model adına göre ayrıştırır; boş seriler elenir."""
    members: dict[str, list[WeatherSample]] = {}
    for model in models:
        samples = parse_minutely_15(payload, model=model)
        if samples and any(s.has_irradiance for s in samples):
            members[model] = samples
    return members


class OpenMeteoClient:
    """Open-Meteo `/v1/forecast` istemcisi (deterministik + ensemble)."""

    def __init__(self, base_url: str = OPEN_METEO_BASE_URL, timeout_s: float = 30.0) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout_s)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _fetch(
        self,
        latitude: float,
        longitude: float,
        start_day: date,
        end_day: date,
        models: Sequence[str] | None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "latitude": latitude,
            "longitude": longitude,
            "minutely_15": ",".join(_MINUTELY_FIELDS),
            "start_date": start_day.isoformat(),
            "end_date": end_day.isoformat(),
            "timezone": "UTC",
            "wind_speed_unit": "ms",
        }
        if models:
            params["models"] = ",".join(models)
        response = await request_with_retry(self._client, "GET", "/v1/forecast", params=params)
        payload: dict[str, Any] = response.json()
        return payload

    async def fetch_day_15m(
        self, latitude: float, longitude: float, day: date
    ) -> list[WeatherSample]:
        """Tek günün deterministik (varsayılan model) serisi."""
        payload = await self._fetch(latitude, longitude, day, day, models=None)
        return parse_minutely_15(payload)

    async def fetch_range_15m(
        self,
        latitude: float,
        longitude: float,
        start_day: date,
        end_day: date,
        models: Sequence[str] | None = None,
    ) -> dict[str, list[WeatherSample]]:
        """Gün aralığını çeker; ensemble isteğinde model adı → seri sözlüğü döndürür.

        Deterministik istekte tek anahtar `""` (boş dize) kullanılır — çağıran
        taraf ensemble olup olmadığını sözlük uzunluğundan anlar.
        """
        payload = await self._fetch(latitude, longitude, start_day, end_day, models)
        if not models:
            return {"": parse_minutely_15(payload)}
        members = parse_ensemble(payload, models)
        if not members:
            # Model seti reddedildiyse (bölge kapsamı vb.) deterministik seriye düş
            fallback = parse_minutely_15(payload)
            return {"": fallback} if fallback else {}
        return members


def sample_interval(samples: Sequence[WeatherSample]) -> timedelta:
    """Serinin zaman adımını damgalardan çıkarır (tek örnekte 15 dk varsayılır)."""
    if len(samples) < 2:
        return timedelta(minutes=15)
    deltas = sorted((b.ts - a.ts) for a, b in zip(samples, samples[1:], strict=False))
    return deltas[len(deltas) // 2]
