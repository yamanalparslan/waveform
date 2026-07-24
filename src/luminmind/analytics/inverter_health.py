"""Cihaz seviyesinde sağlık takibi ve otomatik uyarı üretimi.

Ingestion görevi çalıştıktan sonra bu modül devreye girer:
- Her invertör için son telemetri noktasını Postgres `inverters` tablosundaki
  `last_*` alanlarına yazar (hızlı okuma için cache; kaynak yine Influx).
- Sağlık kurallarını değerlendirip anomali olayı üretir:
  * **Offline** — cihazdan `stale_after` süresinden uzun süredir veri yok.
  * **Overheat** — son sıcaklık `overheat_c` eşiğinin üstünde.
  * **Vendor error** — üretici `error_code` != "0" veya `status` bilinen bir
    "arıza" durumu.
Aynı kural için açık olay varsa güncellenir (tekilleştirme); durum düzelirse
olay `resolved` yapılır.
"""

import logging
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from luminmind.core.models import AnomalyEvent, Inverter, Plant
from luminmind.core.schemas import TelemetryPoint

logger = logging.getLogger(__name__)

# Uyarı kural sabitleri — konfigüre edilebilir (config.py'ye taşınabilir)
STALE_AFTER = timedelta(minutes=30)  # bu eşiği aşan cihaz "çevrimdışı" olur


def fresh_window(interval_minutes: int) -> timedelta:
    """Cihaz cache değerinin (anlık güç, günlük üretim) UI'da "canlı" sayılacağı
    süre. Bir polling atlanabilir toleransı için 2×interval, STALE_AFTER ile
    kapaklı — böylece 5 dk pollingde son 10 dk içinde güncellenen değer canlı,
    5 dk pollingde 15 dkdır kayıp olanınki eski sayılır."""
    return min(timedelta(minutes=interval_minutes * 2), STALE_AFTER)
OVERHEAT_C = 65.0
CRITICAL_OVERHEAT_C = 75.0
HEALTHY_STATUSES = frozenset({"AKTIF", "AKTİF", "ACTIVE", "OK", "NORMAL", "RUN", ""})


KIND_INV_OFFLINE = "inv_offline"
KIND_INV_OVERHEAT = "inv_overheat"
KIND_INV_ERROR = "inv_error"

DEVICE_KIND_LABELS = {
    KIND_INV_OFFLINE: "İnvertör çevrimdışı",
    KIND_INV_OVERHEAT: "Aşırı sıcaklık",
    KIND_INV_ERROR: "Üretici arıza kodu",
}


@dataclass(frozen=True)
class InverterSnapshot:
    """Bir cihazın son telemetri özeti (Influx yerine hızlı erişim için)."""

    vendor_plant_id: str
    vendor_device_id: str
    ts: datetime
    power_kw: float | None
    temp_c: float | None
    energy_daily_kwh: float | None
    error_code: str | None
    status: str | None


def latest_snapshots(points: Sequence[TelemetryPoint]) -> dict[tuple[str, str], InverterSnapshot]:
    """Bir ingestion turunun tüm noktalarından cihaz başına en son noktayı seçer."""
    latest: dict[tuple[str, str], InverterSnapshot] = {}
    for p in points:
        if p.vendor_device_id is None:
            continue
        key = (p.vendor_plant_id, p.vendor_device_id)
        existing = latest.get(key)
        if existing is None or p.ts > existing.ts:
            latest[key] = InverterSnapshot(
                vendor_plant_id=p.vendor_plant_id,
                vendor_device_id=p.vendor_device_id,
                ts=p.ts,
                power_kw=p.ac_power_kw,
                temp_c=p.temp_c,
                energy_daily_kwh=p.energy_daily_kwh,
                error_code=p.error_code,
                status=p.status,
            )
    return latest


async def upsert_inverter_state(
    session: AsyncSession, snapshots: Iterable[InverterSnapshot]
) -> int:
    """Cihaz durum cache'ini günceller. Yeni cihazsa otomatik oluşturur."""
    updated = 0
    for snap in snapshots:
        plant = (
            await session.scalars(
                select(Plant).where(Plant.vendor_plant_id == snap.vendor_plant_id)
            )
        ).one_or_none()
        if plant is None:
            continue
        inverter = (
            await session.scalars(
                select(Inverter).where(
                    Inverter.plant_id == plant.id,
                    Inverter.vendor_device_id == snap.vendor_device_id,
                )
            )
        ).one_or_none()
        if inverter is None:
            inverter = Inverter(
                plant_id=plant.id,
                vendor_device_id=snap.vendor_device_id,
                model=f"{plant.vendor} device",
            )
            session.add(inverter)
        inverter.last_seen_at = snap.ts
        inverter.last_power_kw = snap.power_kw
        inverter.last_temp_c = snap.temp_c
        inverter.last_energy_daily_kwh = snap.energy_daily_kwh
        inverter.last_error_code = snap.error_code
        inverter.last_status = snap.status
        updated += 1
    return updated


