"""Dijital ikiz görevi: hava verisi çek → beklenen üretimi hesapla → Influx'a yaz.

Saat başı çalışır ve iki iş yapar:

1. **Bugün (D+0)** — güncel hava tahminiyle günün tamamı (15 dk çözünürlük)
   yeniden hesaplanır ve `twin_expected` serisine yazılır. Karşılaştırma/anomali
   motorunun okuduğu seri budur. Aynı (tesis, zaman) üzerine yazım idempotenttir.
2. **D+1..D+N** — ileri tahminler `twin_forecast` serisine, `horizon_days`
   etiketiyle yazılır. Arbitraj LP'si bunları kullanır; ayrıca ufka göre
   doğruluk ölçmeyi (skor tahtası) mümkün kılar.

Belirsizlik: `lm_twin_ensemble` açıkken birbirinden bağımsız sayısal hava
tahmini modelleri aynı istekte çekilir ve üye yayılımından P10/P50/P90 bandı
üretilir. Band olmadan "beklenenin altındayız" cümlesi ölçülemez bir iddiadır.

Tesis konfigürasyonu: mock modda mock tesis sabitleri; gerçek modda Postgres
`plants` + `pv_arrays` + `inverters` + `twin_calibrations` tablolarından
yüklenir (dizi kaydı yoksa kapasiteden jenerik dizi türetilir).
"""

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from luminmind.config import Settings, get_settings
from luminmind.core.influx import InfluxStore
from luminmind.core.schemas import TwinPoint
from luminmind.twin.calibration import CalibrationState
from luminmind.twin.components import LossChain
from luminmind.twin.expected import (
    TwinPlantConfig,
    expected_generation,
    expected_generation_ensemble,
    weather_to_frame,
)
from luminmind.twin.plant_model import ArrayConfig, MountType, default_array_for_capacity
from luminmind.twin.soiling import SoilingConfig
from luminmind.twin.weather import (
    DEFAULT_ENSEMBLE_MODELS,
    OpenMeteoClient,
    WeatherProvider,
    WeatherSample,
)
from luminmind.workers.celery_app import app

logger = logging.getLogger(__name__)

# Mock tesis (adapters/mock.py ve scripts/seed.py ile aynı değerler):
# ≈990 kWp DC / 800 kW AC → DC/AC ≈ 1,24, yani öğlen kırpma gerçekçi biçimde oluşur.
_MOCK_TWIN_CONFIG = TwinPlantConfig(
    plant_id="mock-plant-1",
    latitude=37.87,
    longitude=32.48,
    altitude_m=1020.0,
    arrays=[default_array_for_capacity(990.0, ac_capacity_kw=800.0)],
    soiling=SoilingConfig(),
)


class TwinSink(Protocol):
    async def write_twin(self, points: Sequence[TwinPoint]) -> None: ...


class LogTwinSink:
    async def write_twin(self, points: Sequence[TwinPoint]) -> None:
        for point in points:
            logger.debug("twin %s", point.model_dump_json(exclude_none=True))


def _mount_type(raw: str | None) -> MountType:
    try:
        return MountType(raw) if raw else MountType.FIXED_GROUND
    except ValueError:
        logger.warning("unknown mount_type %r; falling back to fixed_ground", raw)
        return MountType.FIXED_GROUND


def _plant_age_years(commissioned_on: date | None, today: date) -> float:
    if commissioned_on is None:
        return 0.0
    return max(0.0, (today - commissioned_on).days / 365.25)


