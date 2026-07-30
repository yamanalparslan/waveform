"""Tahmin doğruluğu skor tahtası: metrik doğruluğu ve kenar durumlar."""

from datetime import UTC, date, datetime, timedelta

import pytest

from luminmind.analytics.accuracy import (
    MIN_SCORED_SAMPLES,
    AccuracyPair,
    align_series,
    persistence_reference,
    score_day,
)

DAY = date(2026, 7, 20)
START = datetime(2026, 7, 20, tzinfo=UTC)
CAPACITY = 1000.0


def pairs_with_constant_error(error_kw: float, count: int = 48) -> list[AccuracyPair]:
    return [
        AccuracyPair(
            ts=START + timedelta(minutes=15 * i),
            actual_kw=500.0,
            expected_kw=500.0 + error_kw,
        )
        for i in range(count)
    ]


def score(pairs, **kwargs):
    return score_day(
        plant_id="p1",
        day=DAY,
        pairs=pairs,
        capacity_kw=CAPACITY,
        model_version="physical-v2",
        **kwargs,
    )


def test_perfect_forecast_scores_zero_error():
    result = score(pairs_with_constant_error(0.0))
    assert result is not None
    assert result.mae_kw == 0.0
    assert result.rmse_kw == 0.0
    assert result.nmbe_pct == 0.0
    assert result.energy_error_pct == 0.0
    assert not result.is_biased


def test_constant_overprediction_is_reported_as_positive_bias():
    result = score(pairs_with_constant_error(50.0))
    assert result is not None
    assert result.mbe_kw == pytest.approx(50.0)
    assert result.nmbe_pct == pytest.approx(5.0)  # kapasitenin %5'i
    assert result.nmae_pct == pytest.approx(5.0)
    assert result.is_biased  # sistematik → kalibrasyon tetiklenmeli
    assert result.energy_error_pct == pytest.approx(10.0)  # 550 vs 500 kWh


def test_underprediction_is_negative_bias():
    result = score(pairs_with_constant_error(-30.0))
    assert result is not None
    assert result.nmbe_pct == pytest.approx(-3.0)
    assert result.energy_error_pct < 0


def test_normalisation_makes_plants_comparable():
    """Aynı oransal hata, farklı ölçekteki tesislerde aynı nMAE vermeli."""
    small = score_day(
        plant_id="small",
        day=DAY,
        pairs=[
            AccuracyPair(ts=START + timedelta(minutes=15 * i), actual_kw=50.0, expected_kw=55.0)
            for i in range(48)
        ],
        capacity_kw=100.0,
        model_version="v",
    )
    large = score_day(
        plant_id="large",
        day=DAY,
        pairs=[
            AccuracyPair(
                ts=START + timedelta(minutes=15 * i), actual_kw=2500.0, expected_kw=2750.0
            )
            for i in range(48)
        ],
        capacity_kw=5000.0,
        model_version="v",
    )
    assert small is not None and large is not None
    assert small.nmae_pct == pytest.approx(large.nmae_pct)


def test_skill_positive_when_model_beats_persistence():
    pairs = [
        AccuracyPair(
            ts=START + timedelta(minutes=15 * i),
            actual_kw=500.0,
            expected_kw=510.0,  # model 10 kW şaşıyor
            reference_kw=400.0,  # persistence 100 kW şaşıyor
        )
        for i in range(48)
    ]
    result = score(pairs)
    assert result is not None
    assert result.skill_vs_reference is not None and result.skill_vs_reference > 0.85


def test_skill_negative_when_model_loses_to_persistence():
    pairs = [
        AccuracyPair(
            ts=START + timedelta(minutes=15 * i),
            actual_kw=500.0,
            expected_kw=300.0,
            reference_kw=490.0,
        )
        for i in range(48)
    ]
    result = score(pairs)
    assert result is not None
    assert result.skill_vs_reference is not None and result.skill_vs_reference < 0


def test_band_coverage_measures_calibration_of_uncertainty():
    pairs = []
    for i in range(48):
        actual = 500.0 if i < 40 else 200.0  # 8 nokta bandın dışında
        pairs.append(
            AccuracyPair(
                ts=START + timedelta(minutes=15 * i),
                actual_kw=actual,
                expected_kw=500.0,
                p10_kw=450.0,
                p90_kw=550.0,
            )
        )
    result = score(pairs)
    assert result is not None
    assert result.band_coverage_pct == pytest.approx(40 / 48 * 100.0, abs=0.1)


def test_too_few_samples_returns_none():
    assert score(pairs_with_constant_error(0.0, count=MIN_SCORED_SAMPLES - 1)) is None


def test_zero_capacity_returns_none():
    assert (
        score_day(
            plant_id="p1",
            day=DAY,
            pairs=pairs_with_constant_error(0.0),
            capacity_kw=0.0,
            model_version="v",
        )
        is None
    )


def test_non_finite_samples_are_dropped():
    pairs = pairs_with_constant_error(0.0) + [
        AccuracyPair(ts=START + timedelta(days=1), actual_kw=float("nan"), expected_kw=500.0)
    ]
    result = score(pairs)
    assert result is not None
    assert result.sample_count == 48


def test_align_series_uses_shared_timestamps_only():
    ts1, ts2, ts3 = (START + timedelta(hours=h) for h in (9, 10, 11))
    pairs = align_series(
        actual={ts1: 100.0, ts2: 200.0, ts3: 300.0},
        expected={ts1: 110.0, ts2: 190.0},
        band={ts1: (90.0, 130.0)},
    )
    assert [p.ts for p in pairs] == [ts1, ts2]
    assert pairs[0].p10_kw == 90.0 and pairs[0].p90_kw == 130.0
    assert pairs[1].p10_kw is None


def test_persistence_reference_shifts_previous_day():
    yesterday = {START - timedelta(days=1) + timedelta(hours=10): 420.0}
    shifted = persistence_reference(yesterday)
    assert shifted == {START + timedelta(hours=10): 420.0}
