"""Teknik veriyi tesis sahibinin dilinde "yapılacak iş"e çeviren katman.

Arayüzün geri kalanı ham `AnomalyEvent` / kWh değerleri yerine buradaki
yapıları kullanır. Üç soruya cevap veririz:

* **Ne oldu?** — cihaz/olay türünden türetilen tek cümlelik başlık.
* **Ne kaybettiriyor?** — tarife (₺/kWh) üzerinden günlük kayıp tahmini.
* **Ne yapmalı?** — somut, sahada uygulanabilir tek adım.

Kayıp tahminleri *kaba*dır: kurulu güç × Türkiye ortalama günlük eşdeğer tam
yük saati üzerinden hesaplanır ve kullanıcıya daima "≈" ile gösterilir. Amaç
kesin muhasebe değil, işleri önem sırasına dizmek.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from luminmind.analytics.insights import (
    PEAK_SUN_HOURS_TR,
    daily_loss_kwh,
    device_id_of,
    playbook_for,
)
from luminmind.config import Settings
from luminmind.core.models import AnomalyEvent, Plant

STATUS_LABELS = {"open": "Yapılacak", "acked": "İlgileniliyor", "resolved": "Tamamlandı"}
STATUS_CHIP = {"open": "crit", "acked": "warn", "resolved": "ok"}

# Performans oranı bantları (%). Durum cümlesi, ısı haritası renkleri ve
# tablodaki çipler aynı eşikleri kullanır; farklı yerlerde farklı sayı olması
# "ekran yeşil ama tablo kırmızı" çelişkisi üretirdi.
PR_EXCELLENT_PCT = 95.0
PR_NORMAL_PCT = 85.0
PR_WEAK_PCT = 70.0


def performance_chip(pct: float) -> str:
    """Performans oranını (%) durum çipi adına çevirir: ok / warn / crit."""
    if pct >= PR_NORMAL_PCT:
        return "ok"
    if pct >= PR_WEAK_PCT:
        return "warn"
    return "crit"


# --------------------------- para ve sayı biçimi ---------------------------


def fmt_number(value: float, decimals: int = 0) -> str:
    """Türkçe sayı biçimi: binlik ayıracı nokta, ondalık ayıracı virgül."""
    text = f"{value:,.{decimals}f}"
    return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def fmt_try(value: float, decimals: int = 0) -> str:
    """₺ tutarı — 1.000 ₺ altındaki küçük tutarlarda kuruş göstermeye gerek yok."""
    return f"{fmt_number(value, decimals)} ₺"


def tariff_for(plant: Plant, settings: Settings) -> float:
    """Tesisin satış tarifesi; girilmemişse kurulum genelindeki varsayılan."""
    return plant.feed_in_tariff_try_kwh or settings.lm_default_tariff_try_kwh


def money_of(kwh: float, tariff: float) -> float:
    return kwh * tariff


# --------------------------- portföy durum cümlesi ---------------------------


@dataclass(frozen=True)
class Headline:
    """Ana ekranın tepesindeki "iyi mi gidiyor?" cevabı."""

    chip: str  # ok | warn | crit | info
    title: str
    detail: str


def portfolio_headline(
    actual_kwh: float,
    expected_kwh: float,
    open_task_count: int,
    *,
    has_data: bool = True,
) -> Headline:
    """Üretim/beklenti oranı ve açık iş sayısından tek cümlelik durum üretir."""
    if not has_data:
        return Headline(
            "info",
            "Veri akışı henüz başlamadı",
            "Santralden ilk ölçümler geldiğinde bu ekran kendiliğinden dolacak.",
        )

    work = (
        f" {open_task_count} işiniz var."
        if open_task_count
        else " Bekleyen işiniz yok."
    )

    if expected_kwh <= 1.0:
        return Headline(
            "info",
            f"Bugün {fmt_number(actual_kwh)} kWh ürettiniz",
            "Hava durumuna göre karşılaştırma için biraz daha veri gerekiyor." + work,
        )

    ratio = actual_kwh / expected_kwh
    pct = fmt_number(ratio * 100)
    if ratio * 100.0 >= PR_EXCELLENT_PCT:
        return Headline(
            "ok",
            "Her şey yolunda",
            f"Bugün havanın elverdiğinin %{pct} kadarını ürettiniz." + work,
        )
    if ratio * 100.0 >= PR_NORMAL_PCT:
        return Headline(
            "ok",
            "Normal seyrinde",
            f"Bugün havanın elverdiğinin %{pct} kadarını ürettiniz." + work,
        )
    if ratio * 100.0 >= PR_WEAK_PCT:
        return Headline(
            "warn",
            "Beklenenin altında",
            f"Bugün havanın elverdiğinin yalnızca %{pct} kadarını ürettiniz."
            " Aşağıdaki işlere göz atın.",
        )
    return Headline(
        "crit",
        "Üretim ciddi biçimde düşük",
        f"Bugün havanın elverdiğinin yalnızca %{pct} kadarını ürettiniz."
        " Bir arıza olma ihtimali yüksek.",
    )


# --------------------------- yapılacak işler ---------------------------


@dataclass(frozen=True)
class Task:
    """Kullanıcıya gösterilen tek bir iş kalemi."""

    id: uuid.UUID
    plant_id: uuid.UUID
    plant_name: str
    title: str
    impact: str
    action: str
    urgency_label: str
    urgency_chip: str
    status: str
    status_label: str
    status_chip: str
    since_label: str
    device_id: str | None
    daily_loss_try: float


def _since_label(started_at: datetime, now: datetime) -> str:
    seconds = max(0, int((now - started_at).total_seconds()))
    if seconds < 3600:
        return f"{max(1, seconds // 60)} dakikadır"
    if seconds < 86400:
        return f"{seconds // 3600} saattir"
    return f"{seconds // 86400} gündür"


def _device_label(device_id: str | None) -> str:
    return f"{device_id} nolu invertör" if device_id else "Santral"


def _plant_daily_kwh(plant: Plant) -> float:
    return (plant.dc_capacity_kwp or 0.0) * PEAK_SUN_HOURS_TR


def _device_daily_kwh(plant: Plant, inverter_count: int) -> float:
    """Bir invertörün günlük payı — tesis üretimi cihazlara eşit dağıtılmış sayılır."""
    if inverter_count <= 0:
        return 0.0
    return _plant_daily_kwh(plant) / inverter_count


def _impact_label(daily_loss_try: float) -> str:
    if daily_loss_try < 1.0:
        return "Parasal etkisi hesaplanamadı — tesis kurulu gücü girilmemiş"
    return f"Günde yaklaşık {fmt_try(daily_loss_try)} kayıp"


def _describe(
    event: AnomalyEvent, plant: Plant, device_id: str | None, inverter_count: int
) -> tuple[str, str, float]:
    """Olay türüne göre (başlık, yapılacak adım, günlük kayıp kWh) döndürür.

    Başlık, öneri ve kayıp modeli `analytics/insights.py`'deki `PLAYBOOK`'tan
    okunur. Aynı bilgi iki yerde durursa biri güncellenmeyi unutulur ve
    kullanıcı aynı arıza için iki farklı öneri görür; "yapılacak iş" listesi ile
    "kurtarılabilir gelir" tablosunun aynı cümleyi söylemesi zorunlu.
    """
    playbook = playbook_for(event.kind)
    return (
        playbook.title.format(device=_device_label(device_id)),
        playbook.recommendation,
        daily_loss_kwh(
            playbook,
            event.deviation_pct,
            _plant_daily_kwh(plant),
            _device_daily_kwh(plant, inverter_count),
        ),
    )


def build_task(
    event: AnomalyEvent,
    plant: Plant,
    tariff: float,
    inverter_count: int,
    now: datetime | None = None,
) -> Task:
    """Bir anomali olayını kullanıcı diliyle yazılmış iş kalemine çevirir."""
    now = now or datetime.now(tz=UTC)
    started_at = event.started_at
    if started_at.tzinfo is None:  # SQLite gibi tz-naive sürücüler
        started_at = started_at.replace(tzinfo=UTC)

    device_id = device_id_of(event)
    title, action, loss_kwh = _describe(event, plant, device_id, inverter_count)
    daily_loss_try = money_of(loss_kwh, tariff)

    if event.status != "open":
        urgency_label, urgency_chip = "—", "muted"
    elif event.severity == "critical":
        urgency_label, urgency_chip = "Acil", "crit"
    else:
        urgency_label, urgency_chip = "Bu hafta", "warn"

    return Task(
        id=event.id,
        plant_id=plant.id,
        plant_name=plant.name,
        title=title,
        impact=_impact_label(daily_loss_try),
        action=action,
        urgency_label=urgency_label,
        urgency_chip=urgency_chip,
        status=event.status,
        status_label=STATUS_LABELS.get(event.status, event.status),
        status_chip=STATUS_CHIP.get(event.status, "muted"),
        since_label=_since_label(started_at, now),
        device_id=device_id,
        daily_loss_try=daily_loss_try,
    )


def sort_tasks(tasks: list[Task]) -> list[Task]:
    """Açık işler önce, sonra en çok para kaybettiren en üstte."""
    order = {"open": 0, "acked": 1, "resolved": 2}
    return sorted(tasks, key=lambda t: (order.get(t.status, 3), -t.daily_loss_try))
