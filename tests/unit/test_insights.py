"""Kurtarılabilir gelir katmanı: fiyatlama, önceliklendirme ve çift sayma.

Bu dosyanın en önemli testleri normalizasyon bölümündedir. Bulgular birbirinden
bağımsız üretildiği için aynı kWh'i birden çok kez talep edebilirler; ham toplama
"Potansiyel Kurtarılabilir Yıllık Gelir" rakamını gerçek kaybın katlarına
çıkarır ve ekranın tamamının güvenilirliğini bitirir.
"""

import uuid
from datetime import UTC, date, datetime

import pytest

from luminmind.analytics.accuracy import AccuracyPair, score_day
from luminmind.analytics.classifiers import KIND_MICROCRACK, KIND_SHADING, KIND_SOILING
from luminmind.analytics.insights import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    CONFIDENCE_UNKNOWN,
    DAYS_PER_YEAR,
    IMMEDIATE_LOSS_FRACTION,
    KIND_CALIBRATION_SCALE,
    KIND_DEGRADATION,
    KIND_HOUR_BIAS,
    OVERHEAT_DERATE,
    PEAK_SUN_HOURS_TR,
    PERSISTENCE,
    PRIORITY_IMMEDIATE,
    PRIORITY_LONG,
    PRIORITY_MID,
    SCOPE_DEVICE,
    SCOPE_SITE,
    LossClaim,
    LossFinding,
    Playbook,
    SiteContext,
    classify_priority,
    confidence_for,
    daily_loss_kwh,
    device_daily_kwh,
    device_id_of,
    finding_from_event,
    normalize_claims,
    playbook_for,
    portfolio_insights,
    shortfall_from_score,
    site_insights,
)
from luminmind.analytics.inverter_health import (
    KIND_INV_ERROR,
    KIND_INV_OFFLINE,
    KIND_INV_OVERHEAT,
)
from luminmind.core.models import AnomalyEvent

TARIFF = 3.0
URETIM = "tescom-izmir-uretim"
MEKANIK = "tescom-izmir-mekanik"

# 400 kWp × 4,5 sa = 1.800 kWh/gün
SITE_DAILY = 400.0 * PEAK_SUN_HOURS_TR


def context(
    key: str = URETIM,
    *,
    capacity: float | None = 400.0,
    devices: int = 1,
    shortfall: float | None = None,
    nmae: float | None = None,
    annual: float | None = None,
) -> SiteContext:
    return SiteContext(
        series_key=key,
        name="Üretim Fabrikası",
        capacity_kwp=capacity,
        tariff_try_kwh=TARIFF,
        device_count=devices,
        measured_shortfall_kwh=shortfall,
        accuracy_nmae_pct=nmae,
        annual_expected_kwh=annual,
    )


def finding(
    kind: str,
    *,
    site: str = URETIM,
    severity: str = "warning",
    deviation: float = 0.0,
    device: str | None = None,
) -> LossFinding:
    return LossFinding(
        kind=kind,
        site_key=site,
        severity=severity,
        deviation_pct=deviation,
        device_id=device,
        started_at=datetime(2026, 7, 29, 9, 0, tzinfo=UTC),
    )


# ------------------------------ playbook ------------------------------


def test_known_kinds_have_a_playbook():
    for kind in (
        KIND_INV_OFFLINE,
        KIND_INV_ERROR,
        KIND_INV_OVERHEAT,
        KIND_MICROCRACK,
        KIND_SHADING,
        KIND_SOILING,
        KIND_HOUR_BIAS,
        KIND_CALIBRATION_SCALE,
        KIND_DEGRADATION,
    ):
        book = playbook_for(kind)
        assert book.title and book.recommendation
        assert book.scope in {SCOPE_DEVICE, SCOPE_SITE}


def test_unknown_kind_falls_back_instead_of_disappearing():
    """Tanımadığımız tür sessizce düşerse kullanıcı kaybı hiç görmez."""
    book = playbook_for("bilinmeyen_arıza")
    assert "{device}" in book.title
    assert book.priority == PRIORITY_MID


def test_device_scoped_titles_carry_the_device_number():
    insights = site_insights(
        context(devices=2), [finding(KIND_INV_OFFLINE, severity="critical", device="3")]
    )
    assert insights[0].title == "3 nolu invertör veri göndermiyor"


def test_site_scoped_titles_do_not_mention_a_device():
    insights = site_insights(context(), [finding(KIND_SOILING, deviation=-8.0)])
    assert insights[0].title == "Paneller kirlenmiş görünüyor"
    assert insights[0].device_id is None


