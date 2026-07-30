"""Saf SVG grafik üreteçleri: yığılmış bar, gruplu bar, ısı haritası, halka.

Testler iki şeyi koruyor: (1) boyut uyuşmazlıklarında sessizce yanlış hücreye
değer yazılmaması, (2) "veri yok" ile "sıfır"ın birbirine karışmaması. İkisi de
grafikte hata gibi görünmez, sadece yanlış teşhis üretir.
"""

import re

import pytest

from luminmind.web.advice import PR_NORMAL_PCT, PR_WEAK_PCT
from luminmind.web.charts import (
    BarGroup,
    Segment,
    donut,
    grouped_bar,
    heatmap,
    performance_color,
    stacked_bar,
)
from luminmind.web.theme import PALETTE, chip_color

RECT_WIDTHS = re.compile(r'<rect[^>]*width="([\d.]+)"')


def widths(svg: str) -> list[float]:
    return [float(w) for w in RECT_WIDTHS.findall(svg)]


# ------------------------------ yığılmış bar ------------------------------


def test_segments_are_proportional_and_fill_the_bar():
    svg = stacked_bar(
        [
            Segment("Acil", 60.0, PALETTE["red"]),
            Segment("Orta", 30.0, PALETTE["amber"]),
            Segment("Uzun", 10.0, PALETTE["yellow"]),
        ]
    )
    assert widths(svg) == pytest.approx([60.0, 30.0, 10.0])
    assert sum(widths(svg)) == pytest.approx(100.0)


def test_absolute_values_are_normalised_to_percentages():
    """Girdi ₺ ya da kWh olabilir; bar her zaman oran gösterir."""
    svg = stacked_bar(
        [Segment("Acil", 90_000.0, PALETTE["red"]), Segment("Uzun", 30_000.0, PALETTE["yellow"])]
    )
    assert widths(svg) == pytest.approx([75.0, 25.0])


def test_zero_segments_are_skipped_not_drawn_as_slivers():
    svg = stacked_bar(
        [
            Segment("Acil", 0.0, PALETTE["red"]),
            Segment("Orta", 50.0, PALETTE["amber"]),
            Segment("Uzun", 0.0, PALETTE["yellow"]),
        ]
    )
    assert widths(svg) == pytest.approx([100.0])
    assert PALETTE["red"] not in svg


def test_empty_breakdown_renders_a_neutral_track_not_a_broken_chart():
    svg = stacked_bar([])
    assert "veri yok" in svg
    assert widths(svg) == pytest.approx([100.0])


def test_negative_values_cannot_invert_the_bar():
    svg = stacked_bar(
        [Segment("Acil", -10.0, PALETTE["red"]), Segment("Orta", 40.0, PALETTE["amber"])]
    )
    assert widths(svg) == pytest.approx([100.0])


def test_bar_stretches_without_distorting_text():
    """Bar esnerken metin deforme olmasın diye içine hiç metin konmaz."""
    svg = stacked_bar([Segment("Acil", 1.0, PALETTE["red"])])
    assert 'preserveAspectRatio="none"' in svg
    assert "<text" not in svg
    assert "<title>" in svg  # erişilebilirlik yine korunur


def test_bar_summary_lists_the_shares():
    svg = stacked_bar(
        [Segment("Acil", 75.0, PALETTE["red"]), Segment("Uzun", 25.0, PALETTE["yellow"])]
    )
    assert "Acil %75" in svg and "Uzun %25" in svg


# ------------------------------ çizgi grafik ------------------------------


def test_dashed_series_is_visually_separated_from_measurements():
    """Model çıktısını ölçümle aynı çizgiyle çizmek ikisini eşit güvenilir gösterir."""
    from datetime import UTC, datetime, timedelta

    from luminmind.web.charts import Series, line_chart

    t0 = datetime(2026, 7, 29, 9, 0, tzinfo=UTC)
    points = [(t0 + timedelta(minutes=15 * i), 100.0 + i) for i in range(4)]
    svg = line_chart(
        [
            Series("Ölçüm", PALETTE["blue"], points),
            Series("Beklenen", PALETTE["violet"], points, dashed=True),
        ],
        UTC,
    )
    # Yalnız referans eğrisi kesik; ölçüm düz kalmalı
    assert svg.count('stroke-dasharray="7 5"') == 1
    # Gösterge de kesik görünür, yoksa hangisinin model olduğu ayırt edilemez
    assert 'stroke-dasharray="4 3"' in svg


# ------------------------------ gruplu bar ------------------------------


LABELS = ("Gerçek PR", "STC PR", "İkiz PR")
COLORS = (PALETTE["blue"], PALETTE["green"], PALETTE["violet"])


def test_grouped_bar_draws_every_series_in_every_group():
    groups = [
        BarGroup("Pzt", [88.0, 92.0, 90.0]),
        BarGroup("Sal", [81.0, 90.0, 89.0]),
    ]
    svg = grouped_bar(groups, LABELS, COLORS)
    for color in COLORS:
        assert svg.count(f'fill="{color}"') == 3  # 2 bar + 1 gösterge karesi
    assert "Pzt" in svg and "Sal" in svg


