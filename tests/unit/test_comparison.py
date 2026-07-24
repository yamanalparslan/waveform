"""Sapma serisi + sınıflandırıcı senaryoları: sağlıklı / kirlilik / gölge / mikro çatlak."""

from datetime import UTC, datetime, timedelta

from luminmind.analytics.classifiers import (
    KIND_MICROCRACK,
    KIND_SHADING,
    KIND_SOILING,
    classify_window,
)
from luminmind.analytics.comparison import (
    DeviationSample,
    build_deviation_series,
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
    series = build_deviation_series(actual, expected, min_expected_kw=10.0)
    assert [s.ts for s in series] == [ts1]
    assert series[0].deviation_pct < 0


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


def test_plant_actual_buckets_devices_at_5m():
    """Farklı saniyelerde gelen cihaz ölçümleri aynı 5 dk bucket'a düşer ve toplanır.
    Bir cihazın bucket içindeki iki değeri ortalama alınır (spike tolerans)."""
    b0 = DAY0 + timedelta(hours=10)         # 10:00:00 (bucket sınırı)
    b1 = DAY0 + timedelta(hours=10, minutes=5)  # 10:05:00 (sonraki bucket)
    samples = [
        # 10:00 bucket'ı — 3 fabrika cihazı farklı saniyelerde
        RawSample(ts=b0 + timedelta(seconds=15),
                  plant_id="p1", inverter_id="mekanik-1",
                  fields={"ac_power_kw": 100.0}),
        RawSample(ts=b0 + timedelta(seconds=45),
                  plant_id="p1", inverter_id="uretim-1",
                  fields={"ac_power_kw": 90.0}),
        RawSample(ts=b0 + timedelta(minutes=2),
                  plant_id="p1", inverter_id="uretim-2",
                  fields={"ac_power_kw": 110.0}),
        # Aynı cihazın aynı bucket'ta ikinci ölçümü — ortalama alınmalı
        RawSample(ts=b0 + timedelta(minutes=4, seconds=59),
                  plant_id="p1", inverter_id="uretim-2",
                  fields={"ac_power_kw": 130.0}),
        # 10:05 bucket'ı — farklı bir noktaya düşmeli
        RawSample(ts=b1 + timedelta(seconds=10),
                  plant_id="p1", inverter_id="mekanik-1",
                  fields={"ac_power_kw": 120.0}),
    ]
    actual = plant_actual_from_samples(samples)
    # 10:00 bucket: 100 + 90 + (110+130)/2 = 100 + 90 + 120 = 310
    assert actual["p1"][b0] == 310.0
    # 10:05 bucket: yalnız mekanik-1 = 120
    assert actual["p1"][b1] == 120.0


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
