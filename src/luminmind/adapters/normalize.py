"""Üretici JSON yanıtlarını kanonik `TelemetryPoint` modeline dönüştürür.

Her üreticinin alan adları ve birimleri farklıdır; tüm eşleme kuralları burada
tek yerde toplanır ve tablo bazlı birim testleriyle doğrulanır.

Birim dönüşümleri:
- Huawei Northbound: güçler kW, enerji kWh (dönüşüm gerekmez).
- SMA ennexOS: güçler W, enerji Wh → kW/kWh'e çevrilir.
"""

from datetime import UTC, datetime
from typing import Any

from luminmind.core.schemas import TelemetryPoint, Vendor


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
