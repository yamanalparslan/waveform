"""Tema bütünlüğü: CSS ile Python paleti ayrışmasın.

Bu dosyadaki testler bir tasarım kararını koruyor: renkler yalnızca
`web/theme.py`'de tanımlıdır, `app.css` sadece `var(--token)` kullanır ve SVG
grafikler aynı sözlükten beslenir. Ayrışma sessizdir — tanımsız bir CSS
değişkeni hata vermez, o kural hiç uygulanmaz; grafiğin çizgisi bir mavi,
gösterge etiketi başka bir mavi olur.
"""

import re

import pytest

from luminmind.analytics.insights import PRIORITY_ORDER
from luminmind.web import charts, theme

CSS = theme.CSS_PATH.read_text(encoding="utf-8")

# Yorumları çıkar: açıklama metnindeki örnek kodlar kural sayılmasın.
CSS_RULES = re.sub(r"/\*.*?\*/", "", CSS, flags=re.DOTALL)

VAR_PATTERN = re.compile(r"var\(--([a-z0-9-]+)\)")
DECL_PATTERN = re.compile(r"--([a-z0-9-]+)\s*:")
HEX_PATTERN = re.compile(r"#[0-9a-fA-F]{3,8}\b")


def test_every_css_variable_is_defined_in_the_palette():
    used = set(VAR_PATTERN.findall(CSS_RULES))
    missing = sorted(used - set(theme.PALETTE))
    assert not missing, f"palette eksik token: {missing}"


def test_css_declares_no_variables_of_its_own():
    """Palet tek yönlü akmalı; CSS'te `--x:` görülürse ikinci kaynak doğmuş."""
    assert not DECL_PATTERN.findall(CSS_RULES)


def test_css_contains_no_literal_colours():
    found = HEX_PATTERN.findall(CSS_RULES)
    assert not found, f"app.css içinde sabit renk kodu: {found}"


def test_root_block_declares_the_whole_palette():
    block = theme.css_root_block()
    assert block.startswith(":root{") and block.endswith("}")
    for name, value in theme.PALETTE.items():
        assert f"--{name}:{value};" in block


def test_root_block_covers_every_token_the_css_needs():
    """base.html bu bloğu gömer; eksik token doğrudan bozuk görünüm demek."""
    block = theme.css_root_block()
    for token in set(VAR_PATTERN.findall(CSS_RULES)):
        assert f"--{token}:" in block


# ------------------------------ grafik renkleri ------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "CHART_ACTUAL",
        "CHART_EXPECTED",
        "CHART_BAND",
        "CHART_TEMPERATURE",
        "CHART_PRICE",
        "CHART_REVENUE",
        "CHART_CHARGE",
        "CHART_DISCHARGE",
        "CHART_IDLE",
        "CHART_EMPTY_TEXT",
    ],
)
def test_chart_colours_come_from_the_palette(name):
    value = getattr(theme, name)
    assert value in theme.PALETTE.values(), f"{name} palet dışı bir renk kullanıyor"


def test_charts_module_holds_no_colour_of_its_own():
    """SVG üreticileri renk uydurursa CSS'le ayrışır."""
    source = charts.__file__
    with open(source, encoding="utf-8") as handle:
        body = handle.read()
    assert not HEX_PATTERN.findall(body)


def test_actual_and_expected_are_distinguishable():
    """Aynı grafikte üst üste çizilen iki seri aynı renk olmamalı."""
    assert theme.CHART_ACTUAL != theme.CHART_EXPECTED
    assert theme.CHART_CHARGE != theme.CHART_DISCHARGE


# ------------------------------ öncelik renkleri ------------------------------


def test_every_priority_has_a_colour():
    for priority in PRIORITY_ORDER:
        assert theme.priority_color(priority) in theme.PALETTE.values()


def test_priority_colours_are_distinct():
    colours = {theme.priority_color(p) for p in PRIORITY_ORDER}
    assert len(colours) == len(PRIORITY_ORDER)


def test_unknown_priority_falls_back_to_neutral():
    assert theme.priority_color("bilinmeyen") == theme.PALETTE["slate"]
    assert theme.chip_color("bilinmeyen") == theme.PALETTE["slate"]


def test_chip_colours_cover_the_status_vocabulary():
    for chip in ("ok", "warn", "crit", "info", "muted"):
        assert theme.chip_color(chip) in theme.PALETTE.values()


# ------------------------------ şablonlar ------------------------------
# Şablonlarda satır içi stil kaçınılmaz (tek seferlik hizalamalar), ama renk
# oradan da paletten gelmeli. Paletten kalkmış bir token sessizce renksiz
# render edilir; aşağıdaki iki test o sessizliği kırıyor.

