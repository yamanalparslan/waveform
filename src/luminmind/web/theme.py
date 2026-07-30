"""Arayüzün tek renk kaynağı.

Palet neden Python'da? Çünkü renkler **iki** yerde kullanılıyor: tarayıcıdaki
CSS ve sunucuda üretilen SVG grafikler. Grafikler harici bir JS kütüphanesi
kullanmadığı için renkleri `charts.py` içinde satır içi yazmak zorundaydı; palet
CSS'te ayrıca tanımlıydı. İkisi kaçınılmaz olarak ayrışıyordu — grafiğin çizgisi
bir mavi, altındaki gösterge etiketinin arka planı başka bir mavi oluyordu.

Şimdi tek yön var: `PALETTE` → (`css_root_block()` ile) tarayıcı, (`CHART_*`
sabitleriyle) SVG. Bir rengi değiştirmek tek satır.

Yerleşim, bileşen ve duyarlılık kuralları `static/app.css`'te durur ve yalnızca
`var(--token)` kullanır; sabit bir renk kodu yazmaz. `tests/unit/test_theme.py`
CSS'teki her `var(--token)`'ın burada tanımlı olduğunu doğrular — aksi halde
tanımsız değişken sessizce "renk yok" olarak render edilir.
"""

from pathlib import Path

from luminmind.analytics.insights import (
    PRIORITY_IMMEDIATE,
    PRIORITY_LONG,
    PRIORITY_MID,
)

# `/static` altında sunulan dizin. Paket verisi olarak dağıtılır.
STATIC_DIR = Path(__file__).parent / "static"
CSS_PATH = STATIC_DIR / "app.css"

# CSS özel değişken adları (başındaki `--` olmadan) → değer.
PALETTE: dict[str, str] = {
    # --- zemin ve yüzeyler: açık tema ---
    "bg": "#f4f6fb",
    "surface": "#ffffff",
    "surface-2": "#f8fafc",
    "surface-3": "#eef2f8",
    "line": "#e3e8f0",
    "line-soft": "#edf1f7",
    # --- lacivert kenar çubuğu ---
    "nav": "#1b2a6b",
    "nav-2": "#152156",  # gradyanın alt ucu
    "nav-line": "#ffffff24",
    "nav-text": "#c3cffb",
    "nav-text-strong": "#ffffff",
    "nav-active": "#ffffff1f",
    "nav-hover": "#ffffff14",
    # --- metin ---
    "text": "#0f172a",
    "text-dim": "#54617a",
    # Eskiden #8794ab idi: beyaz üstünde ~3.2:1 kontrast, 11 px etiketlerde
    # okunmuyordu. #66748c ~4.7:1 verir (WCAG AA sınırı 4.5:1).
    "text-faint": "#66748c",
    "text-invert": "#ffffff",
    # --- vurgular ---
    "blue": "#2563eb",
    "blue-soft": "#2563eb14",
    "blue-line": "#2563eb40",
    "red": "#e11d48",
    "red-soft": "#e11d4814",
    "red-line": "#e11d4840",
    "amber": "#f59e0b",
    "amber-soft": "#f59e0b1f",
    "amber-line": "#f59e0b4d",
    "yellow": "#fbbf24",
    "yellow-soft": "#fbbf2426",
    "green": "#059669",
    "green-soft": "#0596691a",
    "green-line": "#05966940",
    "violet": "#7c3aed",
    "violet-soft": "#7c3aed14",
    "slate": "#94a3b8",
    # --- geometri ---
    "r-sm": "6px",
    "r-md": "10px",
    "r-lg": "14px",
    "r-xl": "20px",
    "shadow-sm": "0 1px 2px #0f172a0f",
    "shadow-md": "0 4px 12px -4px #0f172a1f, 0 1px 3px #0f172a14",
    "shadow-lg": "0 18px 40px -18px #0f172a2e, 0 2px 8px -2px #0f172a14",
    # --- tipografi ---
    # Apple'ın San Francisco'su (SF Pro) web'e gömülemez: lisansı yalnızca Apple
    # platformlarındaki uygulama arayüzlerini kapsar, bir sunucudan yayınlamayı
    # kapsamaz. Bu yüzden iki yol birlikte kullanılıyor:
    #   • Mac/iPhone/iPad'de `-apple-system` gerçek SF Pro'yu getirir.
    #   • Diğer sistemlerde SF'ye ölçü ve karakter olarak en yakın açık lisanslı
    #     yüz olan Inter, `static/inter-*.woff2` dosyalarından iner (bkz.
    #     app.css içindeki `@font-face`). Harici bir CDN'e bağlanmadığı için
    #     internete kapalı sahalarda da aynı görünür.
    # `display` metin gövdesinden ayrı durur: Apple'da SF Pro Display (başlıklar
    # için sıkı harf aralıklı optik kesim), diğer sistemlerde aynı Inter.
    "sans": '-apple-system, BlinkMacSystemFont, "SF Pro Text", Inter, "Segoe UI",'
    ' Roboto, "Helvetica Neue", Arial, sans-serif',
    "display": '-apple-system, BlinkMacSystemFont, "SF Pro Display", Inter,'
    ' "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif',
    "mono": '"SF Mono", ui-monospace, "JetBrains Mono", Menlo, Consolas,'
    ' "Liberation Mono", monospace',
}

