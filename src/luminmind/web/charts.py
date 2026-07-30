"""Sunucu tarafında üretilen SVG grafikler.

Harici JS grafik kütüphanesi yerine saf Python: bağımlılık yok, çevrimdışı
çalışır, birim test edilebilir. Zaman ekseni çağıranın verdiği zaman diliminde
(UI'da TRT) etiketlenir.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo
from html import escape
from math import pi, sqrt

from luminmind.web.advice import performance_chip
from luminmind.web.theme import (
    CHART_ACTUAL,
    CHART_CHARGE,
    CHART_DISCHARGE,
    CHART_EMPTY_TEXT,
    CHART_IDLE,
    CHART_PRICE,
    PALETTE,
    chip_color,
)

_WIDTH = 900
_HEIGHT = 300
_PAD_LEFT = 62
_PAD_RIGHT = 16
_PAD_TOP = 14
_PAD_BOTTOM = 34


@dataclass(frozen=True)
class Series:
    """Grafikteki tek eğri.

    `dashed=True` ölçülmemiş, türetilmiş bir referansı işaretler (beklenti,
    hedef, pay). Ölçümle referansı aynı kalınlıkta düz çizgiyle çizmek ikisini
    eşit güvenilirlikte gösterir; kesik çizgi "bu bir model çıktısı" der.
    """

    label: str
    color: str
    points: list[tuple[datetime, float]]
    dashed: bool = False


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


def _monotone_slopes(xs: "Sequence[float]", ys: "Sequence[float]") -> list[float]:
    """Fritsch–Carlson monoton kübik teğetleri.

    Sıradan bir kardinal spline veri **uydurur**: iki nokta arasında ikisinden
    de düşük bir çukur ya da yüksek bir tepe çizer. Üretim grafiğinde bu,
    gerçekleşmemiş bir düşüşü gerçek gibi göstermek olurdu — 0'ın altına inen
    bir üretim eğrisi bile çıkabilir. Fritsch–Carlson sınırlayıcısı teğetleri
    kısarak eğrinin her aralıkta o aralığın iki değeri arasında kalmasını
    garanti eder: yumuşak ama sadık.
    """
    n = len(ys)
    if n < 2:
        return [0.0] * n
    h = [xs[i + 1] - xs[i] for i in range(n - 1)]
    secant = [
        (ys[i + 1] - ys[i]) / h[i] if h[i] else 0.0 for i in range(n - 1)
    ]
    slopes = [0.0] * n
    slopes[0] = secant[0]
    slopes[-1] = secant[-1]
    for i in range(1, n - 1):
        # İşaret değişimi = yerel tepe/çukur; teğeti sıfırlamak taşmayı keser
        slopes[i] = 0.0 if secant[i - 1] * secant[i] <= 0 else (secant[i - 1] + secant[i]) / 2
    for i in range(n - 1):
        if secant[i] == 0.0:
            slopes[i] = slopes[i + 1] = 0.0
            continue
        a, b = slopes[i] / secant[i], slopes[i + 1] / secant[i]
        magnitude = a * a + b * b
        if magnitude > 9.0:
            scale = 3.0 / sqrt(magnitude)
            slopes[i] = scale * a * secant[i]
            slopes[i + 1] = scale * b * secant[i]
    return slopes


def _smooth_path(points: "Sequence[tuple[float, float]]") -> str:
    """Ekran koordinatlarından yumuşak SVG `d` yolu (monoton kübik Bézier)."""
    if not points:
        return ""
    if len(points) == 1:
        x, y = points[0]
        return f"M{x:.1f},{y:.1f}"
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    slopes = _monotone_slopes(xs, ys)
    parts = [f"M{xs[0]:.1f},{ys[0]:.1f}"]
    for i in range(len(points) - 1):
        span = (xs[i + 1] - xs[i]) / 3.0
        parts.append(
            f"C{xs[i] + span:.1f},{ys[i] + slopes[i] * span:.1f} "
            f"{xs[i + 1] - span:.1f},{ys[i + 1] - slopes[i + 1] * span:.1f} "
            f"{xs[i + 1]:.1f},{ys[i + 1]:.1f}"
        )
    return "".join(parts)


def sparkline(
    points: list[tuple[datetime, float]],
    color: str = CHART_ACTUAL,
    width: int = 220,
    height: int = 44,
) -> str:
    """Kart içi mini eğri — eksen/etiket yok, sadece dolgulu çizgi."""
    if not points:
        return (
            f'<svg viewBox="0 0 {width} {height}" class="spark" preserveAspectRatio="none">'
            f'<text x="{width / 2}" y="{height / 2 + 4}" text-anchor="middle" '
            f'style="fill:{CHART_EMPTY_TEXT};font-size:11px">veri bekleniyor</text></svg>'
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

    coords = [(sx(t.timestamp()), sy(v)) for t, v in pts]
    curve = _smooth_path(coords)
    # Dolgu aynı eğriyi izler, sonra tabana iner — iki ayrı geometri çizmek
    # dolgu ile çizginin birbirinden ayrılmasına yol açardı
    area = f"{curve} L{coords[-1][0]:.1f},{height} L{coords[0][0]:.1f},{height} Z"
    return (
        f'<svg viewBox="0 0 {width} {height}" class="spark" preserveAspectRatio="none" '
        f'role="img">'
        f'<path d="{area}" fill="{color}" opacity="0.15"/>'
        f'<path d="{curve}" fill="none" stroke="{color}" '
        f'stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"/>'
        f"</svg>"
    )


def line_chart(
    series: list[Series],
    zone: tzinfo,
    unit: str = "kW",
    width: int = _WIDTH,
    height: int = _HEIGHT,
    time_format: str = "%H:%M",
) -> str:
    """Çok serili çizgi grafik; y ekseni 0'dan başlar.

    `time_format` gün içi eğriler için saat (`%H:%M`), günlük toplamlar için
    tarih (`%d.%m`) verir. Sabit saat biçimi günlük veride bütün etiketleri
    `00:00` yapıyordu.
    """
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
        label = datetime.fromtimestamp(t, tz=zone).strftime(time_format)
        parts.append(
            f'<text x="{x:.1f}" y="{height - 12}" text-anchor="middle" class="tick">'
            f"{label}</text>"
        )
    # seriler
    for s in series:
        if not s.points:
            continue
        coords = [
            (
                _scale(ts.timestamp(), t_min, t_max, x0, x1),
                _scale(v, 0.0, v_max, y0, y1),
            )
            for ts, v in sorted(s.points)
        ]
        dash = ' stroke-dasharray="7 5"' if s.dashed else ""
        parts.append(
            f'<path d="{_smooth_path(coords)}" fill="none" stroke="{s.color}" '
            f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"{dash}/>'
        )
    # gösterge (legend) — kesik çizgili seriler göstergede de kesik görünür,
    # yoksa hangi eğrinin ölçüm hangisinin model olduğu ayırt edilemez
    legend_x = x0
    for s in series:
        if s.dashed:
            swatch = (
                f'<line x1="{legend_x}" y1="{_PAD_TOP - 5}" x2="{legend_x + 12}" '
                f'y2="{_PAD_TOP - 5}" stroke="{s.color}" stroke-width="2" '
                f'stroke-dasharray="4 3"/>'
            )
        else:
            swatch = (
                f'<rect x="{legend_x}" y="{_PAD_TOP - 10}" width="10" height="10" '
                f'fill="{s.color}" rx="2"/>'
            )
        parts.append(
            f'{swatch}<text x="{legend_x + 16}" y="{_PAD_TOP}" class="tick">'
            f"{escape(s.label)}</text>"
        )
        legend_x += 16 + 8 * len(s.label) + 24
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
            # Fiyatlar TL/MWh gelir; kullanıcı elektriği kWh üzerinden düşündüğü
            # için eksende ₺/kWh gösteririz (ölçek aynı, yalnızca etiket çevrilir).
            f'<text x="{x0 - 8}" y="{y + 4:.1f}" text-anchor="end" class="tick">'
            f"{value / 1000:,.2f}</text>"
        )
    parts.append(f'<text x="6" y="{_PAD_TOP + 10}" class="tick unit">₺/kWh</text>')

    # basamaklı fiyat çizgisi
    coords: list[str] = []
    for ts, price in prices:
        xa = _scale(ts.timestamp(), t_min, t_max, x0, x1)
        xb = _scale(ts.timestamp() + slot_s, t_min, t_max, x0, x1)
        y = _scale(price, 0.0, p_max, y0, y1)
        coords.append(f"{xa:.1f},{y:.1f} {xb:.1f},{y:.1f}")
    parts.append(
        f'<polyline points="{" ".join(coords)}" fill="none" stroke="{CHART_PRICE}" '
        f'stroke-width="2"/>'
    )

    # eylem bandı
    colors = {"charge": CHART_CHARGE, "discharge": CHART_DISCHARGE, "idle": CHART_IDLE}
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


# ==================== DeepSolar tarzı bileşenler ====================
# Hepsi saf SVG: harici JS yok, çevrimdışı çalışır, birim test edilebilir.
# Renkler `theme.py`'den gelir; buraya sabit renk kodu yazılmaz.


@dataclass(frozen=True)
class Segment:
    """Yığılmış barın bir dilimi."""

    label: str
    value: float
    color: str


def _segment_summary(segments: "Sequence[Segment]", total: float) -> str:
    shares = [
        f"{s.label} %{max(0.0, s.value) / total * 100:.0f}"
        for s in segments
        if s.value > 0.0
    ]
    return ", ".join(shares)


def stacked_bar(segments: "Sequence[Segment]", height: int = 10) -> str:
    """Tek satırlık yığılmış oran barı (ör. Acil / Orta / Uzun kırılımı).

    Metin **içermez** ve `preserveAspectRatio="none"` ile esner: kart genişliği
    ne olursa olsun yüksekliği CSS'ten sabit kalır, dilim oranları bozulmaz.
    Etiketler HTML'de (`.prio-legend`) durur — SVG içine yazılsalardı bar
    esnerken harfler de yatay olarak deforme olurdu.
    """
    total = sum(max(0.0, s.value) for s in segments)
    parts = [
        f'<svg viewBox="0 0 100 {height}" class="stacked-bar" role="img" '
        f'preserveAspectRatio="none">'
    ]
    if total <= 0.0:
        # Boş bar: "kayıp yok" hâli; kırık grafik gibi görünmemeli
        parts.append("<title>Kırılım için veri yok</title>")
        parts.append(
            f'<rect x="0" y="0" width="100" height="{height}" fill="{CHART_IDLE}" '
            f'opacity="0.28"/></svg>'
        )
        return "".join(parts)

    parts.append(f"<title>{escape(_segment_summary(segments, total))}</title>")
    offset = 0.0
    for segment in segments:
        value = max(0.0, segment.value)
        if value <= 0.0:
            continue
        span = value / total * 100.0
        parts.append(
            f'<rect x="{offset:.3f}" y="0" width="{span:.3f}" height="{height}" '
            f'fill="{segment.color}"/>'
        )
        offset += span
    parts.append("</svg>")
    return "".join(parts)


@dataclass(frozen=True)
class BarGroup:
    """Gruplu barın bir kategorisi; `values` seri sırasıyla eşleşir."""

    label: str
    values: list[float]


def grouped_bar(
    groups: "Sequence[BarGroup]",
    series_labels: "Sequence[str]",
    colors: "Sequence[str]",
    unit: str = "%",
    width: int = _WIDTH,
    height: int = 260,
) -> str:
    """Kategori başına yan yana barlar (ör. haftalık Gerçek / STC / İkiz PR).

    Seri sayısı ile renk sayısı uyuşmazsa hata verir; sessizce döngüye almak
    iki farklı seriyi aynı renge boyar ve grafik yanlış okunur.
    """
    if len(series_labels) != len(colors):
        raise ValueError("her seri için bir renk gerekir")
    if not groups:
        return _empty(width, height, "Veri yok")
    for group in groups:
        if len(group.values) != len(series_labels):
            raise ValueError(f"{group.label}: değer sayısı seri sayısıyla eşleşmiyor")

    v_max = max((v for g in groups for v in g.values), default=0.0)
    v_max = v_max * 1.12 if v_max > 0 else 1.0

    x0, x1 = _PAD_LEFT, width - _PAD_RIGHT
    y0, y1 = height - _PAD_BOTTOM, _PAD_TOP + 14

    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img">']
    for i in range(5):
        value = v_max * i / 4
        y = _scale(value, 0.0, v_max, y0, y1)
        parts.append(
            f'<line x1="{x0}" y1="{y:.1f}" x2="{x1}" y2="{y:.1f}" class="grid"/>'
            f'<text x="{x0 - 8}" y="{y + 4:.1f}" text-anchor="end" class="tick">'
            f"{value:,.0f}</text>"
        )
    parts.append(f'<text x="10" y="{_PAD_TOP + 20}" class="tick unit">{escape(unit)}</text>')

    slot = (x1 - x0) / len(groups)
    bar_gap = 3.0
    inner = slot * 0.72
    bar_w = max(3.0, (inner - bar_gap * (len(series_labels) - 1)) / len(series_labels))
    for gi, group in enumerate(groups):
        base = x0 + slot * gi + (slot - inner) / 2
        for si, value in enumerate(group.values):
            bx = base + si * (bar_w + bar_gap)
            by = _scale(max(0.0, value), 0.0, v_max, y0, y1)
            parts.append(
                f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bar_w:.1f}" '
                f'height="{max(0.0, y0 - by):.1f}" fill="{colors[si]}" rx="2">'
                f"<title>{escape(group.label)} · {escape(series_labels[si])}: "
                f"{value:,.1f} {escape(unit)}</title></rect>"
            )
        parts.append(
            f'<text x="{x0 + slot * gi + slot / 2:.1f}" y="{height - 12}" '
            f'text-anchor="middle" class="tick">{escape(group.label)}</text>'
        )

    legend_x = x0
    for label, color in zip(series_labels, colors, strict=True):
        parts.append(
            f'<rect x="{legend_x}" y="{_PAD_TOP - 8}" width="10" height="10" '
            f'fill="{color}" rx="2"/>'
            f'<text x="{legend_x + 14}" y="{_PAD_TOP + 2}" class="tick">{escape(label)}</text>'
        )
        legend_x += 14 + 8 * len(label) + 22
    parts.append("</svg>")
    return "".join(parts)


def performance_color(pct: float) -> str:
    """Performans oranı (%) → renk. Eşikler `advice.performance_chip`'ten gelir."""
    return chip_color(performance_chip(pct))