# ------------------------------ kayıp modeli ------------------------------


def test_device_share_splits_site_potential():
    assert device_daily_kwh(SITE_DAILY, 2) == pytest.approx(900.0)
    assert device_daily_kwh(SITE_DAILY, 0) == 0.0  # cihaz yoksa pay da yok


def test_offline_device_loses_its_whole_share():
    loss = daily_loss_kwh(playbook_for(KIND_INV_OFFLINE), 0.0, SITE_DAILY, 900.0)
    assert loss == pytest.approx(900.0)


def test_overheating_device_only_loses_the_derated_part():
    loss = daily_loss_kwh(playbook_for(KIND_INV_OVERHEAT), 0.0, SITE_DAILY, 900.0)
    assert loss == pytest.approx(900.0 * OVERHEAT_DERATE)


def test_site_finding_scales_with_deviation():
    loss = daily_loss_kwh(playbook_for(KIND_SOILING), -10.0, SITE_DAILY, 900.0)
    assert loss == pytest.approx(180.0)


def test_deviation_sign_does_not_flip_the_loss():
    """Sınıflandırıcı negatif, cihaz sağlığı pozitif sapma yazıyor; ikisi de kayıp."""
    book = playbook_for(KIND_SHADING)
    assert daily_loss_kwh(book, -12.0, SITE_DAILY, 0.0) == daily_loss_kwh(
        book, 12.0, SITE_DAILY, 0.0
    )


# ------------------------------ çift sayma normalizasyonu ------------------------------


def test_one_device_cannot_lose_more_than_it_produces():
    claims = [
        LossClaim(SCOPE_DEVICE, "1", 900.0),  # çevrimdışı
        LossClaim(SCOPE_DEVICE, "1", 180.0),  # aynı cihaz, arıza kodu
    ]
    values = normalize_claims(claims, SITE_DAILY, 900.0)
    assert sum(values) == pytest.approx(900.0)
    # oransal küçültme: büyük iddia payını korur
    assert values[0] == pytest.approx(750.0)
    assert values[1] == pytest.approx(150.0)


def test_separate_devices_keep_their_own_ceilings():
    claims = [LossClaim(SCOPE_DEVICE, "1", 900.0), LossClaim(SCOPE_DEVICE, "2", 900.0)]
    assert normalize_claims(claims, SITE_DAILY, 900.0) == [900.0, 900.0]


def test_site_claims_only_draw_from_what_devices_left():
    """Çevrimdışı cihazın payı kirlilikle ikinci kez kaybedilemez."""
    claims = [
        LossClaim(SCOPE_DEVICE, "1", 900.0),
        LossClaim(SCOPE_SITE, None, 1800.0),  # tüm sahayı iddia eden bulgu
    ]
    values = normalize_claims(claims, SITE_DAILY, 900.0)
    assert values == [900.0, 900.0]  # saha iddiası kalan üretime indi
    assert sum(values) == pytest.approx(SITE_DAILY)


def test_modest_site_claims_pass_through_untouched():
    claims = [
        LossClaim(SCOPE_DEVICE, "1", 900.0),
        LossClaim(SCOPE_SITE, None, 180.0),
        LossClaim(SCOPE_SITE, None, 216.0),
    ]
    assert normalize_claims(claims, SITE_DAILY, 900.0) == [900.0, 180.0, 216.0]


def test_measured_shortfall_caps_site_claims():
    claims = [LossClaim(SCOPE_SITE, None, 180.0), LossClaim(SCOPE_SITE, None, 216.0)]
    values = normalize_claims(claims, SITE_DAILY, 900.0, measured_shortfall_kwh=100.0)
    assert sum(values) == pytest.approx(100.0)
    # oranlar korunur: 180/396 ve 216/396
    assert values[0] == pytest.approx(100.0 * 180.0 / 396.0)


def test_shortfall_fully_explained_by_a_device_zeroes_site_claims():
    """Ölçülen açığın tamamı çevrimdışı cihazsa kirlilik ayrıca fatura edilemez."""
    claims = [LossClaim(SCOPE_DEVICE, "1", 900.0), LossClaim(SCOPE_SITE, None, 180.0)]
    values = normalize_claims(claims, SITE_DAILY, 900.0, measured_shortfall_kwh=900.0)
    assert values == [900.0, 0.0]