TEMPLATE_DIR = theme.STATIC_DIR.parent / "templates"
TEMPLATES = sorted(TEMPLATE_DIR.glob("*.html"))


def template_bodies():
    for path in TEMPLATES:
        text = path.read_text(encoding="utf-8")
        # Jinja yorumlarını çıkar: açıklama metni kural sayılmasın
        yield path.name, re.sub(r"\{#.*?#\}", "", text, flags=re.DOTALL)


def test_templates_exist():
    assert len(TEMPLATES) >= 10


def test_every_template_variable_is_defined_in_the_palette():
    missing: dict[str, list[str]] = {}
    for name, body in template_bodies():
        unknown = sorted(set(VAR_PATTERN.findall(body)) - set(theme.PALETTE))
        if unknown:
            missing[name] = unknown
    assert not missing, f"palette olmayan token: {missing}"


def test_templates_carry_no_literal_colours():
    """Sabit renk kodu, tema değiştiğinde geride kalan tek şeydir."""
    offenders = {
        name: HEX_PATTERN.findall(body) for name, body in template_bodies()
        if HEX_PATTERN.findall(body)
    }
    assert not offenders, f"şablonda sabit renk kodu: {offenders}"


def test_base_template_wires_the_palette_before_the_stylesheet():
    """Sıra ters olursa `var(--x)` tanımsızken uygulanır ve sayfa çıplak açılır."""
    body = (TEMPLATE_DIR / "base.html").read_text(encoding="utf-8")
    assert body.index("{{ theme_css }}") < body.index('href="/static/app.css"')


# ------------------------------ statik dosya ------------------------------


def test_static_directory_holds_the_stylesheet():
    assert theme.STATIC_DIR.is_dir()
    assert theme.CSS_PATH.is_file()
    assert theme.CSS_PATH.parent == theme.STATIC_DIR


# ------------------------------ tipografi ------------------------------
# Yüz dosyaları sessiz kırılan türden: eksik bir woff2 yalnızca 404 verir,
# sayfa Segoe UI ile açılır ve kimse fark etmez. Aşağıdakiler o sessizliği kırar.


@pytest.mark.parametrize("filename", theme.FONT_FILES)
def test_font_files_are_present_on_disk(filename):
    path = theme.STATIC_DIR / filename
    assert path.is_file(), f"{filename} yok — sayfa yedek yüze düşer"
    assert path.read_bytes()[:4] == b"wOF2", f"{filename} geçerli bir woff2 değil"


@pytest.mark.parametrize("filename", theme.FONT_FILES)
def test_css_loads_every_font_file(filename):
    assert f'url("/static/{filename}")' in CSS_RULES


def test_font_face_urls_all_point_at_known_files():
    """CSS'te diskte olmayan bir dosyaya `url()` kalmasın."""
    referenced = set(re.findall(r'url\("/static/([^"]+)"\)', CSS_RULES))
    assert referenced == set(theme.FONT_FILES)


def test_font_faces_cover_turkish_characters():
    """ı `latin` alt kümesinde, İ/ğ/ş ve ₺ `latin-ext`te — ikisi de gerekli."""
    ranges = " ".join(re.findall(r"unicode-range:([^;]+);", CSS_RULES))
    assert "U+0131" in ranges  # ı
    assert "U+0100-02BA" in ranges  # İ, ğ, ş bu bloğun içinde
    assert "U+20AD-20C0" in ranges  # ₺ (U+20BA)


@pytest.mark.parametrize("token", ["sans", "display", "mono"])
def test_typography_stacks_end_in_a_generic_family(token):
    """Yığının sonu jenerik aile olmazsa hiçbir yüz bulunamayınca tarayıcı
    varsayılanına değil, tanımsız davranışa düşer."""
    assert theme.PALETTE[token].rstrip().endswith(("sans-serif", "monospace"))


def test_apple_system_font_comes_first_in_the_ui_stacks():
    """SF Pro yayınlanamıyor; Apple cihazlarda tek yolu sistem yüzü çağırmak."""
    for token in ("sans", "display"):
        assert theme.PALETTE[token].startswith("-apple-system")


def test_self_hosted_face_is_in_the_ui_stacks():
    """Apple dışındaki sistemlerde SF'nin yerini `@font-face` ile inen Inter alır."""
    for token in ("sans", "display"):
        assert "Inter" in theme.PALETTE[token]
    assert "font-family:Inter;" in CSS_RULES.replace(" ", "")


def test_stylesheet_is_shipped_as_package_data():
    """Tekerlek/imaj `web/static/*` taşımazsa sayfa stilsiz açılır ve hata vermez."""
    root = theme.STATIC_DIR.parents[3]  # …/src/luminmind/web/static → repo kökü
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert '"web/static/*"' in pyproject