def heatmap(
    row_labels: "Sequence[str]",
    col_labels: "Sequence[str]",
    values: "Sequence[Sequence[float | None]]",
    color_of: "Callable[[float], str] | None" = None,
    unit: str = "%",
    width: int = _WIDTH,
    row_height: int = 26,
    label_width: int = 120,
) -> str:
    """Satır × sütun performans ızgarası (cihaz × saat).

    `None` hücre "veri yok" demektir ve nötr boyanır; 0 boyamak, ölçüm
    gelmemiş bir saati "hiç üretmedi" gibi gösterirdi ki bambaşka bir teşhis.

    Boyut uyuşmazlığında hata verir: sessizce kırpmak bir cihazın değerini
    başka bir saatin hücresine yazardı.
    """
    if not row_labels or not col_labels:
        return _empty(width, row_height * 3, "Veri yok")
    if len(values) != len(row_labels):
        raise ValueError("satır sayısı etiket sayısıyla eşleşmiyor")
    for label, row in zip(row_labels, values, strict=True):
        if len(row) != len(col_labels):
            raise ValueError(f"{label}: sütun sayısı etiket sayısıyla eşleşmiyor")

    paint = color_of or performance_color
    header_h = 20
    height = header_h + row_height * len(row_labels) + 6
    cell_w = (width - label_width - _PAD_RIGHT) / len(col_labels)

    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart heat" role="img">']
    for ci, col in enumerate(col_labels):
        parts.append(
            f'<text x="{label_width + cell_w * (ci + 0.5):.1f}" y="{header_h - 7}" '
            f'text-anchor="middle">{escape(col)}</text>'
        )
    for ri, row_label in enumerate(row_labels):
        y = header_h + row_height * ri
        parts.append(
            f'<text x="{label_width - 10}" y="{y + row_height / 2 + 3.5:.1f}" '
            f'text-anchor="end" class="label">{escape(row_label)}</text>'
        )
        for ci, value in enumerate(values[ri]):
            x = label_width + cell_w * ci
            if value is None:
                fill, opacity, title = PALETTE["surface-3"], "1", "veri yok"
            else:
                fill, opacity = paint(value), "0.85"
                title = f"{value:,.0f} {unit}"
            parts.append(
                f'<rect x="{x:.1f}" y="{y}" width="{cell_w:.1f}" height="{row_height}" '
                f'fill="{fill}" opacity="{opacity}" rx="2">'
                f"<title>{escape(row_label)} · {escape(col_labels[ci])}: "
                f"{escape(title)}</title></rect>"
            )
    parts.append("</svg>")
    return "".join(parts)


