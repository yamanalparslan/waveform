from datetime import UTC, datetime, timedelta

from luminmind.core.aggregate import RawSample, aggregate_daily, aggregate_hourly

H0 = datetime(2026, 7, 20, 7, 0, tzinfo=UTC)


def sample(minute_offset: int, ac: float, energy: float, dev: str = "inv-01") -> RawSample:
    return RawSample(
        ts=H0 + timedelta(minutes=minute_offset),
        plant_id="p1",
        inverter_id=dev,
        fields={"ac_power_kw": ac, "energy_total_kwh": energy},
    )


def test_hourly_mean_max_and_energy_delta():
    samples = [
        sample(0, 100.0, 1000.0),
        sample(15, 120.0, 1030.0),
        sample(30, 140.0, 1065.0),
        sample(45, 120.0, 1095.0),
    ]
    [agg] = aggregate_hourly(samples)
    assert agg.hour_start == H0
    assert agg.sample_count == 4
    assert agg.ac_power_kw_mean == 120.0
    assert agg.ac_power_kw_max == 140.0
    assert agg.energy_kwh == 95.0  # 1095 - 1000


def test_hourly_groups_by_device_and_hour():
    samples = [
        sample(0, 100.0, 1000.0, dev="inv-01"),
        sample(0, 90.0, 900.0, dev="inv-02"),
        sample(60, 80.0, 1050.0, dev="inv-01"),
    ]
    aggs = aggregate_hourly(samples)
    assert len(aggs) == 3
    keys = {(a.hour_start, a.inverter_id) for a in aggs}
    assert keys == {
        (H0, "inv-01"),
        (H0, "inv-02"),
        (H0 + timedelta(hours=1), "inv-01"),
    }


def test_hourly_single_sample_has_no_energy_delta():
    [agg] = aggregate_hourly([sample(0, 100.0, 1000.0)])
    assert agg.energy_kwh is None
    assert agg.ac_power_kw_mean == 100.0


def test_hourly_is_deterministic_idempotent():
    samples = [sample(0, 100.0, 1000.0), sample(15, 120.0, 1030.0)]
    assert aggregate_hourly(samples) == aggregate_hourly(list(reversed(samples)))


def test_daily_sums_energy_and_takes_peak_per_plant():
    samples = [
        sample(0, 100.0, 1000.0, dev="inv-01"),
        sample(15, 140.0, 1030.0, dev="inv-01"),
        sample(60, 90.0, 1060.0, dev="inv-01"),
        sample(75, 70.0, 1080.0, dev="inv-01"),
    ]
    hourly = aggregate_hourly(samples)
    [daily] = aggregate_daily(hourly)
    assert daily.plant_id == "p1"
    assert daily.day_start == H0.replace(hour=0)
    assert daily.energy_kwh == 50.0  # (1030-1000) + (1080-1060)
    assert daily.peak_ac_power_kw == 140.0


def test_daily_counter_reset_does_not_produce_negative_energy():
    """Tescom `gunluk_uretim_kwh` gece yarısı sıfırlanır.

    Düz fark alınırsa reset saatinde negatif enerji çıkar ve günlük toplamı
    aşağı çeker. Reset saatindeki üretim, sıfırlama sonrası birikmiş değerdir.
    """
    base = datetime(2026, 7, 21, 23, 45, tzinfo=UTC)
    samples = [
        RawSample(ts=base, plant_id="p", inverter_id="1",
                  fields={"energy_total_kwh": 18.4, "ac_power_kw": 0.0}),
        RawSample(ts=base + timedelta(minutes=10), plant_id="p", inverter_id="1",
                  fields={"energy_total_kwh": 18.4, "ac_power_kw": 0.0}),
    ]
    # Aynı saat içinde reset: 18.4 → 0.0 → 0.6
    reset_hour = datetime(2026, 7, 22, 0, 0, tzinfo=UTC)
    samples += [
        RawSample(ts=reset_hour, plant_id="p", inverter_id="1",
                  fields={"energy_total_kwh": 0.0, "ac_power_kw": 0.0}),
        RawSample(ts=reset_hour + timedelta(minutes=30), plant_id="p", inverter_id="1",
                  fields={"energy_total_kwh": 0.6, "ac_power_kw": 1.2}),
    ]
    hourly = {a.hour_start: a for a in aggregate_hourly(samples)}
    assert hourly[reset_hour].energy_kwh == 0.6  # negatif değil
    assert all(a.energy_kwh >= 0 for a in hourly.values())


def test_cumulative_counter_behaviour_is_unchanged():
    """Kümülatif sayaçta (Huawei/SMA) reset dalı hiç çalışmamalı."""
    base = datetime(2026, 7, 21, 9, 0, tzinfo=UTC)
    samples = [
        RawSample(ts=base, plant_id="p", inverter_id="1",
                  fields={"energy_total_kwh": 1000.0}),
        RawSample(ts=base + timedelta(minutes=45), plant_id="p", inverter_id="1",
                  fields={"energy_total_kwh": 1042.5}),
    ]
    [hourly] = aggregate_hourly(samples)
    assert hourly.energy_kwh == 42.5
