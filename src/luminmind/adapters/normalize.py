"""Üretici JSON yanıtlarını kanonik `TelemetryPoint` modeline dönüştürür.

Her üreticinin alan adları ve birimleri farklıdır; tüm eşleme kuralları burada
tek yerde toplanır ve tablo bazlı birim testleriyle doğrulanır.

Birim dönüşümleri:
- Huawei Northbound: güçler kW, enerji kWh (dönüşüm gerekmez).
- SMA ennexOS: güçler W, enerji Wh → kW/kWh'e çevrilir.
"""

import logging
from datetime import UTC, datetime, tzinfo
from typing import Any

from luminmind.core.schemas import TelemetryPoint, Vendor

logger = logging.getLogger(__name__)


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_huawei_dev_kpi(
    vendor_plant_id: str, payload: dict[str, Any]
) -> list[TelemetryPoint]:
    """Huawei FusionSolar Northbound `getDevFiveMinutes`/`getDevRealKpi` yanıtı.

    Beklenen yapı::

        {"success": true, "failCode": 0,
         "data": [{"devId": 1000000031104426,
                   "collectTime": 1719900000000,          # epoch ms, UTC
                   "dataItemMap": {"active_power": 12.5,  # kW
                                   "mppt_power": 13.1,    # kW (DC)
                                   "pv1_u": 610.2,        # V (1. string)
                                   "pv1_i": 8.9,          # A (1. string)
                                   "total_cap": 15230.5,  # kWh (kümülatif)
                                   "temperature": 41.2}}]}

    DC voltaj/akım için 1. MPPT string'i (pv1) referans alınır; string bazlı
    detay Faz 3'te tek hat modeliyle birlikte ele alınacak.
    """
    points: list[TelemetryPoint] = []
    for item in payload.get("data") or []:
        data = item.get("dataItemMap") or {}
        collect_time_ms = item.get("collectTime")
        if collect_time_ms is None:
            continue
        points.append(
            TelemetryPoint(
                vendor=Vendor.HUAWEI,
                vendor_plant_id=vendor_plant_id,
                vendor_device_id=str(item["devId"]),
                ts=datetime.fromtimestamp(collect_time_ms / 1000, tz=UTC),
                ac_power_kw=_as_float(data.get("active_power")),
                dc_power_kw=_as_float(data.get("mppt_power")),
                dc_voltage_v=_as_float(data.get("pv1_u")),
                dc_current_a=_as_float(data.get("pv1_i")),
                energy_total_kwh=_as_float(data.get("total_cap")),
                temp_c=_as_float(data.get("temperature")),
            )
        )
    return points


