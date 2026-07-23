"""Gün-öncesi üretim tahmini görevi (day-ahead PV forecast).

Dijital ikizi yarının (ve birkaç gün ilerisinin) Open-Meteo hava tahminine
koşarak beklenen üretimi hesaplar ve `twin_expected` ölçümüne gelecek zaman
damgalarıyla yazar. Böylece arayüz "yarının üretim tahmini"ni gösterir ve
(DeepSolar Predict tarzı) arbitraj/dengeleme kararlarına girdi olur.

Aynı twin boru hattını kullanır — tek fark hedef günün gelecekte olması.
Idempotenttir: aynı (tesis, gün) yeniden yazılır.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from luminmind.config import Settings, get_settings
from luminmind.twin.weather import WeatherProvider
from luminmind.workers.celery_app import app
from luminmind.workers.tasks.twin import TwinSink, _resolve_configs, run_twin

logger = logging.getLogger(__name__)

# Kaç gün ileriyi tahmin edelim — Open-Meteo ~16 güne kadar verir; GÖP/planlama
# için 2 gün (bugün+yarın kapsanır) pratik ve doğruluğu yüksek penceredir.
DEFAULT_DAYS_AHEAD = 2


async def run_forecast(
    settings: Settings | None = None,
    days_ahead: int = DEFAULT_DAYS_AHEAD,
    sink: TwinSink | None = None,
    weather: WeatherProvider | None = None,
) -> int:
    """Yarından itibaren `days_ahead` gün için beklenen üretimi hesaplar.

    Tesis konfigürasyonları bir kez çözülür ve her gün için twin çalıştırılır;
    yazılan toplam nokta sayısını döndürür.
    """
    settings = settings or get_settings()
    configs = await _resolve_configs(settings)
    if not configs:
        logger.info("forecast: no plant configs; skipped")
        return 0

    today = datetime.now(tz=UTC).date()
    total = 0
    for offset in range(1, days_ahead + 1):
        target = today + timedelta(days=offset)
        written = await run_twin(
            settings=settings,
            sink=sink,
            weather=weather,
            day=target,
            configs=configs,
        )
        total += written
        logger.info("forecast day=%s points=%d", target.isoformat(), written)
    logger.info("forecast run complete: %d points over %d days", total, days_ahead)
    return total


@app.task(name="luminmind.forecast_generation")  # type: ignore[untyped-decorator]
def forecast_generation() -> int:
    return asyncio.run(run_forecast())
