"""Sapma serisi + sınıflandırıcı senaryoları: sağlıklı / kirlilik / gölge / mikro çatlak."""

from datetime import UTC, datetime, timedelta

import pytest

from luminmind.analytics.classifiers import (
    KIND_MICROCRACK,
    KIND_SHADING,
    KIND_SOILING,
    classify_window,
)
from luminmind.analytics.comparison import (
    FALLBACK_MIN_EXPECTED_KW,
    DeviationSample,
    build_deviation_series,
    min_expected_kw,
    plant_actual_from_samples,
)
from luminmind.core.aggregate import RawSample

DAY0 = datetime(2026, 7, 19, tzinfo=UTC)


def make_day(
    day_start: datetime,
    loss_fn,
    expected_kw: float = 800.0,
) -> list[DeviationSample]:
    """05:00–16:45 UTC arası 15 dk'lık örnekler; loss_fn(ts) → çarpan (1.0 = sağlıklı)."""
    samples = []
    ts = day_start + timedelta(hours=5)
    while ts < day_start + timedelta(hours=17):
        samples.append(
            DeviationSample(ts=ts, actual_kw=expected_kw * loss_fn(ts), expected_kw=expected_kw)
        )
        ts += timedelta(minutes=15)
    return samples


def test_build_deviation_series_aligns_and_filters_night():
    ts1 = DAY0 + timedelta(hours=10)
    ts2 = DAY0 + timedelta(hours=22)  # beklenen ~0 → filtrelenmeli
    ts3 = DAY0 + timedelta(hours=11)  # yalnızca actual'da var → hizalanamaz
    actual = {ts1: 700.0, ts2: 0.0, ts3: 500.0}
    expected = {ts1: 750.0, ts2: 0.5}
    series = build_deviation_series(actual, expected, min_expected_kw_threshold=10.0)
    assert [s.ts for s in series] == [ts1]
    assert series[0].deviation_pct < 0


def test_min_expected_kw_scales_with_capacity():
    # Aynı kod, farklı ölçek: eşik kapasitenin sabit bir oranı olmalı
    assert min_expected_kw(100.0) == pytest.approx(5.0)
    assert min_expected_kw(5000.0) == pytest.approx(250.0)
    # Kapasite bilinmiyorsa sabit tabana düşer
    assert min_expected_kw(None) == FALLBACK_MIN_EXPECTED_KW
    assert min_expected_kw(0.0) == FALLBACK_MIN_EXPECTED_KW


def test_band_absorbs_deviation_within_forecast_uncertainty():
    ts = DAY0 + timedelta(hours=10)
    inside = DeviationSample(
        ts=ts, actual_kw=700.0, expected_kw=800.0, expected_p10_kw=650.0, expected_p90_kw=900.0
    )
    assert inside.within_band
    assert inside.deviation_pct == pytest.approx(-12.5)
    assert inside.excess_deviation_pct == 0.0  # sapma tamamen hava belirsizliği

    outside = DeviationSample(
        ts=ts, actual_kw=600.0, expected_kw=800.0, expected_p10_kw=650.0, expected_p90_kw=900.0
    )
    assert not outside.within_band
    # Yalnızca bandın altına taşan 50 kW raporlanır
    assert outside.excess_deviation_pct == pytest.approx(-6.25)


def test_excess_deviation_falls_back_to_raw_without_band():
    ts = DAY0 + timedelta(hours=10)
    sample = DeviationSample(ts=ts, actual_kw=700.0, expected_kw=800.0)
    assert sample.excess_deviation_pct == sample.deviation_pct


def test_mostly_in_band_window_is_healthy():
    # %12 kayıp ama band geniş → arıza değil, tahmin belirsizliği
    samples = [
        DeviationSample(
            ts=s.ts,
            actual_kw=s.actual_kw,
            expected_kw=s.expected_kw,
            expected_p10_kw=s.expected_kw * 0.80,
            expected_p90_kw=s.expected_kw * 1.10,
        )
        for s in make_day(DAY0, lambda ts: 0.88)
    ]
    assert classify_window(samples) is None
    # Aynı kayıp, dar band → arıza olarak raporlanır
    narrow = [
        DeviationSample(
            ts=s.ts,
            actual_kw=s.actual_kw,
            expected_kw=s.expected_kw,
            expected_p10_kw=s.expected_kw * 0.98,
            expected_p90_kw=s.expected_kw * 1.02,
        )
        for s in make_day(DAY0, lambda ts: 0.88)
    ]
    finding = classify_window(narrow)
    assert finding is not None and finding.kind == KIND_SOILING


def test_plant_actual_sums_devices():
    ts = DAY0 + timedelta(hours=10)
    samples = [
        RawSample(ts=ts, plant_id="p1", inverter_id="i1", fields={"ac_power_kw": 200.0}),
        RawSample(ts=ts, plant_id="p1", inverter_id="i2", fields={"ac_power_kw": 210.0}),
        RawSample(ts=ts, plant_id="p1", inverter_id="i3", fields={"temp_c": 40.0}),  # AC yok
        RawSample(ts=ts, plant_id="p2", inverter_id="i1", fields={"ac_power_kw": 50.0}),
    ]
    actual = plant_actual_from_samples(samples)
    assert actual["p1"][ts] == 410.0
    assert actual["p2"][ts] == 50.0


def test_healthy_plant_yields_no_finding():
    samples = make_day(DAY0, lambda ts: 0.99)
    assert classify_window(samples) is None