def test_taller_value_gets_a_taller_bar():
    groups = [BarGroup("Pzt", [40.0]), BarGroup("Sal", [80.0])]
    svg = grouped_bar(groups, ("PR",), (PALETTE["blue"],))
    heights = [float(h) for h in re.findall(r'<rect[^>]*height="([\d.]+)"', svg)]
    bars = [h for h in heights if h != 10.0]  # 10 = gösterge karesi
    assert bars[1] > bars[0]


def test_mismatched_colour_count_is_rejected():
    with pytest.raises(ValueError, match="renk"):
        grouped_bar([BarGroup("Pzt", [1.0, 2.0])], ("a", "b"), (PALETTE["blue"],))


def test_group_with_missing_value_is_rejected_not_silently_shifted():
    """Eksik değeri sessizce kabul etmek ikinci seriyi birinciye kaydırırdı."""
    with pytest.raises(ValueError, match="Sal"):
        grouped_bar(
            [BarGroup("Pzt", [1.0, 2.0]), BarGroup("Sal", [3.0])],
            ("a", "b"),
            (PALETTE["blue"], PALETTE["green"]),
        )


def test_grouped_bar_without_groups_says_so():
    assert "Veri yok" in grouped_bar([], LABELS, COLORS)


def test_grouped_bar_survives_an_all_zero_week():
    svg = grouped_bar([BarGroup("Pzt", [0.0])], ("PR",), (PALETTE["blue"],))
    assert "<svg" in svg and "Veri yok" not in svg


def test_bar_tooltips_carry_the_value_and_unit():
    svg = grouped_bar([BarGroup("Pzt", [88.4])], ("Gerçek PR",), (PALETTE["blue"],), unit="%")
    assert "Pzt · Gerçek PR: 88.4 %" in svg


# ------------------------------ ısı haritası ------------------------------


HOURS = tuple(f"{h:02d}" for h in range(6, 12))


def test_heatmap_paints_one_cell_per_pair():
    values = [[90.0] * len(HOURS), [70.0] * len(HOURS)]
    svg = heatmap(("1 nolu", "2 nolu"), HOURS, values)
    assert len(RECT_WIDTHS.findall(svg)) == 2 * len(HOURS)
    assert "1 nolu" in svg and "06" in svg


def test_missing_data_is_neutral_not_zero():
    """None hücreyi 0 boyamak 'hiç üretmedi' demek olurdu — başka bir teşhis."""
    svg = heatmap(("1 nolu",), ("06",), [[None]])
    assert PALETTE["surface-3"] in svg
    assert "veri yok" in svg
    zero = heatmap(("1 nolu",), ("06",), [[0.0]])
    assert chip_color("crit") in zero  # sıfır üretim gerçekten kritik


def test_row_count_mismatch_is_rejected():
    with pytest.raises(ValueError, match="satır"):
        heatmap(("1", "2"), ("06",), [[90.0]])


def test_column_count_mismatch_names_the_offending_row():
    with pytest.raises(ValueError, match="2 nolu"):
        heatmap(("1 nolu", "2 nolu"), ("06", "07"), [[90.0, 91.0], [88.0]])


def test_empty_heatmap_reports_no_data():
    assert "Veri yok" in heatmap((), (), [])
    assert "Veri yok" in heatmap(("1",), (), [[]])


def test_custom_colour_scale_is_honoured():
    svg = heatmap(("1",), ("06",), [[50.0]], color_of=lambda _v: PALETTE["violet"])
    assert PALETTE["violet"] in svg


def test_performance_colour_follows_the_shared_thresholds():
    """Isı haritası ile durum çipi aynı eşiği kullanmalı, yoksa ekran kendiyle çelişir."""
    assert performance_color(PR_NORMAL_PCT + 1) == chip_color("ok")
    assert performance_color(PR_NORMAL_PCT - 1) == chip_color("warn")
    assert performance_color(PR_WEAK_PCT - 1) == chip_color("crit")


def test_heatmap_escapes_device_labels():
    svg = heatmap(('<script>"x"',), ("06",), [[90.0]])
    assert "<script>" not in svg
    assert "&lt;script&gt;" in svg


# ------------------------------ halka ------------------------------


def test_donut_arc_grows_with_the_value():
    def arc(svg: str) -> float:
        return float(re.search(r'stroke-dasharray="([\d.]+)', svg).group(1))

    assert arc(donut(0.0)) == pytest.approx(0.0)
    assert arc(donut(100.0)) > arc(donut(50.0)) > arc(donut(10.0))


def test_donut_clamps_out_of_range_values():
    assert "%100.0" in donut(140.0)
    assert "%0.0" in donut(-5.0)


def test_donut_shows_the_percentage_and_caption():
    svg = donut(12.4, caption="kurtarılabilir")
    assert "%12.4" in svg
    assert "kurtarılabilir" in svg


def test_donut_without_caption_centres_the_number():
    svg = donut(30.0)
    assert svg.count("<text") == 1


def test_donut_uses_the_given_accent():
    assert PALETTE["red"] in donut(50.0, color=PALETTE["red"])