def _parse_tescom_ts(value: str, tz: tzinfo) -> datetime | None:
    """Tescom yerel zaman damgasını (tz'siz) verilen zaman dilimiyle işaretler.

    **Ayırıcı uç noktaya göre değişiyor.** `/devices` damgayı `datetime` olarak
    döndürüyor ve FastAPI onu ISO 8601 ile serileştiriyor (`2026-08-03T14:13:55`),
    `/devices/{id}/latest` ise `str(datetime)` uyguladığı için boşluk kullanıyor
    (`2026-08-03 14:13:55`). Eskiden yalnız boşluklu biçim `strptime` ile
    denenirdi; üretici `/devices` çıktısını ISO'ya çevirdiği gün her nokta
    sessizce düştü ve çekim "0 points" diyerek başarılı göründü.
    `fromisoformat` iki ayırıcıyı da kabul eder.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        # Sessiz düşüş bir daha teşhis edilemez olmasın: biçim değişikliği
        # üretimde tüm çekimi durdurabiliyor.
        logger.warning("Tescom timestamp not ISO 8601, point dropped: %r", value)
        return None
    # Üretici tz'siz yerel saat gönderiyor; bir gün offset eklerse ezmeyelim.
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=tz)


def normalize_tescom_devices(
    default_plant_id: str,
    payload: list[dict[str, Any]],
    tz: tzinfo,
    plant_id_by_factory: dict[str, str] | None = None,
) -> list[TelemetryPoint]:
    """Tescom `/api/v1/devices` yanıtını kanonik modele dönüştürür.

    Beklenen yapı (cihaz listesi)::

        [{"fabrika_id": "uretim", "slave_id": 1,
          "zaman": "2026-07-21 10:39:27.661885",
          "guc": 124.73,              # AC güç, kW
          "voltaj": 801.9,            # DC gerilim, V
          "akim": 90.33,              # DC akım, A
          "sicaklik": 52.6,           # °C
          "gunluk_uretim_kwh": 22.0,  # gün başından beri üretim
          "hata_kodu": 0, "durum": "AKTIF"}]

    **`fabrika_id` cihaz kimliğinin parçasıdır.** API'de `slave_id` yalnızca
    fabrika içinde tekildir: hem `mekanik` hem `uretim` fabrikasında 1 numaralı
    cihaz vardır. Yalnız `slave_id` kullanılırsa iki fabrikanın cihazı aynı
    zaman serisine yazılır ve yakın zaman damgalarında biri diğerinin üzerine
    yazarak veriyi yok eder. Bu yüzden her fabrika ayrı bir `vendor_plant_id`
    (saha anahtarı) altına yazılır; eşleme `plant_id_by_factory` ile verilir.

    `fabrika_id` içermeyen eski/kısmi yanıtlar `default_plant_id` altında
    toplanır — geriye dönük uyumluluk için.

    Zaman damgası tz'siz yerel (fabrika saati) gelir; `tz` ile işaretlenip
    `TelemetryPoint` içinde UTC'ye çevrilir.
    """
    mapping = plant_id_by_factory or {}
    points: list[TelemetryPoint] = []
    for item in payload:
        raw_ts = item.get("zaman") or item.get("son_zaman")
        if not isinstance(raw_ts, str):
            continue
        ts = _parse_tescom_ts(raw_ts, tz)
        if ts is None:
            continue
        factory = item.get("fabrika_id")
        factory_key = str(factory) if factory is not None else ""
        error_code = item.get("hata_kodu")

        dev_id = item.get("id")
        if dev_id is None:
            dev_id = item.get("slave_id")

        points.append(
            TelemetryPoint(
                vendor=Vendor.TESCOM,
                vendor_plant_id=mapping.get(factory_key, default_plant_id),
                vendor_device_id=str(dev_id),
                ts=ts,
                ac_power_kw=_as_float(item.get("guc")),
                dc_voltage_v=_as_float(item.get("voltaj")),
                dc_current_a=_as_float(item.get("akim")),
                temp_c=_as_float(item.get("sicaklik")),
                # Gün başından beri üretilen enerji; saatlik/günlük agregatların
                # tek kaynağı budur (eşlenmediği sürece enerji hep boş kalıyordu)
                energy_total_kwh=_as_float(item.get("gunluk_uretim_kwh")),
                error_code=str(error_code) if error_code is not None else None,
                status=item.get("durum") if isinstance(item.get("durum"), str) else None,
            )
        )
    return points


def normalize_sma_measurements(
    vendor_plant_id: str, payload: dict[str, Any]
) -> list[TelemetryPoint]:
    """SMA ennexOS ölçüm yanıtı.

    NOT: SMA'nın herkese açık resmi API şeması yok (PLAN.md açık soru #2);
    bu yapı gerçek erişim sağlanana kadar geçicidir ve mock fixture'larla
    birebir aynı tutulur. Gerçek şema netleşince yalnızca bu fonksiyon değişir.

    Beklenen yapı::

        {"plantId": "sma-plant-1",
         "measurements": [{"deviceId": "inv-01",
                           "timestamp": "2026-07-20T10:15:00Z",
                           "values": {"pvGeneration": 12500,   # W
                                      "dcVoltage": 610.5,      # V
                                      "dcCurrent": 8.9,        # A
                                      "totalYield": 15230500,  # Wh (kümülatif)
                                      "temperature": 41.0}}]}
    """
    points: list[TelemetryPoint] = []
    for item in payload.get("measurements") or []:
        values = item.get("values") or {}
        timestamp = item.get("timestamp")
        if timestamp is None:
            continue
        ac_power_w = _as_float(values.get("pvGeneration"))
        total_yield_wh = _as_float(values.get("totalYield"))
        points.append(
            TelemetryPoint(
                vendor=Vendor.SMA,
                vendor_plant_id=vendor_plant_id,
                vendor_device_id=str(item["deviceId"]),
                ts=datetime.fromisoformat(timestamp),
                ac_power_kw=None if ac_power_w is None else ac_power_w / 1000,
                dc_voltage_v=_as_float(values.get("dcVoltage")),
                dc_current_a=_as_float(values.get("dcCurrent")),
                energy_total_kwh=None if total_yield_wh is None else total_yield_wh / 1000,
                temp_c=_as_float(values.get("temperature")),
            )
        )
    return points
