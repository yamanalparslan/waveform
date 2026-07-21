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