def donut(
    value_pct: float,
    color: str = CHART_ACTUAL,
    size: int = 120,
    caption: str = "",
) -> str:
    """Ortasında yüzde yazan halka. Değer 0–100 aralığına kırpılır."""
    clamped = min(100.0, max(0.0, value_pct))
    radius = size / 2 - 9
    circumference = 2 * pi * radius
    filled = circumference * clamped / 100.0
    center = size / 2
    parts = [
        f'<svg viewBox="0 0 {size} {size}" class="chart" role="img">',
        f"<title>%{clamped:.1f}{' ' + escape(caption) if caption else ''}</title>",
        f'<circle cx="{center}" cy="{center}" r="{radius:.1f}" fill="none" '
        f'stroke="{PALETTE["surface-3"]}" stroke-width="9"/>',
        f'<circle cx="{center}" cy="{center}" r="{radius:.1f}" fill="none" '
        f'stroke="{color}" stroke-width="9" stroke-linecap="round" '
        f'stroke-dasharray="{filled:.2f} {circumference - filled:.2f}" '
        f'transform="rotate(-90 {center} {center})"/>',
        f'<text x="{center}" y="{center - (2 if caption else 0)}" '
        f'text-anchor="middle" dominant-baseline="middle" '
        f'style="fill:{PALETTE["text"]};font:700 21px {PALETTE["display"]};'
        f'letter-spacing:-.5px;font-variant-numeric:tabular-nums">'
        f"%{clamped:.1f}</text>",
    ]
    if caption:
        parts.append(
            f'<text x="{center}" y="{center + 18}" text-anchor="middle" '
            f'class="label">{escape(caption)}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)
