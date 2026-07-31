import json
import os
from pathlib import Path
from typing import Any

# Testler geliştiricinin `.env` dosyasından tamamen bağımsız olmalı. Bu satır
# `luminmind.config` ilk kez içe aktarılmadan önce çalışır (conftest her zaman
# test modüllerinden önce yüklenir) ve dosya okumasını kapatır. Aksi halde
# makinede `.env` olup olmamasına göre testler farklı sonuç verir.
os.environ["LM_ENV_FILE"] = ""

import pytest  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def load_fixture():
    def _load(relative_path: str) -> dict[str, Any]:
        return json.loads((FIXTURES / relative_path).read_text())

    return _load


@pytest.fixture(scope="session")
def synthetic_tmy():
    """8760 satırlık sentetik tipik meteorolojik yıl üreten kurucu.

    Fizibilite testleri (`test_prospect_*`) gerçek PVGIS yanıtı yerine bunu
    kullanıyor: amaç uydu verisini taklit etmek değil, üretim zincirine tutarlı
    bir yıl vermek. Birden çok test modülü paylaştığı için burada duruyor —
    `tests/unit` bir paket olmadığından modüller arası içe alma çalışmıyor.

    `scale` açık gökyüzü ışınımını çarpar. Varsayılan 0,72 bulutluluğu temsil
    eder; bulutsuz bir yıl gerçekçi olmayan ~2200 kWh/kWp verirdi. Kırpma
    davranışı sınanırken 1,0 kullanılmalı, çünkü ölçekleme tam olarak kırpmaya
    yol açan öğle tepelerini bastırıyor.
    """
    import math

    import pandas as pd
    from pvlib.location import Location

    from luminmind.prospect.pvgis import TMY_REFERENCE_YEAR, TmyDataset

    def _build(
        latitude: float = 37.87,
        longitude: float = 32.48,
        scale: float = 0.72,
        min_temp_c: float = -12.0,
    ) -> "TmyDataset":
        start = pd.Timestamp(year=TMY_REFERENCE_YEAR, month=1, day=1, tz="UTC")
        times = pd.date_range(start, periods=8760, freq="1h")
        clearsky = Location(latitude, longitude, tz="UTC").get_clearsky(
            times, model="simplified_solis"
        )

        frame = clearsky[["ghi", "dni", "dhi"]].astype(float) * scale
        seasonal = -pd.Series(
            [
                math.cos(2.0 * math.pi * (day - 15.0) / 365.0)
                for day in times.dayofyear.to_numpy(dtype=float)
            ],
            index=times,
        )
        daily = pd.Series(
            [
                math.sin(math.pi * max(0.0, (hour - 6.0) / 12.0))
                for hour in times.hour.to_numpy(dtype=float)
            ],
            index=times,
        )
        # Ortalama 13 °C, mevsimsel ±14 °C, günlük ±5 °C; sonra en düşük değer
        # `min_temp_c`'ye oturtulur — string boyutlandırma bu uca bakıyor.
        frame["temp_air"] = 13.0 + 14.0 * seasonal + 5.0 * daily
        frame["temp_air"] += min_temp_c - float(frame["temp_air"].min())
        frame["wind_speed"] = 2.5
        frame["relative_humidity"] = 45.0
        frame["pressure"] = 900.0

        return TmyDataset(
            weather=frame,
            latitude=latitude,
            longitude=longitude,
            altitude_m=1_020.0,
            radiation_db="PVGIS-SARAH3",
            meteo_db="ERA5",
            year_min=2005,
            year_max=2023,
            horizon_included=True,
            irradiance_time_offset_h=0.1712,
            months_selected=tuple((month, 2015) for month in range(1, 13)),
        )

    return _build
