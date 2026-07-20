import pytest

from luminmind.bess.calibration import fit_rc_params, parse_cycle_csv
from luminmind.bess.models import CellParams
from luminmind.bess.soh import (
    capacity_soh,
    estimate_capacity_ah,
    estimate_r0_from_step,
    resistance_soh,
)
from luminmind.bess.synthetic import simulate_cell, step_profile

TRUE_CELL = CellParams(r0_ohm=0.025, r1_ohm=0.012, c1_farad=1500.0)


def make_cycle_csv(dt_s: float = 1.0) -> str:
    """Bilinen parametrelerle sentetik puls-deşarj CSV'si üretir (CSV sözleşme formatında)."""
    current = step_profile(
        [(300.0, 3.0), (300.0, 0.0), (300.0, 2.0), (300.0, 0.0), (300.0, 4.0)], dt_s=dt_s
    )
    sim = simulate_cell(TRUE_CELL, current, dt_s=dt_s, soc0=0.9)
    lines = ["t_s,voltage_v,current_a,temp_c"]
    for k in range(len(current)):
        lines.append(f"{k * dt_s},{sim.terminal_voltage_v[k]:.6f},{current[k]:.3f},25.0")
    return "\n".join(lines)


def test_parse_cycle_csv_roundtrip():
    cycle = parse_cycle_csv(make_cycle_csv())
    assert cycle.dt_s == 1.0
    assert len(cycle.voltage_v) == 1500
    assert cycle.current_a[0] == 3.0
    assert cycle.temp_c[0] == 25.0


def test_parse_rejects_missing_columns():
    with pytest.raises(ValueError, match="missing required columns"):
        parse_cycle_csv("t_s,voltage_v\n0,3.7\n1,3.7\n2,3.7")


def test_parse_rejects_non_uniform_sampling():
    csv = "t_s,voltage_v,current_a,temp_c\n0,3.7,1,25\n1,3.69,1,25\n5,3.68,1,25"
    with pytest.raises(ValueError, match="uniformly sampled"):
        parse_cycle_csv(csv)


def test_fit_recovers_true_rc_params():
    """Bozuk başlangıç tahmininden gerçek R0/R1/C1'i %15 tolerans içinde bulmalı."""
    cycle = parse_cycle_csv(make_cycle_csv())
    base = CellParams()  # OCV + kapasite bilinen, RC parametreleri fit edilecek
    fitted = fit_rc_params(
        cycle,
        base,
        soc0=0.9,
        initial_guess=(0.010, 0.030, 3000.0),  # gerçekten belirgin şekilde uzak
    )
    assert fitted.r0_ohm == pytest.approx(TRUE_CELL.r0_ohm, rel=0.15)
    assert fitted.r1_ohm == pytest.approx(TRUE_CELL.r1_ohm, rel=0.15)
    assert fitted.c1_farad == pytest.approx(TRUE_CELL.c1_farad, rel=0.25)
    assert fitted.rmse_v < 0.002  # fit artığı < 2 mV


def test_capacity_soh_detects_fade():
    aged = CellParams(capacity_ah=4.2)  # nominal 5.0 → %84
    current = step_profile([(3600.0, 2.0)])
    sim = simulate_cell(aged, current, dt_s=1.0, soc0=0.95)
    estimated = estimate_capacity_ah(
        current, dt_s=1.0, soc_start=0.95, soc_end=float(sim.true_soc[-1])
    )
    soh = capacity_soh(estimated, nominal_capacity_ah=5.0)
    assert soh == pytest.approx(0.84, abs=0.02)


def test_r0_estimate_from_current_step():
    current = step_profile([(60.0, 0.0), (60.0, 3.0)])
    sim = simulate_cell(TRUE_CELL, current, dt_s=1.0, soc0=0.8)
    r0 = estimate_r0_from_step(sim.terminal_voltage_v, current, step_index=60)
    # basamak anında ΔV ≈ R0·ΔI (+ ilk RC adımı payı)
    assert r0 == pytest.approx(TRUE_CELL.r0_ohm, rel=0.1)
    assert 0.5 < resistance_soh(r0, TRUE_CELL.r0_ohm) <= 1.1
