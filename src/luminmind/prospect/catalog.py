from typing import Any

from luminmind.prospect.layout import InverterSpec, ModuleSpec

# Panel Kataloğu: ID -> (ModuleSpec, birim fiyatı ₺/Wp)
# Fiyatları ₺/Wp (TRY) cinsinden tutalım (Örn: 20-30 ₺/Wp)
# Ev tipi bir sistemde Wp maliyeti: 28 ₺/Wp
# Ticari/Endüstriyel bir sistemde Wp maliyeti: 18 ₺/Wp

MODULE_CATALOG: dict[str, dict[str, Any]] = {
    "generic_580_topcon": {
        "label": "Jenerik 580 W TOPCon (Ticari / Endüstriyel)",
        "spec": ModuleSpec(
            name="Jenerik 580 W TOPCon",
            width_m=1.134,
            height_m=2.278,
            pdc0_w=580.0,
            gamma_pdc=-0.0029,
            voc_stc_v=52.0,
            vmp_stc_v=43.5,
            module_type="monosi",
        ),
        "base_capex_wp_try": 18.0,
    },
    "generic_410_halfcut": {
        "label": "Jenerik 410 W Half-Cut (Ev Tipi)",
        "spec": ModuleSpec(
            name="Jenerik 410 W Half-Cut",
            width_m=1.134,
            height_m=1.722,
            pdc0_w=410.0,
            gamma_pdc=-0.0035,
            voc_stc_v=37.5,
            vmp_stc_v=31.2,
            module_type="monosi",
        ),
        "base_capex_wp_try": 28.0,
    },
    "jinko_615_bifacial": {
        "label": "Jinko 615 W Tiger Neo Bifacial (Ticari)",
        "spec": ModuleSpec(
            name="Jinko 615 W Tiger Neo Bifacial",
            width_m=1.134,
            height_m=2.465,
            pdc0_w=615.0,
            gamma_pdc=-0.0029,
            voc_stc_v=55.4,
            vmp_stc_v=46.1,
            module_type="monosi",
            bifaciality=0.80,
        ),
        "base_capex_wp_try": 19.5,
    }
}

INVERTER_CATALOG: dict[str, dict[str, Any]] = {
    "generic_100kw": {
        "label": "Jenerik 100 kW String (Ticari)",
        "spec": InverterSpec(
            name="Jenerik 100 kW String",
            ac_kw=100.0,
            max_dc_voltage_v=1100.0,
            mppt_min_voltage_v=200.0,
            mppt_inputs=10,
            strings_per_mppt=2,
            eta_nom=0.98,
        )
    },
    "generic_10kw": {
        "label": "Jenerik 10 kW String (Ev Tipi)",
        "spec": InverterSpec(
            name="Jenerik 10 kW String",
            ac_kw=10.0,
            max_dc_voltage_v=1000.0,
            mppt_min_voltage_v=150.0,
            mppt_inputs=2,
            strings_per_mppt=1,
            eta_nom=0.97,
        )
    },
    "huawei_50kw": {
        "label": "Huawei SUN2000-50KTL (Ticari)",
        "spec": InverterSpec(
            name="Huawei SUN2000-50KTL",
            ac_kw=50.0,
            max_dc_voltage_v=1100.0,
            mppt_min_voltage_v=200.0,
            mppt_inputs=4,
            strings_per_mppt=2,
            eta_nom=0.983,
        )
    }
}
