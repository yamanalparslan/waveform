from datetime import UTC, datetime, timedelta

from luminmind.adapters import MockAdapter


async def test_daytime_generation_positive_and_split_across_devices():
    noon = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)  # UTC 10:00 = TRT 13:00, tepe civarı
    async with MockAdapter(now=noon) as adapter:
        plants = await adapter.fetch_plants()
        points = await adapter.fetch_telemetry(
            plants[0].vendor_plant_id, since=noon - timedelta(minutes=15)
        )

    # 15 dk'da 4 invertör × 2 slot (10:00 dahil, 09:45'ten sonraki ilk slot 09:45... ceil → 09:45+)
    assert points, "gündüz veri üretilmeli"
    assert all(p.ac_power_kw is not None and p.ac_power_kw > 0 for p in points)
    per_ts = {p.ts for p in points}
    for ts in per_ts:
        slot_points = [p for p in points if p.ts == ts]
        assert len(slot_points) == 4  # cihaz başına bir nokta


async def test_night_generation_is_zero():
    midnight = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)
    async with MockAdapter(now=midnight) as adapter:
        points = await adapter.fetch_telemetry(
            "mock-plant-1", since=midnight - timedelta(minutes=15)
        )
    assert points
    assert all(p.ac_power_kw == 0.0 for p in points)


async def test_deterministic_output():
    now = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)
    since = now - timedelta(minutes=15)
    async with MockAdapter(now=now) as a1, MockAdapter(now=now) as a2:
        p1 = await a1.fetch_telemetry("mock-plant-1", since=since)
        p2 = await a2.fetch_telemetry("mock-plant-1", since=since)
    assert p1 == p2


async def test_timestamps_aligned_to_15m_grid():
    now = datetime(2026, 7, 20, 10, 7, tzinfo=UTC)
    async with MockAdapter(now=now) as adapter:
        points = await adapter.fetch_telemetry(
            "mock-plant-1", since=now - timedelta(minutes=20)
        )
    assert points
    for p in points:
        assert p.ts.minute % 15 == 0
        assert p.ts.second == 0
