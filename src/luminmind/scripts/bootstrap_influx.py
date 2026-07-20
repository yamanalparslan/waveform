"""InfluxDB bucket bootstrap: lm_raw / lm_hourly / lm_daily + retention (PLAN.md §3.2).

Çalıştırma: `python -m luminmind.scripts.bootstrap_influx` (compose'ta influxdb
ayağa kalktıktan sonra bir kez). Idempotenttir — mevcut bucket'lara dokunmaz.
"""

import logging

from influxdb_client.client.influxdb_client import InfluxDBClient
from influxdb_client.domain.bucket_retention_rules import BucketRetentionRules

from luminmind.config import get_settings
from luminmind.core.influx import BUCKET_DAILY, BUCKET_HOURLY, BUCKET_RAW

logger = logging.getLogger(__name__)

_DAY_S = 24 * 3600
# bucket -> saniye cinsinden retention (0 = sınırsız)
_BUCKETS: dict[str, int] = {
    BUCKET_RAW: 400 * _DAY_S,
    BUCKET_HOURLY: 3 * 365 * _DAY_S,
    BUCKET_DAILY: 0,
}


def bootstrap(client: InfluxDBClient, org: str) -> list[str]:
    """Eksik bucket'ları oluşturur; oluşturulanların adlarını döndürür."""
    buckets_api = client.buckets_api()
    created: list[str] = []
    for name, retention_s in _BUCKETS.items():
        if buckets_api.find_bucket_by_name(name) is not None:
            logger.info("bucket %s already exists", name)
            continue
        rules = (
            [BucketRetentionRules(type="expire", every_seconds=retention_s)]
            if retention_s > 0
            else []
        )
        buckets_api.create_bucket(bucket_name=name, org=org, retention_rules=rules)
        logger.info("created bucket %s (retention=%ss)", name, retention_s or "infinite")
        created.append(name)
    return created


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    if not settings.influx_url:
        raise SystemExit("INFLUX_URL is not configured")
    with InfluxDBClient(
        url=settings.influx_url, token=settings.influx_token, org=settings.influx_org
    ) as client:
        bootstrap(client, settings.influx_org)


if __name__ == "__main__":
    main()