def test_device_claims_are_not_capped_by_the_measured_shortfall():
    """Cihaz sinyali ikizden bağımsız: ikiz yanılıyorsa cihaz gerçekten durdu."""
    claims = [LossClaim(SCOPE_DEVICE, "1", 900.0)]
    assert normalize_claims(claims, SITE_DAILY, 900.0, measured_shortfall_kwh=10.0) == [900.0]


def test_negative_and_zero_inputs_are_safe():
    assert normalize_claims([LossClaim(SCOPE_SITE, None, -50.0)], SITE_DAILY, 900.0) == [0.0]
    # kapasitesi bilinmeyen saha fiyatlanamaz
    assert normalize_claims([LossClaim(SCOPE_SITE, None, 180.0)], 0.0, 0.0) == [0.0]


def test_missing_device_share_falls_back_to_site_ceiling():
    """Cihaz sayısı bilinmiyorsa tavan sahanın kendisidir, sıfır değil."""
    claims = [LossClaim(SCOPE_DEVICE, "1", 5000.0)]
    assert normalize_claims(claims, SITE_DAILY, 0.0) == [SITE_DAILY]


def test_total_recoverable_never_exceeds_the_measured_loss():
    """Katmanın varlık sebebi: rakam ölçülen kaybın katlarına çıkmasın."""
    findings = [
        finding(KIND_INV_OFFLINE, severity="critical", device="1"),
        finding(KIND_SOILING, deviation=-10.0),
        finding(KIND_SHADING, deviation=-12.0),
    ]
    measured = 1000.0
    insights = site_insights(context(devices=2, shortfall=measured), findings)

    raw = 900.0 + 180.0 + 216.0  # normalizasyon olmasa
    assert raw > measured
    assert sum(i.daily_loss_kwh for i in insights) == pytest.approx(measured)


# ------------------------------ öncelik ------------------------------


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (KIND_INV_OFFLINE, PRIORITY_IMMEDIATE),
        (KIND_INV_ERROR, PRIORITY_IMMEDIATE),
        (KIND_MICROCRACK, PRIORITY_IMMEDIATE),
        (KIND_INV_OVERHEAT, PRIORITY_MID),
        (KIND_SHADING, PRIORITY_MID),
        (KIND_HOUR_BIAS, PRIORITY_MID),
        (KIND_SOILING, PRIORITY_LONG),
        (KIND_CALIBRATION_SCALE, PRIORITY_LONG),
        (KIND_DEGRADATION, PRIORITY_LONG),
    ],
)
def test_base_priority_per_kind(kind, expected):
    assert playbook_for(kind).priority == expected


def test_large_critical_loss_escalates_to_immediate():
    book = playbook_for(KIND_SOILING)
    loss = SITE_DAILY * 0.18
    assert classify_priority(book, "critical", loss, SITE_DAILY) == PRIORITY_IMMEDIATE


def test_large_but_non_critical_loss_keeps_its_bucket():
    """Yalnız büyüklüğe bakmak her kirlilik bulgusunu 'Acil' yapar, kova ayrımı çöker."""
    book = playbook_for(KIND_SOILING)
    loss = SITE_DAILY * 0.14  # eşiğin çok üstünde ama şiddet 'warning'
    assert loss / SITE_DAILY > IMMEDIATE_LOSS_FRACTION
    assert classify_priority(book, "warning", loss, SITE_DAILY) == PRIORITY_LONG


def test_small_critical_loss_does_not_escalate():
    book = playbook_for(KIND_SOILING)
    loss = SITE_DAILY * 0.02
    assert classify_priority(book, "critical", loss, SITE_DAILY) == PRIORITY_LONG


def test_unrecoverable_loss_never_becomes_urgent():
    book = playbook_for(KIND_DEGRADATION)
    assert classify_priority(book, "critical", SITE_DAILY * 0.5, SITE_DAILY) == PRIORITY_LONG


def test_zero_capacity_site_cannot_escalate():
    book = playbook_for(KIND_SOILING)
    assert classify_priority(book, "critical", 0.0, 0.0) == PRIORITY_LONG


# ------------------------------ yıllıklandırma ------------------------------


def test_yearly_income_discounts_by_persistence():
    [soiling] = site_insights(context(), [finding(KIND_SOILING, deviation=-10.0)])
    expected_kwh = 180.0 * DAYS_PER_YEAR * PERSISTENCE[PRIORITY_LONG]
    assert soiling.priority == PRIORITY_LONG
    assert soiling.recoverable_kwh_year == pytest.approx(expected_kwh, rel=1e-4)
    assert soiling.recoverable_try_year == pytest.approx(expected_kwh * TARIFF, rel=1e-4)


