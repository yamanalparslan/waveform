"""Sunucu tarafında üretilen SVG grafikler.

Harici JS grafik kütüphanesi yerine saf Python: bağımlılık yok, çevrimdışı
çalışır, birim test edilebilir. Zaman ekseni çağıranın verdiği zaman diliminde
(UI'da TRT) etiketlenir.
"""

from dataclasses import dataclass
from datetime import datetime, tzinfo
from html import escape

_WIDTH = 900
_HEIGHT = 300
_PAD_LEFT = 62
_PAD_RIGHT = 16
_PAD_TOP = 14
_PAD_BOTTOM = 34


@dataclass(frozen=True)
class Series:
    label: str
    color: str
    points: list[tuple[datetime, float]]


def _empty(width: int, height: int, message: str) -> str:
    return (
        f'<svg viewBox="0 0 {width} {height}" class="chart" role="img">'
        f'<text x="{width / 2}" y="{height / 2}" text-anchor="middle" '
        f'class="chart-empty">{escape(message)}</text></svg>'
    )


def _scale(
    value: float, domain_min: float, domain_max: float, range_min: float, range_max: float
) -> float:
    if domain_max == domain_min:
        return (range_min + range_max) / 2
    ratio = (value - domain_min) / (domain_max - domain_min)
    return range_min + ratio * (range_max - range_min)


def sparkline(
    points: list[tuple[datetime, float]],
    color: str = "#f2b544",
    width: int = 220,
    height: int = 44,
) -> str:
    """Kart içi mini eğri — eksen/etiket yok, sadece dolgulu çizgi."""
    if not points:
        return (
            f'<svg viewBox="0 0 {width} {height}" class="spark" preserveAspectRatio="none">'
            f'<text x="{width / 2}" y="{height / 2 + 4}" text-anchor="middle" '
            f'style="fill:#6a778a;font-size:11px">veri bekleniyor</text></svg>'
        )
    pts = sorted(points)
    v_min = min(v for _, v in pts)
    v_max = max(v for _, v in pts)
    v_min = min(v_min, 0.0)
    if v_max == v_min:
        v_max = v_min + 1.0
    x_min = pts[0][0].timestamp()
    x_max = pts[-1][0].timestamp()
    if x_max == x_min:
        x_max = x_min + 1

    def sx(t: float) -> float:
        return (t - x_min) / (x_max - x_min) * width

    def sy(v: float) -> float:
        return height - (v - v_min) / (v_max - v_min) * (height - 4) - 2

    coords = " ".join(f"{sx(t.timestamp()):.1f},{sy(v):.1f}" for t, v in pts)
    area = (
        f"{sx(x_min):.1f},{height} "
        + coords
        + f" {sx(x_max):.1f},{height}"
    )
    return (
        f'<svg viewBox="0 0 {width} {height}" class="spark" preserveAspectRatio="none" '
        f'role="img">'
        f'<polygon points="{area}" fill="{color}" opacity="0.15"/>'
        f'<polyline points="{coords}" fill="none" stroke="{color}" '
        f'stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"/>'
        f"</svg>"
    )


def line_chart(
    series: list[Series],
    zone: tzinfo,
    unit: str = "kW",
    width: int = _WIDTH,
    height: int = _HEIGHT,
) -> str:
    """Çok serili çizgi grafik; y ekseni 0'dan başlar, x ekseni saat etiketli."""
    all_points = [p for s in series for p in s.points]
    if not all_points:
        return _empty(width, height, "Veri yok")

    t_min = min(ts for ts, _ in all_points).timestamp()
    t_max = max(ts for ts, _ in all_points).timestamp()
    v_max = max((v for _, v in all_points), default=0.0)
    v_max = v_max * 1.08 if v_max > 0 else 1.0

    x0, x1 = _PAD_LEFT, width - _PAD_RIGHT
    y0, y1 = height - _PAD_BOTTOM, _PAD_TOP

    parts: list[str] = [
        f'<svg viewBox="0 0 {width} {height}" class="chart" role="img">',
    ]
    # y ekseni ızgarası + etiketleri
    for i in range(5):
        value = v_max * i / 4
        y = _scale(value, 0.0, v_max, y0, y1)
        parts.append(
            f'<line x1="{x0}" y1="{y:.1f}" x2="{x1}" y2="{y:.1f}" class="grid"/>'
            f'<text x="{x0 - 8}" y="{y + 4:.1f}" text-anchor="end" class="tick">'
            f"{value:,.0f}</text>"
        )
    parts.append(
        f'<text x="14" y="{_PAD_TOP + 10}" class="tick unit">{escape(unit)}</text>'
    )
    # x ekseni saat etiketleri (6 dilim)
    for i in range(7):
        t = t_min + (t_max - t_min) * i / 6
        x = _scale(t, t_min, t_max, x0, x1)
        label = datetime.fromtimestamp(t, tz=zone).strftime("%H:%M")
        parts.append(
            f'<text x="{x:.1f}" y="{height - 12}" text-anchor="middle" class="tick">'
            f"{label}</text>"
        )
    # seriler
    for s in series:
        if not s.points:
            continue
        coords = " ".join(
            f"{_scale(ts.timestamp(), t_min, t_max, x0, x1):.1f},"
            f"{_scale(v, 0.0, v_max, y0, y1):.1f}"
            for ts, v in sorted(s.points)
        )
        parts.append(
            f'<polyline points="{coords}" fill="none" stroke="{s.color}" '
            f'stroke-width="2"/>'
        )
    # gösterge (legend)
    legend_x = x0
    for s in series:
        parts.append(
            f'<rect x="{legend_x}" y="{_PAD_TOP - 10}" width="10" height="10" '
            f'fill="{s.color}" rx="2"/>'
            f'<text x="{legend_x + 14}" y="{_PAD_TOP}" class="tick">{escape(s.label)}</text>'
        )
        legend_x += 14 + 8 * len(s.label) + 24
    parts.append("</svg>")
    return "".join(parts)


