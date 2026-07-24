"""Örnek veri tohumu: admin kullanıcı + mock tesis + invertörler + BESS + kimlik bilgisi.

Çalıştırma: `python -m luminmind.scripts.seed` (önce `alembic upgrade head`).
Idempotenttir — mevcut kayıtları e-posta / vendor_plant_id üzerinden bulur, çoğaltmaz.
"""

import asyncio
import json
import logging
import os

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from luminmind.config import get_settings
from luminmind.core.db import create_engine, session_scope
from luminmind.core.models import (
    BatterySystem,
    Inverter,
    Plant,
    PvArray,
    User,
    VendorCredential,
)
from luminmind.core.schemas import Vendor
from luminmind.core.security import encrypt_payload, hash_password

logger = logging.getLogger(__name__)

_MOCK_PLANT_ID = "mock-plant-1"
_PLANT_NAME = "Konya GES"


async def seed(session: AsyncSession) -> None:
    admin = (
        await session.scalars(select(User).where(User.email == "admin@luminmind.local"))
    ).one_or_none()
    if admin is None:
        admin = User(
            email="admin@luminmind.local",
            hashed_password=hash_password(os.environ.get("SEED_ADMIN_PASSWORD", "admin")),
            role="admin",
        )
        session.add(admin)
        logger.info("created admin user %s", admin.email)

    await _seed_mock_plant(session, admin)
    await _seed_tescom_plant(session, admin)


async def _seed_tescom_plant(session: AsyncSession, admin: User) -> None:
    """Tescom API yapılandırılmışsa İzmir tesisini Postgres'e ekler (idempotent).

    Böylece gerçek üretim verisi arayüzde/haritada görünür ve (kapasite girildiyse)
    dijital ikiz beklenen üretimi hesaplar. Depolama (BESS) tanımlanmaz — PV izleme.
    """
    # Not: Tescom tesisleri artık adaptör tarafından dinamik keşfedilir
    # (upsert_discovered_plants); her `fabrika_id` ayrı bir tesis olarak
    # otomatik oluşturulur. Bu fonksiyon eski tek-tesis kurulumlarındaki
    # `tescom-izmir` kaydını yerinde bırakır — çünkü Influx'ta hâlâ o
    # plant_id ile geçmiş veri olabilir.
    return


async def _seed_mock_plant(session: AsyncSession, admin: User) -> None:
    plant = (
        await session.scalars(select(Plant).where(Plant.vendor_plant_id == _MOCK_PLANT_ID))
    ).one_or_none()
    if plant is not None:
        # Mevcut kurulumlarda tesis adını hedefe senkronla (idempotent rename)
        if plant.name != _PLANT_NAME:
            plant.name = _PLANT_NAME
            logger.info("renamed existing plant to %s", _PLANT_NAME)
        else:
            logger.info("seed already applied; nothing to do")
        return

    plant = Plant(
        owner=admin,
        name=_PLANT_NAME,
        vendor=Vendor.MOCK.value,
        vendor_plant_id=_MOCK_PLANT_ID,
        latitude=37.87,
        longitude=32.48,
        dc_capacity_kwp=1000.0,
        ac_capacity_kw=1000.0,
    )
    plant.inverters = [
        Inverter(
            vendor_device_id=f"{_MOCK_PLANT_ID}-inv-{i:02d}",
            model="Mock String Inverter 250kW",
            ac_capacity_kw=250.0,
        )
        for i in range(1, 5)
    ]
    plant.pv_arrays = [
        PvArray(
            modules_per_string=25,
            strings=73,  # 25 × 73 × 550 W ≈ 1004 kWp
            tilt_deg=25.0,
            azimuth_deg=180.0,
            module_params={"pdc0": 550.0, "gamma_pdc": -0.0035},
        )
    ]
    plant.batteries = [
        BatterySystem(
            chemistry="NMC-21700",
            cells_series=8,
            cells_parallel=1,
            pack_count=1,
            rated_energy_kwh=0.4,  # 8S masaüstü Ar-Ge düzeneği (Faz 4 kalibrasyon kaynağı)
            rated_power_kw=0.5,
        )
    ]
    settings = get_settings()
    if settings.credentials_enc_key:
        plant.credential = VendorCredential(
            vendor=Vendor.MOCK.value,
            auth_type="session",
            encrypted_payload=encrypt_payload(
                json.dumps({"note": "mock vendor, no real credentials"}),
                settings.credentials_enc_key,
            ),
        )
    session.add(plant)
    logger.info("created mock plant with 4 inverters and 1 battery system")


def main() -> None:
    logging.basicConfig(level=logging.INFO)

    async def _run() -> None:
        engine = create_engine()
        try:
            async with session_scope(engine) as session:
                await seed(session)
        finally:
            await engine.dispose()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