def test_immediate_findings_are_annualised_in_full():
    [offline] = site_insights(
        context(), [finding(KIND_INV_OFFLINE, severity="critical", device="1")]
    )
    assert offline.priority == PRIORITY_IMMEDIATE
    assert PERSISTENCE[PRIORITY_IMMEDIATE] == 1.0
    assert offline.recoverable_kwh_year == pytest.approx(SITE_DAILY * DAYS_PER_YEAR, rel=1e-4)


def test_same_daily_loss_is_worth_more_when_it_persists():
    """Kalıcılık katsayısının tek görünür etkisi: aynı kayıp, farklı gelir."""
    shading = site_insights(context(), [finding(KIND_SHADING, deviation=-10.0)])[0]
    soiling = site_insights(context(), [finding(KIND_SOILING, deviation=-10.0)])[0]
    assert shading.daily_loss_kwh == soiling.daily_loss_kwh
    assert shading.recoverable_try_year > soiling.recoverable_try_year


def test_degradation_is_listed_but_priced_at_zero():
    """Yaşlanma geri kazanılamaz; gelire saymak rakamı şişirirdi."""
    [aging] = site_insights(context(), [finding(KIND_DEGRADATION, deviation=-5.0)])
    assert aging.daily_loss_kwh > 0.0
    assert aging.recoverable_try_year == 0.0
    assert aging.recoverable_pct == 0.0


def test_recoverable_pct_is_share_of_yearly_expectation():
    [soiling] = site_insights(context(), [finding(KIND_SOILING, deviation=-10.0)])
    reference = SITE_DAILY * DAYS_PER_YEAR
    assert soiling.recoverable_pct == pytest.approx(
        180.0 * DAYS_PER_YEAR * PERSISTENCE[PRIORITY_LONG] / reference * 100.0, rel=1e-3
    )


def test_twin_expectation_overrides_the_rule_of_thumb_denominator():
    rough = site_insights(context(), [finding(KIND_SOILING, deviation=-10.0)])[0]
    exact = site_insights(
        context(annual=SITE_DAILY * DAYS_PER_YEAR * 0.8),
        [finding(KIND_SOILING, deviation=-10.0)],
    )[0]
    # Aynı kayıp, daha küçük payda → daha yüksek oran
    assert exact.recoverable_pct > rough.recoverable_pct


def test_site_without_capacity_reports_no_money_instead_of_guessing():
    [soiling] = site_insights(context(capacity=None), [finding(KIND_SOILING, deviation=-10.0)])
    assert soiling.daily_loss_kwh == 0.0
    assert soiling.recoverable_try_year == 0.0


# ------------------------------ güven ------------------------------


def test_device_findings_are_trusted_regardless_of_model_error():
    """Üretici 'çevrimdışı' diyorsa ikizin nMAE'si konuyla ilgisiz."""
    assert confidence_for(SCOPE_DEVICE, None, 0.0) == CONFIDENCE_HIGH
    assert confidence_for(SCOPE_DEVICE, 25.0, 0.0) == CONFIDENCE_HIGH


def test_unscored_site_finding_is_marked_unverified():
    assert confidence_for(SCOPE_SITE, None, -10.0) == CONFIDENCE_UNKNOWN


@pytest.mark.parametrize(
    ("nmae", "deviation", "expected"),
    [
        (3.0, -10.0, CONFIDENCE_HIGH),
        (7.0, -10.0, CONFIDENCE_MEDIUM),
        (12.0, -20.0, CONFIDENCE_LOW),
    ],
)
def test_confidence_tracks_model_accuracy(nmae, deviation, expected):
    assert confidence_for(SCOPE_SITE, nmae, deviation) == expected


def test_finding_smaller_than_the_model_error_is_weak_evidence():
    """nMAE %3 iyi bir model, ama -%2 bulgu onun gürültüsünden ayırt edilemez."""
    assert confidence_for(SCOPE_SITE, 3.0, -2.0) == CONFIDENCE_LOW
    assert confidence_for(SCOPE_SITE, 12.0, -10.0) == CONFIDENCE_LOW


def test_confidence_reaches_the_insight():
    [soiling] = site_insights(context(nmae=3.0), [finding(KIND_SOILING, deviation=-10.0)])
    assert soiling.confidence == CONFIDENCE_HIGH
    assert soiling.confidence_label == "Yüksek güven"