@dataclass(frozen=True)
class HealthFinding:
    """Sağlık kuralı tetikleyicisi (anomali olayına dönüştürülür)."""

    kind: str
    severity: str  # warning | critical
    deviation_pct: float  # anomaly_events şeması gerektirdiği için taşıyıcı alan
    started_at: datetime
    evidence: dict[str, str | float | None]


def evaluate_inverter(inverter: Inverter, now: datetime) -> list[HealthFinding]:
    """Bir cihazın son durumundan uyarı tetikleyicilerini hesaplar."""
    findings: list[HealthFinding] = []
    if inverter.last_seen_at is None:
        return findings  # henüz veri yok, kural uygulama

    age = now - inverter.last_seen_at
    if age > STALE_AFTER:
        findings.append(
            HealthFinding(
                kind=KIND_INV_OFFLINE,
                severity="critical" if age > STALE_AFTER * 4 else "warning",
                deviation_pct=-100.0,
                started_at=inverter.last_seen_at,
                evidence={
                    "device_id": inverter.vendor_device_id,
                    "minutes_since_last": round(age.total_seconds() / 60, 1),
                },
            )
        )
        # offline'ken diğer kurallar anlamsız — burada bit
        return findings

    if inverter.last_temp_c is not None and inverter.last_temp_c > OVERHEAT_C:
        findings.append(
            HealthFinding(
                kind=KIND_INV_OVERHEAT,
                severity="critical" if inverter.last_temp_c > CRITICAL_OVERHEAT_C else "warning",
                deviation_pct=round(inverter.last_temp_c - OVERHEAT_C, 1),
                started_at=inverter.last_seen_at,
                evidence={
                    "device_id": inverter.vendor_device_id,
                    "temp_c": inverter.last_temp_c,
                    "threshold_c": OVERHEAT_C,
                },
            )
        )

    if inverter.last_error_code is not None and inverter.last_error_code not in {"0", "0.0", ""}:
        findings.append(
            HealthFinding(
                kind=KIND_INV_ERROR,
                severity="critical",
                deviation_pct=0.0,
                started_at=inverter.last_seen_at,
                evidence={
                    "device_id": inverter.vendor_device_id,
                    "error_code": inverter.last_error_code,
                    "status": inverter.last_status,
                },
            )
        )
    elif inverter.last_status is not None and inverter.last_status.upper() not in HEALTHY_STATUSES:
        findings.append(
            HealthFinding(
                kind=KIND_INV_ERROR,
                severity="warning",
                deviation_pct=0.0,
                started_at=inverter.last_seen_at,
                evidence={
                    "device_id": inverter.vendor_device_id,
                    "status": inverter.last_status,
                },
            )
        )
    return findings


async def apply_findings(
    session: AsyncSession, plant_id: uuid.UUID, findings: list[HealthFinding], now: datetime
) -> tuple[int, int]:
    """Bulguları anomali tablosuna işler (dedupe + auto-resolve).

    Aynı (plant, kind, evidence.device_id) için açık olay varsa güncellenir;
    yoksa yenisi oluşturulur. Aynı grup için bulgusuz gelen açık olaylar
    otomatik `resolved` yapılır.
    """
    created = 0
    resolved = 0
    open_events = (
        await session.scalars(
            select(AnomalyEvent).where(
                AnomalyEvent.plant_id == plant_id,
                AnomalyEvent.status == "open",
                AnomalyEvent.kind.in_({KIND_INV_OFFLINE, KIND_INV_OVERHEAT, KIND_INV_ERROR}),
            )
        )
    ).all()

    def _device(e: AnomalyEvent) -> str:
        return str(e.evidence.get("device_id", ""))

    finding_by_key = {(f.kind, str(f.evidence.get("device_id", ""))): f for f in findings}

    for event in open_events:
        key = (event.kind, _device(event))
        if key in finding_by_key:
            # güncelle
            f = finding_by_key.pop(key)
            event.severity = f.severity
            event.deviation_pct = f.deviation_pct
            event.evidence = {k: str(v) if v is not None else "" for k, v in f.evidence.items()}
        else:
            # düzeldi
            event.status = "resolved"
            event.ended_at = now
            resolved += 1

    for f in finding_by_key.values():
        session.add(
            AnomalyEvent(
                plant_id=plant_id,
                kind=f.kind,
                severity=f.severity,
                deviation_pct=f.deviation_pct,
                started_at=f.started_at,
                ended_at=None,
                status="open",
                evidence={k: str(v) if v is not None else "" for k, v in f.evidence.items()},
            )
        )
        created += 1
    return created, resolved