# `app.css` içindeki `@font-face` kurallarının indirdiği dosyalar. Testler bu
# listeyi hem diskte hem CSS'te arar: eksik bir dosya 404 verir, sayfa sessizce
# Segoe UI'ya düşer ve fark edilmez.
FONT_FILES: tuple[str, ...] = (
    "inter-latin-wght-normal.woff2",
    "inter-latin-ext-wght-normal.woff2",
)


def css_root_block() -> str:
    """Paleti `:root{…}` bloğuna çevirir; `base.html` satır içi gömer.

    Statik dosyaya yazılamaz: `app.css` tarayıcıda önbelleğe alınır ve palet
    Python'da yaşar. Bu blok küçüktür (~1 kB), asıl stil hacmi `app.css`'te
    kalır ve önbelleklenebilir.
    """
    body = "".join(f"--{name}:{value};" for name, value in PALETTE.items())
    return f":root{{{body}}}"


# ------------------------------ grafik renkleri ------------------------------
# SVG üreticileri bu sabitleri kullanır; CSS ile aynı değerler.

CHART_ACTUAL = PALETTE["blue"]  # gerçekleşen üretim
CHART_EXPECTED = PALETTE["violet"]  # dijital ikizin beklentisi
CHART_BAND = PALETTE["violet"]  # P10–P90 belirsizlik bandı (düşük opaklıkla)
CHART_TEMPERATURE = PALETTE["amber"]
CHART_PRICE = PALETTE["amber"]
CHART_REVENUE = PALETTE["green"]
CHART_CHARGE = PALETTE["green"]
CHART_DISCHARGE = PALETTE["red"]
CHART_IDLE = PALETTE["slate"]
CHART_EMPTY_TEXT = PALETTE["text-faint"]

# Aynı grafikte birden çok saha/cihaz çizilirken sırayla kullanılan renkler.
SERIES_COLORS: tuple[str, ...] = (
    PALETTE["blue"],
    PALETTE["green"],
    PALETTE["amber"],
    PALETTE["violet"],
    PALETTE["red"],
    PALETTE["slate"],
)


def series_color(index: int) -> str:
    """Sıradaki seri rengi; liste bitince başa döner."""
    return SERIES_COLORS[index % len(SERIES_COLORS)]

# Anomali/aksiyon önceliği → renk. `insights` modülündeki sabitler anahtar
# olarak kullanılır; yeni bir öncelik eklenirse burada da eksikliği fark edilir.
PRIORITY_COLORS: dict[str, str] = {
    PRIORITY_IMMEDIATE: PALETTE["red"],
    PRIORITY_MID: PALETTE["amber"],
    PRIORITY_LONG: PALETTE["yellow"],
}

# Durum çipi adı → renk (arayüzdeki `ok/warn/crit/info/muted` sınıflarıyla aynı).
CHIP_COLORS: dict[str, str] = {
    "ok": PALETTE["green"],
    "warn": PALETTE["amber"],
    "crit": PALETTE["red"],
    "info": PALETTE["blue"],
    "muted": PALETTE["slate"],
}


def priority_color(priority: str) -> str:
    """Öncelik rengi; bilinmeyen öncelikte nötr griye düşer."""
    return PRIORITY_COLORS.get(priority, PALETTE["slate"])


def chip_color(chip: str) -> str:
    """Durum çipi rengi; bilinmeyen çipte nötr griye düşer."""
    return CHIP_COLORS.get(chip, PALETTE["slate"])
