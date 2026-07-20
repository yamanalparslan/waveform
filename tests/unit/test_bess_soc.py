"""Coulomb Counting ve EKF doğrulaması: sentetik profilde gerçek SoC bilinir."""

import numpy as np
import pytest

from luminmind.bess.coulomb import coulomb_soc
from luminmind.bess.ekf import run_ekf
from luminmind.bess.models import CellParams
from luminmind.bess.synthetic import default_validation_profile, simulate_cell, step_profile

CELL = CellParams()
DT = 1.0


def rmse(a, b) -> float:
    return float(np.sqrt(np.mean((a - b) ** 2)))


def test_simulator_soc_decreases_under_discharge():
    current = step_profile([(600.0, 2.0)])
    sim = simulate_cell(CELL, current, dt_s=DT, soc0=0.9)
    assert sim.true_soc[-1] < 0.9
    # deşarjda terminal gerilimi OCV'nin altında kalır
    assert sim.terminal_voltage_v[-1] < float(CELL.ocv.voltage(sim.true_soc[-1]))


def test_coulomb_exact_with_perfect_sensor():
    current = default_validation_profile()
    sim = simulate_cell(CELL, current, dt_s=DT, soc0=0.9)
    estimate = coulomb_soc(current, DT, CELL.capacity_ah, 0.9, CELL.coulomb_efficiency)
    assert rmse(estimate, sim.true_soc) < 1e-9


def test_coulomb_drifts_with_current_bias():
    current = default_validation_profile()
    sim = simulate_cell(CELL, current, dt_s=DT, soc0=0.9)
    biased = current + 0.05  # 50 mA sensör bias'ı
    estimate = coulomb_soc(biased, DT, CELL.capacity_ah, 0.9, CELL.coulomb_efficiency)
    # bias birikir: ~0.05A × 2 saat / 5Ah ≈ %2 hata
    assert abs(estimate[-1] - sim.true_soc[-1]) > 0.015


def test_ekf_tracks_soc_within_2pct_with_noise_and_bias():
    """PLAN.md Faz 4 hedefi: sentetik veride RMSE < %2 (gürültü + bias + yanlış başlangıç)."""
    current = default_validation_profile()
    sim = simulate_cell(
        CELL, current, dt_s=DT, soc0=0.9, voltage_noise_std_v=0.005,
        rng=np.random.default_rng(7),
    )
    biased_current = current + 0.05
    estimate = run_ekf(CELL, biased_current, sim.terminal_voltage_v, DT, soc0_guess=0.6)

    settle = len(estimate) // 4  # yakınsama sonrası değerlendir
    assert rmse(estimate[settle:], sim.true_soc[settle:]) < 0.02
    assert abs(estimate[-1] - sim.true_soc[-1]) < 0.02


def test_ekf_recovers_from_wrong_initial_soc():
    current = step_profile([(1200.0, 2.0), (600.0, 0.0)])
    sim = simulate_cell(CELL, current, dt_s=DT, soc0=0.9)
    estimate = run_ekf(CELL, current, sim.terminal_voltage_v, DT, soc0_guess=0.4)
    assert abs(estimate[-1] - sim.true_soc[-1]) < 0.02


def test_ekf_beats_biased_coulomb():
    current = default_validation_profile()
    sim = simulate_cell(
        CELL, current, dt_s=DT, soc0=0.9, voltage_noise_std_v=0.005,
        rng=np.random.default_rng(11),
    )
    biased = current + 0.05
    ekf_est = run_ekf(CELL, biased, sim.terminal_voltage_v, DT, soc0_guess=0.9)
    cc_est = coulomb_soc(biased, DT, CELL.capacity_ah, 0.9, CELL.coulomb_efficiency)
    half = len(current) // 2
    assert rmse(ekf_est[half:], sim.true_soc[half:]) < rmse(cc_est[half:], sim.true_soc[half:])


def test_ekf_rejects_mismatched_inputs():
    with pytest.raises(ValueError, match="equal length"):
        run_ekf(CELL, np.zeros(10), np.zeros(9), DT, soc0_guess=0.5)