# ------------------------------ skor tahtası köprüsü ------------------------------


def make_score(actual_kw: float, expected_kw: float):
    day = date(2026, 7, 28)
    pairs = [
        AccuracyPair(
            ts=datetime(2026, 7, 28, 9, 0, tzinfo=UTC).replace(minute=15 * (i % 4)),
            actual_kw=actual_kw,
            expected_kw=expected_kw,
        )
        for i in range(12)
    ]
    return score_day(URETIM, day, pairs, capacity_kw=333.0, model_version="twin-v1")


def test_shortfall_comes_from_the_accuracy_scoreboard():
    score = make_score(actual_kw=90.0, expected_kw=100.0)
    assert score is not None
    # 12 nokta × 0,25 sa × 10 kW = 30 kWh açık
    assert shortfall_from_score(score) == pytest.approx(30.0)


def test_overproduction_reports_no_shortfall_not_a_negative_one():
    score = make_score(actual_kw=110.0, expected_kw=100.0)
    assert shortfall_from_score(score) == 0.0


def test_missing_score_leaves_the_cap_unset():
    assert shortfall_from_score(None) is None


# ------------------------------ sıralama ve toplama ------------------------------


def test_insights_are_ordered_by_priority_then_money():
    findings = [
        finding(KIND_SOILING, deviation=-10.0),
        finding(KIND_INV_OFFLINE, severity="critical", device="1"),
        finding(KIND_SHADING, deviation=-12.0),
    ]
    insights = site_insights(context(devices=2), findings)
    assert [i.priority for i in insights] == [PRIORITY_IMMEDIATE, PRIORITY_MID, PRIORITY_LONG]
    assert insights[0].priority_chip == "crit"
    assert insights[0].priority_label == "Acil"


def test_equal_priority_puts_the_costliest_first():
    findings = [
        finding(KIND_SHADING, deviation=-4.0),
        finding(KIND_SHADING, deviation=-12.0),
    ]
    insights = site_insights(context(), findings)
    assert insights[0].recoverable_try_year > insights[1].recoverable_try_year


def test_portfolio_groups_by_site_and_totals_by_priority():
    contexts = [
        context(URETIM, capacity=400.0, devices=2),
        SiteContext(
            series_key=MEKANIK,
            name="Mekanik Fabrika",
            capacity_kwp=250.0,
            tariff_try_kwh=TARIFF,
            device_count=1,
        ),
    ]
    findings = [
        finding(KIND_INV_OFFLINE, site=URETIM, severity="critical", device="1"),
        finding(KIND_SOILING, site=URETIM, deviation=-10.0),
        finding(KIND_SOILING, site=MEKANIK, deviation=-6.0),
    ]
    portfolio = portfolio_insights(contexts, findings)

    assert portfolio.total_count == 3
    assert portfolio.count_by_priority[PRIORITY_IMMEDIATE] == 1
    assert portfolio.count_by_priority[PRIORITY_LONG] == 2
    assert portfolio.recoverable_try_year == pytest.approx(
        sum(i.recoverable_try_year for i in portfolio.insights), rel=1e-6
    )
    assert portfolio.try_by_priority[PRIORITY_IMMEDIATE] > 0.0
    assert {i.site_name for i in portfolio.insights} == {"Üretim Fabrikası", "Mekanik Fabrika"}


def test_normalization_does_not_leak_between_sites():
    """Bir sahanın ölçülen açığı diğer sahanın bulgusunu kısmamalı."""
    contexts = [
        context(URETIM, capacity=400.0, devices=1, shortfall=0.0),
        SiteContext(
            series_key=MEKANIK,
            name="Mekanik Fabrika",
            capacity_kwp=250.0,
            tariff_try_kwh=TARIFF,
            device_count=1,
        ),
    ]
    findings = [
        finding(KIND_SOILING, site=URETIM, deviation=-10.0),
        finding(KIND_SOILING, site=MEKANIK, deviation=-10.0),
    ]
    portfolio = portfolio_insights(contexts, findings)
    by_site = {i.site_key: i for i in portfolio.insights}
    assert by_site[URETIM].daily_loss_kwh == 0.0  # açığı yok → kurtarılacak şey de yok
    assert by_site[MEKANIK].daily_loss_kwh > 0.0


def test_finding_from_an_unknown_series_is_dropped_with_a_warning(caplog):
    with caplog.at_level("WARNING"):
        portfolio = portfolio_insights([context()], [finding(KIND_SOILING, site="yok-boyle")])
    assert portfolio.total_count == 0
    assert "yok-boyle" in caplog.text


