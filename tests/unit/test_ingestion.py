from luminmind.adapters import MockAdapter
from luminmind.config import Settings
from luminmind.workers.tasks.ingestion import build_adapters, run_ingestion


def test_build_adapters_mock_mode():
    settings = Settings(lm_use_mock_vendors=True)
    adapters = build_adapters(settings)
    assert len(adapters) == 1
    assert isinstance(adapters[0], MockAdapter)


def test_build_adapters_real_mode_requires_config():
    settings = Settings(lm_use_mock_vendors=False)
    assert build_adapters(settings) == []


def test_build_adapters_real_mode_with_huawei():
    settings = Settings(
        lm_use_mock_vendors=False,
        huawei_base_url="https://eu5.fusionsolar.huawei.com",
        huawei_username="u",
        huawei_system_code="s",
    )
    adapters = build_adapters(settings)
    assert len(adapters) == 1
    assert type(adapters[0]).__name__ == "HuaweiAdapter"


def test_build_adapters_real_mode_with_tescom():
    settings = Settings(
        lm_use_mock_vendors=False,
        tescom_base_url="http://host.docker.internal:8503",
        tescom_api_key="k",
    )
    adapters = build_adapters(settings)
    assert len(adapters) == 1
    assert type(adapters[0]).__name__ == "TescomAdapter"


async def test_run_ingestion_mock_end_to_end():
    settings = Settings(lm_use_mock_vendors=True, ingestion_interval_minutes=15)
    total = await run_ingestion(settings)
    # 1 mock tesis × 4 invertör × en az 1 slot
    assert total >= 4
    assert total % 4 == 0


class CapturingSink:
    def __init__(self):
        self.points = []

    async def write_telemetry(self, points):
        self.points.extend(points)


async def test_run_ingestion_writes_to_injected_sink():
    settings = Settings(lm_use_mock_vendors=True, ingestion_interval_minutes=15)
    sink = CapturingSink()
    total = await run_ingestion(settings, sink=sink)
    assert total == len(sink.points)
    assert all(p.vendor_plant_id == "mock-plant-1" for p in sink.points)


async def test_ingest_adapter_isolates_plant_discovery_failure():
    """fetch_plants patlarsa (ör. 401) tur çökmesin — boş dönsün."""
    from luminmind.adapters.base import AdapterError
    from luminmind.core.schemas import Vendor
    from luminmind.workers.tasks.ingestion import ingest_adapter

    class FailingAdapter:
        vendor = Vendor.TESCOM

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def fetch_plants(self):
            raise AdapterError("401 unauthorized")

    from datetime import UTC, datetime

    points, plants = await ingest_adapter(FailingAdapter(), since=datetime.now(tz=UTC))
    assert points == []
    assert plants == []


async def test_ingest_to_continues_when_one_adapter_fails():
    """Bir adaptör patlasa da sink'e yazım akışı çökmesin (izolasyon)."""
    from datetime import UTC, datetime

    from luminmind.core.schemas import TelemetryPoint, Vendor
    from luminmind.workers.tasks.ingestion import _ingest_to

    class BoomAdapter:
        vendor = Vendor.HUAWEI

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def fetch_plants(self):
            raise RuntimeError("boom")

    good_point = TelemetryPoint(
        vendor=Vendor.MOCK, vendor_plant_id="p", vendor_device_id="1",
        ts=datetime.now(tz=UTC), ac_power_kw=10.0,
    )

    class GoodAdapter:
        vendor = Vendor.MOCK

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def fetch_plants(self):
            from luminmind.core.schemas import PlantMeta
            return [PlantMeta(vendor=Vendor.MOCK, vendor_plant_id="p", name="P")]

        async def fetch_telemetry(self, vendor_plant_id, since):
            return [good_point]

    import luminmind.workers.tasks.ingestion as ing

    orig = ing.build_adapters
    ing.build_adapters = lambda s: [BoomAdapter(), GoodAdapter()]
    try:
        sink = CapturingSink()
        total = await _ingest_to(sink, Settings(lm_use_mock_vendors=True))
    finally:
        ing.build_adapters = orig
    # Patlayan adaptöre rağmen iyi adaptörün noktası yazıldı
    assert total == 1
    assert sink.points == [good_point]
