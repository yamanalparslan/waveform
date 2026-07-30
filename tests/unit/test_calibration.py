"""Dijital ikiz kalibrasyonu: öğrenme, sınırlar ve arıza yutmama garantileri."""

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from luminmind.twin.calibration import (
    CalibrationConfig,
    CalibrationSample,
    CalibrationState,
    fit_calibration,
)

START = datetime(2026, 7, 1, tzinfo=UTC)
CAPACITY = 1000.0


def samples(ratio, days: int = 3, expected_kw: float = 600.0) -> list[CalibrationSample]:
    """Gündüz saatlerinde (05–17 UTC) 15 dk'lık gerçek/beklenen çiftleri."""
    out: list[CalibrationSample] = []
    for day in range(days):
        base = START + timedelta(days=day)
        for slot in range(48):
            ts = base + timedelta(hours=5, minutes=15 * slot)
            factor = ratio(ts) if callable(ratio) else ratio
            out.append(
                CalibrationSample(
                    ts=ts, actual_kw=expected_kw * factor, expected_kw=expected_kw
                )
            )
    return out


def test_persistent_shortfall_is_learned_as_scale():
    state = fit_calibration("p1", samples(0.90), CAPACITY)
    # Öğrenme oranı 0,35 → tek fit'te hedefin bir kısmı alınır (kararlılık için)
    assert 0.90 < state.scale < 1.0
    assert state.sample_count > 0
    assert state.quality["mape_before_pct"] == pytest.approx(10.0, abs=0.5)
    assert state.quality["mape_after_pct"] < state.quality["mape_before_pct"]


def test_repeated_fits_converge_towards_truth():
    """Kapalı döngü: her fit'te beklenen seri mevcut kalibrasyonla üretilmiş olur.

    Gerçek tesis %90 üretiyorsa, kalibrasyon 0,90'a yaklaştıkça gözlenen *kalan*
    oran 1,0'a yaklaşır ve durum orada durur — sınırsız aşağı yürümez.
    """
    state: CalibrationState | None = None
    for _ in range(15):
        applied = state.scale if state else 1.0
        # Beklenen seri `applied` ile ölçeklendiği için gözlenen oran budur
        state = fit_calibration("p1", samples(0.90 / applied), CAPACITY, previous=state)
    assert state is not None
    assert state.scale == pytest.approx(0.90, abs=0.01)


def test_incremental_fit_does_not_double_apply():
    """Beklenen seri zaten kalibre olduğundan kalan oran 1,0 ise durum değişmez."""
    previous = CalibrationState(plant_id="p1", scale=0.90)
    state = fit_calibration("p1", samples(1.0), CAPACITY, previous=previous)
    assert state.scale == pytest.approx(0.90, abs=1e-4)


def test_scale_is_clamped_so_faults_are_not_absorbed():
    """%50 kayıp bir arızadır; model kendini oraya uydurmamalı."""
    state = fit_calibration("p1", samples(0.50), CAPACITY)
    limits = CalibrationConfig().scale_limits
    assert state.scale >= limits[0]
    # Tek fit'te bile taban sınırın altına inemez
    assert state.scale > 0.60


def test_hour_bias_captures_time_of_day_pattern():
    def ratio(ts: datetime) -> float:
        return 0.85 if ts.hour in (6, 7) else 1.0

    state = fit_calibration("p1", samples(ratio, days=5), CAPACITY)
    assert 6 in state.hour_bias and 7 in state.hour_bias
    assert state.hour_bias[6] < 0.99
    assert state.hour_bias.get(12, 1.0) == pytest.approx(1.0, abs=0.02)


def test_hour_bias_is_clamped():
    def ratio(ts: datetime) -> float:
        return 0.55 if ts.hour == 6 else 1.0

    config = CalibrationConfig(learning_rate=1.0)  # harmanlamayı kapat, sınırı gör
    state = fit_calibration("p1", samples(ratio, days=5), CAPACITY, config=config)
    low, _ = config.hour_bias_limits
    assert state.hour_bias[6] == pytest.approx(low)


def test_extreme_hourly_loss_is_rejected_not_learned():
    """Bir saatte %80 kayıp model hatası değil arızadır; öğrenilmemeli."""

    def ratio(ts: datetime) -> float:
        return 0.20 if ts.hour == 6 else 1.0

    state = fit_calibration("p1", samples(ratio, days=5), CAPACITY)
    assert 6 not in state.hour_bias  # ham oran ön elemesinde düştü


def test_previous_hour_bias_survives_a_data_poor_fit():
    """Veri azalan bir saatin öğrenilmiş biası silinmemeli."""
    previous = CalibrationState(plant_id="p1", scale=1.0, hour_bias={6: 0.88})

    def ratio(ts: datetime) -> float:
        return 1.0

    only_midday = [s for s in samples(ratio, days=5) if s.ts.hour >= 10]
    state = fit_calibration("p1", only_midday, CAPACITY, previous=previous)
    assert state.hour_bias[6] == pytest.approx(0.88)


def test_low_irradiance_points_are_excluded():
    """Kapasitenin %15'i altındaki noktalar oranı bozar; fit'e girmemeli."""
    noisy = [
        CalibrationSample(ts=START + timedelta(minutes=15 * i), actual_kw=5.0, expected_kw=1.0)
        for i in range(200)
    ]
    state = fit_calibration("p1", noisy, CAPACITY)
    assert state.is_identity  # hiçbiri kullanılabilir değil → durum değişmedi


def test_insufficient_data_keeps_previous_state():
    previous = CalibrationState(plant_id="p1", scale=0.93, sample_count=500)
    state = fit_calibration("p1", samples(0.80, days=1)[:10], CAPACITY, previous=previous)
    assert state is previous


def test_outliers_do_not_move_the_fit():
    good = samples(0.95, days=3)
    spikes = [
        CalibrationSample(ts=s.ts + timedelta(days=10), actual_kw=0.0, expected_kw=600.0)
        for s in good[:20]
    ]
    with_outliers = fit_calibration("p1", good + spikes, CAPACITY)
    clean = fit_calibration("p1", good, CAPACITY)
    assert with_outliers.scale == pytest.approx(clean.scale, abs=0.01)


def test_apply_scales_series_by_hour():
    index = pd.date_range("2026-07-20 05:00", periods=4, freq="1h", tz="UTC")
    series = pd.Series([100.0, 200.0, 300.0, 400.0], index=index)
    state = CalibrationState(plant_id="p1", scale=0.9, hour_bias={6: 0.5})
    result = state.apply(series)
    assert result.iloc[0] == pytest.approx(90.0)  # 05:00 → yalnızca ölçek
    assert result.iloc[1] == pytest.approx(200.0 * 0.9 * 0.5)  # 06:00 → ölçek × bias
    assert result.iloc[3] == pytest.approx(360.0)


def test_identity_state_is_a_no_op():
    index = pd.date_range("2026-07-20", periods=3, freq="1h", tz="UTC")
    series = pd.Series([1.0, 2.0, 3.0], index=index)
    assert CalibrationState(plant_id="p1").apply(series).equals(series)


def test_json_round_trip_preserves_state():
    original = fit_calibration("p1", samples(0.92, days=4), CAPACITY)
    restored = CalibrationState.from_json("p1", original.to_json())
    assert restored.scale == original.scale
    assert restored.hour_bias == original.hour_bias
    assert restored.fitted_at == original.fitted_at
    assert restored.sample_count == original.sample_count


def test_from_json_handles_missing_payload():
    assert CalibrationState.from_json("p1", None).is_identity
