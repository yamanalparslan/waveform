"""Örnek veri tohumu: admin kullanıcı + mock tesis + invertörler + BESS + kimlik bilgisi.

Çalıştırma: `python -m luminmind.scripts.seed` (önce `alembic upgrade head`).
Idempotenttir — mevcut kayıtları e-posta / vendor_plant_id üzerinden bulur, çoğaltmaz.
"""

import asyncio
import json
import logging
import os
from datetime import date

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
from luminmind.twin.plant_model import MountType

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

    # Demo tesis yalnızca mock modda kurulur. Gerçek üretici bağlıyken bunu
    # koşulsuz yaratmak, kaldırılan demo tesisi her `init` çalıştırmasında geri
    # getirirdi. Gerçek tesis/saha yapısı `scripts/bootstrap_sites.py`'nin işi.
    if get_settings().lm_use_mock_vendors:
        await _seed_mock_plant(session, admin)
    else:
        logger.info("gerçek üretici yapılandırılmış; demo tesis kurulmadı")


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

    # Gerçekçi bir saha: 1004 kWp DC / 800 kW AC → DC/AC ≈ 1,25. Bu oran önemli,
    # çünkü invertör kırpması dijital ikizin öğlen tepesini belirler; DC=AC
    # varsayımı ikizi gerçekte olmayan bir tepeye götürür.
    plant = Plant(
        owner=admin,
        name=_PLANT_NAME,
        vendor=Vendor.MOCK.value,
        vendor_plant_id=_MOCK_PLANT_ID,
        latitude=37.87,
        longitude=32.48,
        altitude_m=1020.0,  # Konya ovası
        dc_capacity_kwp=1004.0,
        ac_capacity_kw=800.0,
        grid_export_limit_kw=800.0,
        commissioned_on=date(2021, 6, 1),
    )
    plant.inverters = [
        Inverter(
            vendor_device_id=f"{_MOCK_PLANT_ID}-inv-{i:02d}",
            model="Mock String Inverter 200kW",
            ac_capacity_kw=200.0,
        )
        for i in range(1, 5)
    ]
    # Her invertöre bir dizi: 25 × 18 × 550 W ≈ 247,5 kWp (toplam ≈ 990 kWp)
    plant.pv_arrays = [
        PvArray(
            inverter=inverter,
            modules_per_string=25,
            strings=18,
            tilt_deg=25.0,
            azimuth_deg=180.0,
            module_params={"pdc0": 550.0, "gamma_pdc": -0.0035},
            mount_type=MountType.FIXED_GROUND.value,
            gcr=0.40,
            albedo=0.20,
            bifaciality=0.0,
            module_type="monosi",
        )
        for inverter in plant.inverters
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
