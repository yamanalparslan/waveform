"""Sunucu tarafında üretilen SVG grafikler.

Harici JS grafik kütüphanesi yerine saf Python: bağımlılık yok, çevrimdışı
çalışır, birim test edilebilir. Zaman ekseni çağıranın verdiği zaman diliminde
(UI'da TRT) etiketlenir.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
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


def _nice_num(x: float, round_result: bool) -> float:
    """Klasik "nice numbers" algoritması — 1/2/2.5/5/10 katlarına yuvarlar."""
    if x <= 0:
        return 1.0
    exp = math.floor(math.log10(x))
    f = x / 10 ** exp
    if round_result:
        nice_f = 1 if f < 1.5 else 2 if f < 3 else 5 if f < 7 else 10
    else:
        nice_f = 1 if f <= 1 else 2 if f <= 2 else 5 if f <= 5 else 10
    return nice_f * 10 ** exp


def _nice_ticks(v_max: float, target: int = 5) -> tuple[float, list[float]]:
    """0..v_max aralığı için nice tick değerleri döndürür; ölçek üstünü de büyütür."""
    if v_max <= 0:
        return 1.0, [0.0, 0.25, 0.5, 0.75, 1.0]
    span = _nice_num(v_max, False)
    step = _nice_num(span / max(target - 1, 1), True)
    nice_max = math.ceil(v_max / step) * step
    ticks: list[float] = []
    v = 0.0
    while v <= nice_max + 1e-9:
        ticks.append(round(v, 10))
        v += step
    return nice_max, ticks


def _fmt_axis(value: float, step: float) -> str:
    """Tick etiketini adım büyüklüğüne göre uygun ondalıkla biçimler."""
    if step >= 100:
        return f"{value:,.0f}".replace(",", ".")
    if step >= 10:
        return f"{value:,.0f}".replace(",", ".")
    if step >= 1:
        # 1..10 arası: gerekiyorsa bir ondalık
        return f"{value:.1f}" if value != int(value) else f"{int(value)}"
    if step >= 0.1:
        return f"{value:.1f}"
    return f"{value:.2f}"


def _hour_ticks(
    t_min: float, t_max: float, zone: tzinfo, target: int = 6
) -> list[float]:
    """X ekseni için tam-saat sınırlarına oturan zaman damgaları."""
    if t_max <= t_min:
        return [t_min]
    span_h = (t_max - t_min) / 3600.0
    for step_h in (1, 2, 3, 4, 6, 12, 24):
        if span_h / step_h <= target:
            break
    else:
        step_h = 24
    start_dt = datetime.fromtimestamp(t_min, tz=zone).replace(
        minute=0, second=0, microsecond=0
    )
    if start_dt.timestamp() < t_min:
        start_dt += timedelta(hours=1)
    # step_h'a göre saat sınırına hizala (00, 02, 04... gibi)
    align = start_dt.hour % step_h
    if align:
        start_dt += timedelta(hours=step_h - align)
    ticks: list[float] = []
    cur = start_dt
    while cur.timestamp() <= t_max + 1e-6:
        ticks.append(cur.timestamp())
        cur += timedelta(hours=step_h)
    if not ticks:
        ticks = [t_min, t_max]
    return ticks


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


def daily_bar_chart(
    points: list[tuple[datetime, float]],
    zone: tzinfo,
    unit: str = "kWh",
    color: str = "#f2b544",
    width: int = _WIDTH,
    height: int = _HEIGHT,
) -> str:
    """Günlük toplam için dikey bar grafiği (gün başına bir bar).

    Günlük üretim ayrık bir büyüklük — çizgi yerine bar dürüst gösterimdir.
    X ekseni tarih etiketli (gg.aa); çok gün varsa etiketler seyreltilir.
    Her barın <title>'ı tam değeri taşır (tarayıcı tooltip'i).
    """
    if not points:
        return _empty(width, height, "Veri yok")
    pts = sorted(points)
    raw_max = max(v for _, v in pts)
    v_max, y_ticks = _nice_ticks(raw_max, target=5)
    step = y_ticks[1] - y_ticks[0] if len(y_ticks) >= 2 else 1.0

    x0, x1 = _PAD_LEFT, width - _PAD_RIGHT
    y0, y1 = height - _PAD_BOTTOM, _PAD_TOP

    parts: list[str] = [
        f'<svg viewBox="0 0 {width} {height}" class="chart" role="img">',
    ]
    # y ekseni ızgarası + etiketleri (nice-tick)
    for value in y_ticks:
        y = _scale(value, 0.0, v_max, y0, y1)
        parts.append(
            f'<line x1="{x0}" y1="{y:.1f}" x2="{x1}" y2="{y:.1f}" class="grid"/>'
            f'<text x="{x0 - 8}" y="{y + 4:.1f}" text-anchor="end" class="tick">'
            f"{escape(_fmt_axis(value, step))}</text>"
        )
    parts.append(
        f'<text x="14" y="{_PAD_TOP + 10}" class="tick unit">{escape(unit)}</text>'
    )

    # bar yerleşimi — eşit aralıklı slotlar, slotun %64'ü bar
    n = len(pts)
    slot_w = (x1 - x0) / n
    bar_w = slot_w * 0.64
    # etiket seyreltme: en çok ~10 etiket
    label_stride = max(1, math.ceil(n / 10))

    for i, (ts, v) in enumerate(pts):
        cx = x0 + slot_w * (i + 0.5)
        bx = cx - bar_w / 2
        by = _scale(v, 0.0, v_max, y0, y1)
        bh = max(0.0, y0 - by)
        parts.append(
            f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bar_w:.1f}" height="{bh:.1f}" '
            f'rx="3" fill="{color}" class="bar">'
            f"<title>{escape(_fmt_axis(v, step))} {escape(unit)}</title></rect>"
        )
        if i % label_stride == 0 or i == n - 1:
            label = datetime.fromtimestamp(ts.timestamp(), tz=zone).strftime("%d.%m")
            parts.append(
                f'<text x="{cx:.1f}" y="{height - 12}" text-anchor="middle" '
                f'class="tick">{label}</text>'
            )
    parts.append("</svg>")
    return "".join(parts)


def line_chart(
    series: list[Series],
    zone: tzinfo,
    unit: str = "kW",
    width: int = _WIDTH,
    height: int = _HEIGHT,
) -> str:
    """Çok serili çizgi grafik.

    - Y ekseni her zaman 0'dan başlar; üst sınır "nice number"a yuvarlanır.
    - Y tick etiketleri veri aralığına göre 0/1/2 ondalık gösterir.
    - X ekseni tam saat (veya 2/3/6/12 saat) sınırlarına oturur — "05:27"
      yerine "05:00, 06:00, 07:00" gibi okunabilir işaretler.
    - Az veri (≤12 nokta) olduğunda her ölçüme marker konur; tek nokta da
      görünür kalır. Ayrıca her serinin maksimumuna yakın bir tepe noktası
      küçük halka ile vurgulanır.
    """
    all_points = [p for s in series for p in s.points]
    if not all_points:
        return _empty(width, height, "Veri yok")

    t_min = min(ts for ts, _ in all_points).timestamp()
    t_max = max(ts for ts, _ in all_points).timestamp()
    raw_max = max((v for _, v in all_points), default=0.0)
    v_max, y_ticks = _nice_ticks(raw_max, target=5)
    step = y_ticks[1] - y_ticks[0] if len(y_ticks) >= 2 else 1.0

    x0, x1 = _PAD_LEFT, width - _PAD_RIGHT
    y0, y1 = height - _PAD_BOTTOM, _PAD_TOP

    parts: list[str] = [
        f'<svg viewBox="0 0 {width} {height}" class="chart" role="img">',
    ]
    # y ekseni ızgarası + etiketleri (nice-tick)
    for value in y_ticks:
        y = _scale(value, 0.0, v_max, y0, y1)
        parts.append(
            f'<line x1="{x0}" y1="{y:.1f}" x2="{x1}" y2="{y:.1f}" class="grid"/>'
            f'<text x="{x0 - 8}" y="{y + 4:.1f}" text-anchor="end" class="tick">'
            f"{escape(_fmt_axis(value, step))}</text>"
        )
    parts.append(
        f'<text x="14" y="{_PAD_TOP + 10}" class="tick unit">{escape(unit)}</text>'
    )
    # x ekseni tam-saat sınırları
    for t in _hour_ticks(t_min, t_max, zone):
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
        sorted_pts = sorted(s.points)
        pts_scaled = [
            (
                _scale(ts.timestamp(), t_min, t_max, x0, x1) if t_max > t_min
                else (x0 + x1) / 2,
                _scale(v, 0.0, v_max, y0, y1),
            )
            for ts, v in sorted_pts
        ]
        if len(pts_scaled) >= 2:
            coords = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts_scaled)
            parts.append(
                f'<polyline points="{coords}" fill="none" stroke="{s.color}" '
                f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
            )
        # az nokta okunaksız — her noktaya marker koy (tek nokta da görünsün)
        if len(pts_scaled) <= 12:
            for x, y in pts_scaled:
                parts.append(
                    f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{s.color}"/>'
                )
        # tepe noktasını halka ile vurgula (yeterli veri ve gerçek bir tepe varsa)
        elif len(pts_scaled) >= 6:
            peak_idx = max(range(len(sorted_pts)), key=lambda i: sorted_pts[i][1])
            if sorted_pts[peak_idx][1] > 0:
                px, py = pts_scaled[peak_idx]
                parts.append(
                    f'<circle cx="{px:.1f}" cy="{py:.1f}" r="4" fill="none" '
                    f'stroke="{s.color}" stroke-width="1.5"/>'
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