async def load_twin_configs(
    engine: AsyncEngine, today: date | None = None
) -> list[TwinPlantConfig]:
    """Postgres'ten **saha** bazlı dizi + kalibrasyon konfigürasyonlarını okur.

    İkiz saha seviyesinde çalışır: her fabrikanın kendi kapasitesi, yönelimi ve
    kirlilik geçmişi var. Tek bir tesis konfigürasyonu 400 kWp ile 250 kWp'yi
    aynı fiziksel modele sıkıştırırdı ve ikisinin de beklenen üretimi yanlış
    çıkardı. `TwinPlantConfig.plant_id` bu yüzden **seri anahtarıdır** —
    ölçümlerin yazıldığı Influx etiketiyle birebir aynı.

    Koordinatsız hedefler atlanır — konum olmadan güneş geometrisi kurulamaz.
    """
    from luminmind.core.db import session_scope
    from luminmind.core.models import TwinCalibration
    from luminmind.core.series import all_series_targets

    reference_day = today or datetime.now(tz=UTC).date()
    configs: list[TwinPlantConfig] = []
    async with session_scope(engine) as session:
        for target in await all_series_targets(session):
            plant, site = target.plant, target.site
            series_key = target.series_key
            latitude = (site.latitude if site else None) or plant.latitude
            longitude = (site.longitude if site else None) or plant.longitude
            if latitude is None or longitude is None:
                logger.warning("%s has no coordinates; twin skipped", series_key)
                continue

            inverters = await plant.awaitable_attrs.inverters
            inverter_ac_by_id = {
                inv.id: inv.ac_capacity_kw for inv in inverters if inv.ac_capacity_kw
            }
            all_arrays = await plant.awaitable_attrs.pv_arrays
            # Saha varsa yalnızca o sahanın dizileri; yoksa tesisin tamamı
            pv_arrays = (
                [a for a in all_arrays if a.site_id == site.id] if site else list(all_arrays)
            )
            arrays = [
                ArrayConfig(
                    tilt_deg=a.tilt_deg,
                    azimuth_deg=a.azimuth_deg,
                    modules_per_string=a.modules_per_string,
                    strings=a.strings,
                    module_pdc0_w=float(a.module_params.get("pdc0", 550.0)),
                    gamma_pdc=float(a.module_params.get("gamma_pdc", -0.0035)),
                    inverter_ac_kw=inverter_ac_by_id.get(a.inverter_id),
                    mount=_mount_type(a.mount_type),
                    gcr=a.gcr if a.gcr else 0.40,
                    albedo=a.albedo if a.albedo is not None else 0.20,
                    bifaciality=a.bifaciality if a.bifaciality is not None else 0.0,
                    module_type=a.module_type or "monosi",
                )
                for a in pv_arrays
            ]
            if not arrays:
                # Kapasite 0 "sıfır güçlü santral" değil, "kapasite girilmemiş"
                # demektir. `is None` kontrolü bunu kaçırıyordu: 0 kWp'den tek
                # modüllük sahte bir dizi türetiliyor, o tesisin tüm beklenen
                # üretimi ~0 çıkıyor ve gerçek üretim sonsuz pozitif sapma
                # üretiyordu (anomali motoru ve doğruluk skoru çöpe dönüyordu).
                capacity = target.capacity_kwp
                ac_capacity = (site.ac_capacity_kw if site else None) or plant.ac_capacity_kw
                if not capacity or capacity <= 0:
                    logger.warning(
                        "%s has no arrays and no usable capacity (%s kWp); twin skipped",
                        series_key,
                        capacity,
                    )
                    continue
                arrays = [default_array_for_capacity(capacity, ac_capacity_kw=ac_capacity)]

            calibration_filter = (
                TwinCalibration.site_id == site.id
                if site
                else TwinCalibration.plant_id == plant.id
            )
            latest = (
                await session.scalars(
                    select(TwinCalibration)
                    .where(calibration_filter)
                    .order_by(TwinCalibration.fitted_at.desc())
                    .limit(1)
                )
            ).one_or_none()
            calibration = (
                CalibrationState(
                    plant_id=series_key,
                    scale=latest.scale,
                    hour_bias={int(h): float(v) for h, v in (latest.hour_bias or {}).items()},
                    soiling_base_ratio=latest.soiling_base_ratio,
                    sample_count=latest.sample_count,
                    fitted_at=latest.fitted_at,
                    quality={k: float(v) for k, v in (latest.quality or {}).items()},
                    version=latest.version,
                )
                if latest is not None
                else None
            )
            commissioned = (site.commissioned_on if site else None) or plant.commissioned_on
            losses = LossChain().with_age(_plant_age_years(commissioned, reference_day))
            configs.append(
                TwinPlantConfig(
                    plant_id=series_key,
                    latitude=latitude,
                    longitude=longitude,
                    arrays=arrays,
                    losses=losses,
                    altitude_m=(site.altitude_m if site else None) or plant.altitude_m or 0.0,
                    calibration=calibration,
                    soiling=SoilingConfig(
                        base_ratio=calibration.soiling_base_ratio if calibration else 1.0
                    ),
                )
            )
    return configs