def price_plan_chart(
    prices: list[tuple[datetime, float]],
    actions: dict[datetime, tuple[str, float]],
    zone: tzinfo,
    width: int = _WIDTH,
    height: int = _HEIGHT,
) -> str:
    """GÖP fiyat eğrisi (basamaklı çizgi) + alt bantta saatlik şarj/deşarj planı."""
    if not prices:
        return _empty(width, height, "Fiyat verisi yok")
    prices = sorted(prices)
    band_h = 26
    t_min = prices[0][0].timestamp()
    slot_s = (
        (prices[1][0].timestamp() - t_min) if len(prices) > 1 else 3600.0
    )
    t_max = prices[-1][0].timestamp() + slot_s
    p_max = max(v for _, v in prices) * 1.08

    x0, x1 = _PAD_LEFT, width - _PAD_RIGHT
    y0, y1 = height - _PAD_BOTTOM - band_h, _PAD_TOP

    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img">']
    for i in range(5):
        value = p_max * i / 4
        y = _scale(value, 0.0, p_max, y0, y1)
        parts.append(
            f'<line x1="{x0}" y1="{y:.1f}" x2="{x1}" y2="{y:.1f}" class="grid"/>'
            f'<text x="{x0 - 8}" y="{y + 4:.1f}" text-anchor="end" class="tick">'
            f"{value:,.0f}</text>"
        )
    parts.append(f'<text x="6" y="{_PAD_TOP + 10}" class="tick unit">TL/MWh</text>')

    # basamaklı fiyat çizgisi
    coords: list[str] = []
    for ts, price in prices:
        xa = _scale(ts.timestamp(), t_min, t_max, x0, x1)
        xb = _scale(ts.timestamp() + slot_s, t_min, t_max, x0, x1)
        y = _scale(price, 0.0, p_max, y0, y1)
        coords.append(f"{xa:.1f},{y:.1f} {xb:.1f},{y:.1f}")
    parts.append(
        f'<polyline points="{" ".join(coords)}" fill="none" stroke="#e3b341" '
        f'stroke-width="2"/>'
    )

    # eylem bandı
    colors = {"charge": "#2ea87e", "discharge": "#d9634c", "idle": "#3a4152"}
    band_y = height - _PAD_BOTTOM - band_h + 6
    for ts, _price in prices:
        action, _power = actions.get(ts, ("idle", 0.0))
        xa = _scale(ts.timestamp(), t_min, t_max, x0, x1)
        xb = _scale(ts.timestamp() + slot_s, t_min, t_max, x0, x1)
        parts.append(
            f'<rect x="{xa:.1f}" y="{band_y}" width="{xb - xa:.1f}" height="{band_h - 10}" '
            f'fill="{colors.get(action, colors["idle"])}" rx="2"/>'
        )
    # x etiketleri
    for i in range(7):
        t = t_min + (t_max - t_min) * i / 6
        x = _scale(t, t_min, t_max, x0, x1)
        label = datetime.fromtimestamp(t, tz=zone).strftime("%H:%M")
        parts.append(
            f'<text x="{x:.1f}" y="{height - 10}" text-anchor="middle" class="tick">'
            f"{label}</text>"
        )
    # gösterge
    legend = [
        ("Şarj", colors["charge"]),
        ("Deşarj", colors["discharge"]),
        ("Beklemede", colors["idle"]),
    ]
    legend_x = x0
    for label, color in legend:
        parts.append(
            f'<rect x="{legend_x}" y="{_PAD_TOP - 10}" width="10" height="10" '
            f'fill="{color}" rx="2"/>'
            f'<text x="{legend_x + 14}" y="{_PAD_TOP}" class="tick">{escape(label)}</text>'
        )
        legend_x += 14 + 9 * len(label) + 24
    parts.append("</svg>")
    return "".join(parts)