def test_uniform_loss_classified_as_soiling():
    samples = make_day(DAY0, lambda ts: 0.90)  # tüm gün %10 üniform kayıp
    finding = classify_window(samples)
    assert finding is not None and finding.kind == KIND_SOILING
    assert finding.deviation_pct == -10.0
    assert finding.severity == "warning"


def test_recurring_morning_dip_classified_as_shading():
    def loss(ts: datetime) -> float:
        return 0.80 if 7 <= ts.hour <= 9 else 0.99  # her sabah %20 çukur

    samples = make_day(DAY0, loss) + make_day(DAY0 + timedelta(days=1), loss)
    finding = classify_window(samples)
    assert finding is not None and finding.kind == KIND_SHADING
    assert finding.evidence["band_hours_utc"] == [7, 8, 9]
    assert len(finding.evidence["recurring_days"]) == 2
    assert finding.severity == "critical"  # %20 kayıp


def test_single_day_dip_not_shading():
    def loss(ts: datetime) -> float:
        return 0.80 if 7 <= ts.hour <= 9 else 0.99

    samples = make_day(DAY0, loss)  # tek gün → tekrar şartı sağlanmaz
    finding = classify_window(samples)
    assert finding is None or finding.kind != KIND_SHADING


def test_sudden_persistent_drop_classified_as_microcrack():
    step_at = DAY0 + timedelta(hours=11)

    def loss(ts: datetime) -> float:
        return 0.99 if ts < step_at else 0.85  # öğlen ani %14 basamak

    finding = classify_window(make_day(DAY0, loss))
    assert finding is not None and finding.kind == KIND_MICROCRACK
    assert finding.started_at == step_at
    assert finding.evidence["step_delta_pct"] <= -8.0
    assert finding.evidence["after_median_pct"] <= -14.0


def test_too_few_samples_returns_none():
    samples = make_day(DAY0, lambda ts: 0.5)[:8]
    assert classify_window(samples) is None


# ------------------------------ Izgaraya oturtma ------------------------------


def test_actual_series_is_snapped_to_the_analysis_grid():
    """Üretici damgaları rastgele anlarda gelir; ikiz 15 dk ızgarada üretir.

    Ham damgalarla hizalama yapılırsa hiçbir nokta eşleşmez ve anomali motoru
    sonsuza kadar "sağlıklı" der. Bu test o sessiz kırılmayı engelliyor.
    """
    base = DAY0 + timedelta(hours=6, minutes=59, seconds=3)
    samples = [
        RawSample(ts=base, plant_id="p1", inverter_id="1", fields={"ac_power_kw": 100.0}),
        RawSample(ts=base, plant_id="p1", inverter_id="2", fields={"ac_power_kw": 80.0}),
    ]
    actual = plant_actual_from_samples(samples)
    slot = DAY0 + timedelta(hours=6, minutes=45)
    assert list(actual["p1"]) == [slot]
    assert actual["p1"][slot] == 180.0  # iki cihaz toplanır

    # Izgaraya oturduğu için ikizle hizalanır
    expected = {slot: 200.0}
    [deviation] = build_deviation_series(actual["p1"], expected, min_expected_kw_threshold=10.0)
    assert deviation.deviation_pct == pytest.approx(-10.0)


def test_multiple_samples_of_one_device_are_averaged_not_summed():
    """5 dk çekimde bir aralıkta ~3 örnek var; toplamak gücü üçe katlardı."""
    slot = DAY0 + timedelta(hours=10)
    samples = [
        RawSample(
            ts=slot + timedelta(minutes=m, seconds=7),
            plant_id="p1",
            inverter_id="1",
            fields={"ac_power_kw": power},
        )
        for m, power in ((0, 90.0), (5, 100.0), (10, 110.0))
    ]
    actual = plant_actual_from_samples(samples)
    assert actual["p1"][slot] == pytest.approx(100.0)  # ortalama, 300 değil


def test_devices_averaged_then_summed():
    slot = DAY0 + timedelta(hours=10)
    samples = []
    for device, powers in (("1", (90.0, 110.0)), ("2", (40.0, 60.0))):
        samples += [
            RawSample(
                ts=slot + timedelta(minutes=m),
                plant_id="p1",
                inverter_id=device,
                fields={"ac_power_kw": p},
            )
            for m, p in zip((0, 5), powers, strict=True)
        ]
    actual = plant_actual_from_samples(samples)
    assert actual["p1"][slot] == pytest.approx(150.0)  # 100 + 50


def test_grid_snapping_keeps_separate_slots_separate():
    samples = [
        RawSample(
            ts=DAY0 + timedelta(hours=10, minutes=m, seconds=41),
            plant_id="p1",
            inverter_id="1",
            fields={"ac_power_kw": 100.0 + m},
        )
        for m in (2, 17, 33, 48)
    ]
    actual = plant_actual_from_samples(samples)
    assert [ts.minute for ts in actual["p1"]] == [0, 15, 30, 45]


def test_floor_to_grid_handles_boundaries():
    from luminmind.analytics.comparison import floor_to_grid

    assert floor_to_grid(DAY0 + timedelta(hours=10)).minute == 0
    assert floor_to_grid(DAY0 + timedelta(hours=10, minutes=14, seconds=59)).minute == 0
    assert floor_to_grid(DAY0 + timedelta(hours=10, minutes=15)).minute == 15
    # Mikrosaniye artığı da temizlenir, yoksa eşleşme yine kaçar
    assert floor_to_grid(DAY0 + timedelta(hours=10, microseconds=123)).microsecond == 0
