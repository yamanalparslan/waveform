"""Teknik veriyi kullanıcı diline çeviren katmanın testleri (web/advice.py)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from luminmind.analytics.inverter_health import (
    KIND_INV_ERROR,
    KIND_INV_OFFLINE,
    KIND_INV_OVERHEAT,
)
from luminmind.config import Settings
from luminmind.core.models import AnomalyEvent, Plant
from luminmind.web.advice import (
    PEAK_SUN_HOURS_TR,
    build_task,
    fmt_number,
    fmt_try,
    portfolio_headline,
    sort_tasks,
    tariff_for,
)

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


def make_plant(capacity=1000.0, tariff=None):
    return Plant(
        id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        name="Konya GES",
        vendor="mock",
        vendor_plant_id="mock-plant-1",
        dc_capacity_kwp=capacity,
        feed_in_tariff_try_kwh=tariff,
        timezone="Europe/Istanbul",
    )


def make_event(kind, *, severity="warning", deviation=-10.0, status="open", evidence=None, age_h=2):
    return AnomalyEvent(
        id=uuid.uuid4(),
        plant_id=uuid.uuid4(),
        kind=kind,
        severity=severity,
        deviation_pct=deviation,
        started_at=NOW - timedelta(hours=age_h),
        status=status,
        evidence=evidence if evidence is not None else {},
    )


# ------------------------------ biçimlendirme ------------------------------


@pytest.mark.parametrize(
    ("value", "decimals", "expected"),
    [
        (1234.0, 0, "1.234"),
        (1234567.0, 0, "1.234.567"),
        (2.9, 2, "2,90"),
        (0.0, 0, "0"),
    ],
)
def test_fmt_number_uses_turkish_separators(value, decimals, expected):
    assert fmt_number(value, decimals) == expected


def test_fmt_try_appends_currency():
    assert fmt_try(3100.0) == "3.100 ₺"


# ------------------------------ tarife ------------------------------


def test_tariff_prefers_plant_over_default():
    settings = Settings(lm_default_tariff_try_kwh=2.9)
    assert tariff_for(make_plant(tariff=3.4), settings) == 3.4


def test_tariff_falls_back_to_default_when_plant_has_none():
    settings = Settings(lm_default_tariff_try_kwh=2.9)
    assert tariff_for(make_plant(tariff=None), settings) == 2.9


# ------------------------------ durum cümlesi ------------------------------


def test_headline_is_positive_when_close_to_expectation():
    h = portfolio_headline(actual_kwh=980.0, expected_kwh=1000.0, open_task_count=0)
    assert h.chip == "ok"
    assert h.title == "Her şey yolunda"
    assert "Bekleyen işiniz yok" in h.detail


def test_headline_warns_when_meaningfully_below_expectation():
    h = portfolio_headline(actual_kwh=750.0, expected_kwh=1000.0, open_task_count=2)
    assert h.chip == "warn"
    assert h.title == "Beklenenin altında"
    assert "%75" in h.detail


def test_headline_is_critical_when_far_below_expectation():
    h = portfolio_headline(actual_kwh=400.0, expected_kwh=1000.0, open_task_count=1)
    assert h.chip == "crit"
    assert "arıza" in h.detail


def test_headline_without_expectation_only_reports_production():
    h = portfolio_headline(actual_kwh=1240.0, expected_kwh=0.0, open_task_count=3)
    assert h.chip == "info"
    assert "1.240 kWh" in h.title
    assert "3 işiniz var" in h.detail


def test_headline_without_data_explains_instead_of_alarming():
    h = portfolio_headline(0.0, 0.0, 0, has_data=False)
    assert h.chip == "info"
    assert "henüz başlamadı" in h.title


# ------------------------------ görev üretimi ------------------------------


def test_offline_inverter_task_speaks_plainly_and_prices_the_loss():
    plant = make_plant(capacity=1000.0, tariff=2.9)
    event = make_event(KIND_INV_OFFLINE, severity="critical", evidence={"device_id": "3"})

    task = build_task(event, plant, tariff=2.9, inverter_count=4, now=NOW)

    assert task.title == "3 nolu invertör veri göndermiyor"
    assert "şalter" in task.action
    assert task.urgency_label == "Acil"
    assert task.since_label == "2 saattir"
    # 1000 kWp / 4 cihaz × 4,5 sa × 2,9 ₺ ≈ 3.262 ₺
    assert task.daily_loss_try == pytest.approx(1000.0 / 4 * PEAK_SUN_HOURS_TR * 2.9)
    assert task.impact.startswith("Günde yaklaşık 3.26")
    assert task.impact.endswith("₺ kayıp")


def test_overheat_task_charges_only_the_derated_share():
    plant = make_plant(capacity=1000.0)
    event = make_event(KIND_INV_OVERHEAT, evidence={"device_id": "2"})

    task = build_task(event, plant, tariff=2.9, inverter_count=4, now=NOW)

    assert task.title == "2 nolu invertör fazla ısındı"
    assert task.urgency_label == "Bu hafta"
    # tam kayıp değil, beşte biri
    assert task.daily_loss_try == pytest.approx(1000.0 / 4 * PEAK_SUN_HOURS_TR * 2.9 * 0.2)


def test_error_task_tells_user_to_pass_the_code_to_service():
    event = make_event(KIND_INV_ERROR, severity="critical", evidence={"device_id": "1"})
    task = build_task(event, make_plant(), tariff=2.9, inverter_count=4, now=NOW)
    assert task.title == "1 nolu invertör arıza bildiriyor"
    assert "hata kodunu" in task.action


def test_soiling_task_scales_loss_with_deviation():
    plant = make_plant(capacity=1000.0)
    event = make_event("soiling", deviation=-8.0)

    task = build_task(event, plant, tariff=2.9, inverter_count=4, now=NOW)

    assert task.title == "Paneller kirlenmiş görünüyor"
    assert "temizliği" in task.action
    assert task.daily_loss_try == pytest.approx(1000.0 * PEAK_SUN_HOURS_TR * 0.08 * 2.9)


def test_task_without_capacity_says_so_instead_of_showing_zero_lira():
    plant = make_plant(capacity=None)
    task = build_task(make_event("shading"), plant, tariff=2.9, inverter_count=4, now=NOW)
    assert "hesaplanamadı" in task.impact
    assert "₺" not in task.impact


def test_resolved_task_has_no_urgency_badge():
    event = make_event(KIND_INV_OFFLINE, status="resolved", evidence={"device_id": "3"})
    task = build_task(event, make_plant(), tariff=2.9, inverter_count=4, now=NOW)
    assert task.urgency_label == "—"
    assert task.status_label == "Tamamlandı"


def test_device_id_absent_falls_back_to_plant_wording():
    task = build_task(make_event("microcrack"), make_plant(), 2.9, 4, NOW)
    assert task.device_id is None
    assert "garanti" in task.action.lower()


def test_since_label_switches_units():
    plant = make_plant()
    minutes = build_task(make_event("soiling", age_h=0), plant, 2.9, 4, NOW + timedelta(minutes=20))
    hours = build_task(make_event("soiling", age_h=5), plant, 2.9, 4, NOW)
    days = build_task(make_event("soiling", age_h=50), plant, 2.9, 4, NOW)
    assert minutes.since_label == "20 dakikadır"
    assert hours.since_label == "5 saattir"
    assert days.since_label == "2 gündür"


def test_sort_puts_open_tasks_first_then_costliest():
    plant = make_plant(capacity=1000.0)
    cheap_open = build_task(make_event("soiling", deviation=-2.0), plant, 2.9, 4, NOW)
    costly_open = build_task(make_event("shading", deviation=-30.0), plant, 2.9, 4, NOW)
    costly_done = build_task(
        make_event("microcrack", deviation=-50.0, status="resolved"), plant, 2.9, 4, NOW
    )

    ordered = sort_tasks([cheap_open, costly_done, costly_open])

    assert [t.id for t in ordered] == [costly_open.id, cheap_open.id, costly_done.id]