async def _resolve_configs(settings: Settings, today: date) -> list[TwinPlantConfig]:
    if settings.lm_use_mock_vendors:
        return [_MOCK_TWIN_CONFIG]
    from luminmind.core.db import create_engine

    engine = create_engine(settings.postgres_dsn)
    try:
        return await load_twin_configs(engine, today)
    finally:
        await engine.dispose()


def ensemble_models(settings: Settings) -> tuple[str, ...]:
    """Ayarlardan ensemble model listesi; kapalıysa boş demet."""
    if not settings.lm_twin_ensemble:
        return ()
    configured = [m.strip() for m in settings.lm_twin_ensemble_models.split(",") if m.strip()]
    return tuple(configured) if configured else DEFAULT_ENSEMBLE_MODELS


async def collect_weather(
    provider: WeatherProvider,
    latitude: float,
    longitude: float,
    day: date,
    models: Sequence[str],
) -> dict[str, list[WeatherSample]]:
    """Bir gün için hava üyelerini toplar; ensemble desteklemeyen sağlayıcıda tek üye."""
    fetch_range = getattr(provider, "fetch_range_15m", None)
    if models and callable(fetch_range):
        members: dict[str, list[WeatherSample]] = await fetch_range(
            latitude, longitude, day, day, models
        )
        if members:
            return members
        logger.warning("ensemble fetch returned no members for %s; falling back", day.isoformat())
    return {"": await provider.fetch_day_15m(latitude, longitude, day)}


def _points_for_day(
    config: TwinPlantConfig,
    members: dict[str, list[WeatherSample]],
    horizon_days: int,
) -> list[TwinPoint]:
    frames = {name: weather_to_frame(samples) for name, samples in members.items() if samples}
    if not frames:
        return []
    if len(frames) == 1:
        return expected_generation(
            config, next(iter(frames.values())), horizon_days=horizon_days
        )
    return expected_generation_ensemble(config, frames, horizon_days=horizon_days)


async def run_twin(
    settings: Settings | None = None,
    sink: TwinSink | None = None,
    weather: WeatherProvider | None = None,
    day: date | None = None,
    configs: list[TwinPlantConfig] | None = None,
    horizon_days: int | None = None,
) -> int:
    """Beklenen üretimi hesaplar; yazılan nokta sayısını döndürür.

    `day` verildiğinde yalnızca o gün (ufuk 0) hesaplanır — geçmişe dönük
    yeniden hesaplama ve testler için. Verilmezse bugünden başlayarak
    `horizon_days` gün ileriye kadar hesaplanır.
    """
    settings = settings or get_settings()
    today = datetime.now(tz=UTC).date()
    horizon = 0 if day is not None else (
        horizon_days if horizon_days is not None else settings.lm_twin_horizon_days
    )
    base_day = day or today
    configs = configs if configs is not None else await _resolve_configs(settings, base_day)
    models = ensemble_models(settings)

    own_weather = weather is None
    weather_client = weather or OpenMeteoClient()
    all_points: list[TwinPoint] = []
    try:
        for config in configs:
            for offset in range(horizon + 1):
                target_day = base_day + timedelta(days=offset)
                members = await collect_weather(
                    weather_client, config.latitude, config.longitude, target_day, models
                )
                points = _points_for_day(config, members, horizon_days=offset)
                all_points.extend(points)
                logger.info(
                    "twin computed plant=%s day=%s horizon=%d members=%d points=%d",
                    config.plant_id,
                    target_day.isoformat(),
                    offset,
                    len(members),
                    len(points),
                )

        if sink is not None:
            await sink.write_twin(all_points)
        elif settings.influx_url:
            async with InfluxStore(
                url=settings.influx_url, org=settings.influx_org, token=settings.influx_token
            ) as store:
                await store.write_twin(all_points)
        else:
            await LogTwinSink().write_twin(all_points)
    finally:
        if own_weather and isinstance(weather_client, OpenMeteoClient):
            await weather_client.aclose()
    logger.info("twin run complete: %d points", len(all_points))
    return len(all_points)


@app.task(name="luminmind.compute_expected_generation")  # type: ignore[untyped-decorator]
def compute_expected_generation() -> int:
    return asyncio.run(run_twin())
