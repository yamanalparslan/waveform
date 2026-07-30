"""İnvertör sağlık senkronizasyonu ve uyarı motoru testleri."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from luminmind.analytics.inverter_health import (
    KIND_INV_ERROR,
    KIND_INV_OFFLINE,
    KIND_INV_OVERHEAT,
    OVERHEAT_C,
    STALE_AFTER,
    apply_findings,
    evaluate_inverter,
    latest_snapshots,
    upsert_inverter_state,
)
from luminmind.core.db import session_scope
from luminmind.core.models import AnomalyEvent, Base, Inverter, Plant, User
from luminmind.core.schemas import TelemetryPoint, Vendor

NOW = datetime(2026, 7, 21, 10, 0, tzinfo=UTC)


@pytest.fixture
async def engine():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with session_scope(engine) as session:
        user = User(email="a@b.c", hashed_password="x", role="admin")
        session.add(user)
        await session.flush()
        session.add(Plant(
            owner_id=user.id, name="Tescom İzmir",
            vendor="tescom", vendor_plant_id="tescom-izmir",
            latitude=38.4, longitude=27.1,
        ))
    yield engine
    await engine.dispose()


def _pt(dev: str, ts: datetime, **kwargs) -> TelemetryPoint:
    return TelemetryPoint(
        vendor=Vendor.TESCOM,
        vendor_plant_id="tescom-izmir",
        vendor_device_id=dev,
        ts=ts,
        **kwargs,
    )


def test_latest_snapshots_picks_newest_per_device():
    points = [
        _pt("1", NOW - timedelta(minutes=15), ac_power_kw=100.0),
        _pt("1", NOW, ac_power_kw=120.0),
        _pt("2", NOW - timedelta(minutes=10), ac_power_kw=90.0),
    ]
    snaps = latest_snapshots(points)
    assert len(snaps) == 2
    assert snaps[("tescom-izmir", "1")].power_kw == 120.0


def test_latest_snapshots_ignores_device_without_id():
    points = [_pt("1", NOW, ac_power_kw=100.0),
              TelemetryPoint(vendor=Vendor.TESCOM, vendor_plant_id="tescom-izmir", ts=NOW)]
    assert len(latest_snapshots(points)) == 1


async def test_upsert_creates_inverter_and_updates_state(engine):
    points = [_pt("1", NOW, ac_power_kw=124.7, temp_c=52.6, status="AKTIF", error_code="0")]
    async with session_scope(engine) as session:
        await upsert_inverter_state(session, latest_snapshots(points).values())

    async with session_scope(engine) as session:
        inv = (await session.scalars(select(Inverter))).one()
        assert inv.vendor_device_id == "1"
        assert inv.last_power_kw == 124.7
        assert inv.last_temp_c == 52.6
        assert inv.last_status == "AKTIF"


async def test_upsert_updates_existing_inverter(engine):
    async with session_scope(engine) as session:
        plant = (await session.scalars(select(Plant))).one()
        session.add(Inverter(plant_id=plant.id, vendor_device_id="1", model="eski"))
    points = [_pt("1", NOW, ac_power_kw=50.0, temp_c=40.0)]
    async with session_scope(engine) as session:
        await upsert_inverter_state(session, latest_snapshots(points).values())
    async with session_scope(engine) as session:
        inv = (await session.scalars(select(Inverter))).one()
        assert inv.model == "eski"  # meta korundu
        assert inv.last_power_kw == 50.0


def test_evaluate_healthy_inverter_returns_no_findings():
    inv = Inverter(
        vendor_device_id="1", last_seen_at=NOW, last_power_kw=100.0,
        last_temp_c=45.0, last_error_code="0", last_status="AKTIF",
    )
    assert evaluate_inverter(inv, NOW) == []


def test_evaluate_offline_inverter_triggers_offline():
    inv = Inverter(
        vendor_device_id="1", last_seen_at=NOW - STALE_AFTER - timedelta(minutes=1),
        last_power_kw=0.0, last_status="AKTIF",
    )
    [f] = evaluate_inverter(inv, NOW)
    assert f.kind == KIND_INV_OFFLINE
    # offline'ken diğer kurallar bastırılır
    assert len([x for x in evaluate_inverter(inv, NOW) if x.kind != KIND_INV_OFFLINE]) == 0


def test_offline_becomes_critical_after_long_gap():
    inv = Inverter(
        vendor_device_id="1", last_seen_at=NOW - STALE_AFTER * 5,
        last_power_kw=0.0, last_status="AKTIF",
    )
    [f] = evaluate_inverter(inv, NOW)
    assert f.severity == "critical"


def test_overheat_warning_and_critical():
    inv_warn = Inverter(
        vendor_device_id="1", last_seen_at=NOW, last_power_kw=100.0,
        last_temp_c=OVERHEAT_C + 5, last_status="AKTIF",
    )
    [f] = evaluate_inverter(inv_warn, NOW)
    assert f.kind == KIND_INV_OVERHEAT and f.severity == "warning"

    inv_crit = Inverter(
        vendor_device_id="1", last_seen_at=NOW, last_power_kw=100.0,
        last_temp_c=80.0, last_status="AKTIF",
    )
    [f] = evaluate_inverter(inv_crit, NOW)
    assert f.severity == "critical"


def test_vendor_error_code_triggers_error():
    inv = Inverter(
        vendor_device_id="1", last_seen_at=NOW, last_power_kw=0.0,
        last_error_code="42", last_status="ERROR",
    )
    findings = evaluate_inverter(inv, NOW)
    kinds = {f.kind for f in findings}
    assert KIND_INV_ERROR in kinds


def test_unknown_status_string_triggers_warning():
    inv = Inverter(
        vendor_device_id="1", last_seen_at=NOW, last_power_kw=50.0,
        last_error_code="0", last_status="MAINTENANCE",
    )
    [f] = evaluate_inverter(inv, NOW)
    assert f.kind == KIND_INV_ERROR and f.severity == "warning"


async def test_apply_findings_creates_and_dedupes(engine):
    async with session_scope(engine) as session:
        plant = (await session.scalars(select(Plant))).one()
        finding = _make_offline_finding()
        created, resolved = await apply_findings(session, plant.id, [finding], NOW)
        assert (created, resolved) == (1, 0)

    async with session_scope(engine) as session:
        # aynı finding tekrar → dedupe
        plant = (await session.scalars(select(Plant))).one()
        finding = _make_offline_finding()
        created, resolved = await apply_findings(session, plant.id, [finding], NOW)
        assert created == 0

    async with session_scope(engine) as session:
        events = (await session.scalars(select(AnomalyEvent))).all()
        assert len(events) == 1


async def test_apply_findings_resolves_when_healthy(engine):
    async with session_scope(engine) as session:
        plant = (await session.scalars(select(Plant))).one()
        await apply_findings(session, plant.id, [_make_offline_finding()], NOW)

    async with session_scope(engine) as session:
        plant = (await session.scalars(select(Plant))).one()
        created, resolved = await apply_findings(session, plant.id, [], NOW)
        assert resolved == 1

    async with session_scope(engine) as session:
        event = (await session.scalars(select(AnomalyEvent))).one()
        assert event.status == "resolved"
        assert event.ended_at is not None


async def test_same_device_number_in_two_sites_stays_two_events(engine):
    """Cihaz numarası saha içinde tekil; sahasız tekilleştirme birini yok ederdi."""
    from luminmind.analytics.inverter_health import HealthFinding
    from luminmind.core.models import Site

    async with session_scope(engine) as session:
        plant = (await session.scalars(select(Plant))).one()
        session.add_all(
            [
                Site(
                    plant_id=plant.id, name="Üretim", code="uretim",
                    series_key="tescom-izmir-uretim", dc_capacity_kwp=400.0, display_order=1,
                ),
                Site(
                    plant_id=plant.id, name="Mekanik", code="mekanik",
                    series_key="tescom-izmir-mekanik", dc_capacity_kwp=250.0, display_order=2,
                ),
            ]
        )

    async with session_scope(engine) as session:
        plant = (await session.scalars(select(Plant))).one()
        sites = {s.code: s for s in (await session.scalars(select(Site))).all()}
        findings = [
            HealthFinding(
                kind=KIND_INV_OFFLINE, severity="warning", deviation_pct=-100.0,
                started_at=NOW, evidence={"device_id": "1"}, site_id=sites[code].id,
            )
            for code in ("uretim", "mekanik")
        ]
        created, resolved = await apply_findings(session, plant.id, findings, NOW)
        assert (created, resolved) == (2, 0)

    async with session_scope(engine) as session:
        events = (await session.scalars(select(AnomalyEvent))).all()
        assert len({e.site_id for e in events}) == 2  # her fabrikanın kendi olayı


def test_findings_inherit_the_site_of_their_device():
    site_id = uuid.uuid4()
    inv = Inverter(
        vendor_device_id="1", site_id=site_id,
        last_seen_at=NOW - STALE_AFTER * 2, last_power_kw=0.0,
    )
    [finding] = evaluate_inverter(inv, NOW)
    assert finding.kind == KIND_INV_OFFLINE
    assert finding.site_id == site_id


def test_siteless_install_still_produces_findings():
    """Mock/Huawei kurulumlarında saha yok; kural motoru yine çalışmalı."""
    inv = Inverter(
        vendor_device_id="1", last_seen_at=NOW - STALE_AFTER * 2, last_power_kw=0.0
    )
    [finding] = evaluate_inverter(inv, NOW)
    assert finding.site_id is None


def _make_offline_finding():
    from luminmind.analytics.inverter_health import HealthFinding
    return HealthFinding(
        kind=KIND_INV_OFFLINE, severity="warning", deviation_pct=-100.0,
        started_at=NOW - timedelta(hours=1),
        evidence={"device_id": "1", "minutes_since_last": 60.0},
    )