def test_empty_portfolio_is_zero_not_an_error():
    portfolio = portfolio_insights([], [])
    assert portfolio.recoverable_try_year == 0.0
    assert portfolio.recoverable_pct == 0.0
    assert portfolio.top() == []


def test_portfolio_pct_uses_the_combined_yearly_expectation():
    contexts = [context(URETIM, capacity=400.0, devices=1)]
    portfolio = portfolio_insights(contexts, [finding(KIND_SOILING, deviation=-10.0)])
    reference = SITE_DAILY * DAYS_PER_YEAR
    assert portfolio.recoverable_pct == pytest.approx(
        portfolio.recoverable_kwh_year / reference * 100.0, rel=1e-3
    )


def test_top_limits_the_action_list():
    findings = [finding(KIND_SHADING, deviation=-float(d)) for d in range(2, 12)]
    portfolio = portfolio_insights([context()], findings)
    top = portfolio.top(3)
    assert len(top) == 3
    assert top == portfolio.insights[:3]


# ------------------------------ olay → bulgu köprüsü ------------------------------


def make_event(kind: str, **kwargs):
    defaults = {
        "id": uuid.uuid4(),
        "plant_id": uuid.uuid4(),
        "kind": kind,
        "severity": "warning",
        "deviation_pct": -10.0,
        "started_at": datetime(2026, 7, 29, 9, 0, tzinfo=UTC),
        "status": "open",
        "evidence": {},
    }
    return AnomalyEvent(**{**defaults, **kwargs})


def test_event_carries_its_device_and_identity_into_the_finding():
    event = make_event(KIND_INV_OFFLINE, severity="critical", evidence={"device_id": "2"})
    converted = finding_from_event(event, URETIM)
    assert converted.site_key == URETIM
    assert converted.device_id == "2"
    assert converted.severity == "critical"
    assert converted.event_id == event.id
    assert converted.started_at == event.started_at


def test_site_wide_event_has_no_device():
    assert finding_from_event(make_event(KIND_SOILING), URETIM).device_id is None


def test_device_zero_is_a_real_device_not_a_missing_one():
    """`if raw:` yazmak 0 numaralı cihazı sessizce santral bulgusuna çevirirdi."""
    assert device_id_of(make_event(KIND_INV_ERROR, evidence={"device_id": "0"})) == "0"
    assert device_id_of(make_event(KIND_INV_ERROR, evidence={"device_id": 0})) == "0"


def test_blank_and_absent_device_ids_are_treated_as_site_wide():
    assert device_id_of(make_event(KIND_SOILING, evidence={"device_id": ""})) is None
    assert device_id_of(make_event(KIND_SOILING, evidence={})) is None


def test_events_flow_end_to_end_into_a_priced_plan():
    events = [
        make_event(KIND_INV_OFFLINE, severity="critical", evidence={"device_id": "1"}),
        make_event(KIND_SOILING, deviation_pct=-9.0),
    ]
    findings = [finding_from_event(e, URETIM) for e in events]
    portfolio = portfolio_insights([context(devices=2)], findings)
    assert portfolio.count_by_priority[PRIORITY_IMMEDIATE] == 1
    assert portfolio.count_by_priority[PRIORITY_LONG] == 1
    assert portfolio.recoverable_try_year > 0.0


# ------------------------------ playbook sözleşmesi ------------------------------


def test_device_scoped_playbooks_use_a_device_loss_model():
    """Kapsam ile kayıp modeli tutarsız olursa cihaz kaybı saha kaybı gibi fiyatlanır."""
    for kind, book in ((k, playbook_for(k)) for k in (KIND_INV_OFFLINE, KIND_INV_ERROR)):
        assert book.scope == SCOPE_DEVICE, kind
        assert book.loss_model.startswith("device"), kind


def test_custom_playbook_is_honoured_end_to_end():
    """Yeni bir bulgu türü eklemek sözlüğe bir satır yazmaktan fazlası olmasın."""
    book = Playbook(
        title="Deneme bulgusu",
        recommendation="Bir şey yapın.",
        scope=SCOPE_SITE,
        loss_model="site_deviation",
        priority=PRIORITY_MID,
    )
    assert daily_loss_kwh(book, -10.0, SITE_DAILY, 0.0) == pytest.approx(180.0)
    assert classify_priority(book, "warning", 180.0, SITE_DAILY) == PRIORITY_MID
