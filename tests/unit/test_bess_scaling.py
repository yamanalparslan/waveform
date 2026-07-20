import pytest

from luminmind.bess.models import CellParams
from luminmind.bess.scaling import design_container, rd_bench_pack, scale_cell

CELL = CellParams()  # 5 Ah, OCV(0.5) ≈ 3.63 V


def test_8s_bench_pack_scaling():
    pack = rd_bench_pack(CELL)
    assert pack.series == 8 and pack.parallel == 1
    assert pack.nominal_voltage_v == pytest.approx(8 * 3.63, rel=0.01)
    assert pack.capacity_ah == CELL.capacity_ah
    assert pack.r0_ohm == pytest.approx(8 * CELL.r0_ohm)
    # RC zaman sabiti ölçeklemede korunur
    assert pack.r1_ohm * pack.c1_farad == pytest.approx(CELL.tau_s, rel=1e-9)


def test_parallel_scaling_divides_resistance():
    battery = scale_cell(CELL, series=1, parallel=4)
    assert battery.capacity_ah == 20.0
    assert battery.r0_ohm == pytest.approx(CELL.r0_ohm / 4)
    assert battery.energy_kwh == pytest.approx(3.63 * 20 / 1000, rel=0.01)


def test_container_design_meets_targets():
    container = design_container(CELL, target_energy_kwh=1000.0, target_dc_voltage_v=800.0)
    assert container.nominal_voltage_v == pytest.approx(800.0, rel=0.02)
    assert container.usable_energy_kwh >= 1000.0
    # hedefi aşırı aşmamalı (bir paralel kolu payı)
    per_string_kwh = container.energy_kwh / container.parallel
    assert container.usable_energy_kwh - 1000.0 < per_string_kwh
    assert container.derating == 0.92


def test_scale_rejects_invalid_configuration():
    with pytest.raises(ValueError):
        scale_cell(CELL, series=0, parallel=1)
    with pytest.raises(ValueError):
        design_container(CELL, target_energy_kwh=-5, target_dc_voltage_v=800)
