"""Çatı/arazi poligonu geometrisi: WGS84 ↔ metre ve panel yerleşim yüklemleri.

Kullanıcı çatıyı harita üzerine enlem/boylam olarak çizer; panel paketleme ise
metre cinsinden düzlemde yapılmak zorundadır. Bu dosya iki dünyayı birleştirir
ve yerleşim algoritmasının dayandığı geometrik yüklemleri taşır.

**Neden harici geometri kütüphanesi yok.** İlk tasarımda `shapely` düşünüldü;
gerekçe "poligonu kenar mesafesi kadar içe ötele, sonra panelin içinde olup
olmadığına bak" idi. Bu formülasyon terk edildi: konkav çatılarda negatif
tampon (`buffer(-d)`) poligonu çok parçaya bölebilir, öz-kesişim üretebilir ve
dar boğazlarda sessizce alan kaybeder. Onun yerine doğrudan yüklem kuruluyor —
*"bu dikdörtgen poligonun içinde ve her kenarından en az `setback` uzakta mı?"*
Bu soru içe öteleme gerektirmez, sayısal olarak daha kararlıdır ve tam olarak
cevabı aranan sorudur. Bedeli ~80 satır bilinen hesaplamalı geometri; kazancı
bir C bağımlılığından ve onun kenar durumlarından kurtulmak.

**Yerel teğet düzlem.** Ölçek çatı için birkaç on metre, arazi için birkaç yüz
metre. Bu ölçekte poligon merkezine oturtulmuş teğet düzlem (enlem için
meridyen eğrilik yarıçapı, boylam için dikey kesit yarıçapı) santimetre
mertebesinde doğrudur. Küresel yaklaşım (`R = 6371 km`) yerine WGS84 elipsoid
yarıçapları kullanılır: fark Türkiye enlemlerinde binde bir mertebesinde, yani
100 m'lik bir çatıda ~10 cm — panel sayısını değiştirmez ama alan raporunda
görünür ve bedava kaçınılabilir bir hata.
"""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

# WGS84 elipsoidi
_WGS84_A = 6378137.0  # yarı büyük eksen (m)
_WGS84_F = 1.0 / 298.257223563  # basıklık
_WGS84_E2 = _WGS84_F * (2.0 - _WGS84_F)  # birinci dışmerkezlik karesi

# Kayan nokta karşılaştırmalarında kenar üstü durumlar için tolerans (m).
# Harita tıklamasının çözünürlüğü zaten santimetrenin çok üstünde.
_EPS = 1e-9

Point = tuple[float, float]
"""Yerel düzlemde (x=doğu, y=kuzey) metre cinsinden nokta."""

Ring = tuple[Point, ...]
"""Kapalı poligon halkası — son nokta ilkine *eşit değil*, kapanış örtüktür."""


@dataclass(frozen=True)
class LatLon:
    """WGS84 coğrafi koordinat."""

    lat: float
    lon: float


@dataclass(frozen=True)
class LocalFrame:
    """Bir referans noktasına oturtulmuş yerel teğet düzlem (ENU).

    Dönüşüm doğrusaldır: enlem/boylam farkı sabit ölçeklerle çarpılır. Ölçekler
    referans enlemindeki eğrilik yarıçaplarından türetildiği için düzlem yalnızca
    o enlem civarında geçerlidir — bu yüzden `centered_on` poligonun kendi
    merkezini kullanır.
    """

    origin_lat: float
    origin_lon: float
    m_per_deg_lat: float
    m_per_deg_lon: float

    @classmethod
    def at(cls, lat: float, lon: float) -> "LocalFrame":
        phi = math.radians(lat)
        sin2 = math.sin(phi) ** 2
        w = 1.0 - _WGS84_E2 * sin2
        # Meridyen (M) ve dikey kesit (N) eğrilik yarıçapları
        radius_meridian = _WGS84_A * (1.0 - _WGS84_E2) / (w**1.5)
        radius_normal = _WGS84_A / math.sqrt(w)
        return cls(
            origin_lat=lat,
            origin_lon=lon,
            m_per_deg_lat=radius_meridian * math.pi / 180.0,
            m_per_deg_lon=radius_normal * math.cos(phi) * math.pi / 180.0,
        )

    @classmethod
    def centered_on(cls, points: Sequence[LatLon]) -> "LocalFrame":
        """Nokta kümesinin ortalama konumuna oturtulmuş düzlem."""
        if not points:
            raise ValueError("LocalFrame için en az bir nokta gerekir")
        lat = sum(p.lat for p in points) / len(points)
        lon = sum(p.lon for p in points) / len(points)
        return cls.at(lat, lon)

    def to_local(self, point: LatLon) -> Point:
        return (
            (point.lon - self.origin_lon) * self.m_per_deg_lon,
            (point.lat - self.origin_lat) * self.m_per_deg_lat,
        )

    def to_wgs84(self, point: Point) -> LatLon:
        return LatLon(
            lat=self.origin_lat + point[1] / self.m_per_deg_lat,
            lon=self.origin_lon + point[0] / self.m_per_deg_lon,
        )

    def ring_to_local(self, points: Sequence[LatLon]) -> Ring:
        return tuple(self.to_local(p) for p in points)

    def ring_to_wgs84(self, ring: Sequence[Point]) -> tuple[LatLon, ...]:
        return tuple(self.to_wgs84(p) for p in ring)


# --- Temel poligon büyüklükleri -------------------------------------------------


def signed_area(ring: Ring) -> float:
    """Ayakkabı bağı formülü. Pozitif = saat yönünün tersi (CCW)."""
    if len(ring) < 3:
        return 0.0
    total = 0.0
    for i in range(len(ring)):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % len(ring)]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def polygon_area_m2(ring: Ring) -> float:
    return abs(signed_area(ring))


def polygon_perimeter_m(ring: Ring) -> float:
    if len(ring) < 2:
        return 0.0
    return sum(
        math.dist(ring[i], ring[(i + 1) % len(ring)]) for i in range(len(ring))
    )


def polygon_centroid(ring: Ring) -> Point:
    """Alan merkezi. Dejenere (sıfır alanlı) halkalarda köşe ortalamasına düşer."""
    area = signed_area(ring)
    if abs(area) < _EPS:
        n = len(ring) or 1
        return (sum(p[0] for p in ring) / n, sum(p[1] for p in ring) / n)
    cx = cy = 0.0
    for i in range(len(ring)):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % len(ring)]
        cross = x1 * y2 - x2 * y1
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    return (cx / (6.0 * area), cy / (6.0 * area))


def normalize_ring(ring: Ring) -> Ring:
    """Yönü CCW'ye çevirir ve tekrarlanan kapanış noktasını atar.

    Yerleşim algoritması yön varsayımı yapmaz, ama alan işareti ve kenar
    normalleri tutarlı yön ister; girdi kullanıcıdan geldiği için garanti yok.
    """
    points = list(ring)
    while len(points) > 1 and math.dist(points[0], points[-1]) < _EPS:
        points.pop()
    cleaned: list[Point] = []
    for p in points:
        if not cleaned or math.dist(cleaned[-1], p) > _EPS:
            cleaned.append(p)
    result = tuple(cleaned)
    return result if signed_area(result) >= 0.0 else tuple(reversed(result))


def bounding_box(ring: Ring) -> tuple[float, float, float, float]:
    """(min_x, min_y, max_x, max_y)."""
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return (min(xs), min(ys), max(xs), max(ys))


# --- Yüklemler ------------------------------------------------------------------


def _cross(o: Point, a: Point, b: Point) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def point_in_ring(point: Point, ring: Ring) -> bool:
    """Işın atma (ray casting) ile içeride mi testi.

    Yarı-açık kural (`y1 <= y < y2`) kullanılır: köşe hizasındaki ışınların iki
    kez sayılmasını engeller, aksi halde bir köşeden geçen yatay hizada sonuç
    kararsız olur.
    """
    x, y = point
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 <= y < y2) or (y2 <= y < y1):
            t = (y - y1) / (y2 - y1)
            if x < x1 + t * (x2 - x1):
                inside = not inside
    return inside


def point_to_segment_distance(point: Point, a: Point, b: Point) -> float:
    px, py = point
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq < _EPS:
        return math.dist(point, a)
    t = ((px - ax) * dx + (py - ay) * dy) / length_sq
    t = max(0.0, min(1.0, t))
    return math.dist(point, (ax + t * dx, ay + t * dy))


def segments_intersect(p1: Point, p2: Point, q1: Point, q2: Point) -> bool:
    """İki kapalı doğru parçası kesişiyor mu (uç değme ve eşdoğrusal örtüşme dahil)."""
    d1 = _cross(q1, q2, p1)
    d2 = _cross(q1, q2, p2)
    d3 = _cross(p1, p2, q1)
    d4 = _cross(p1, p2, q2)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        if abs(d1) > _EPS and abs(d2) > _EPS and abs(d3) > _EPS and abs(d4) > _EPS:
            return True
    # Eşdoğrusal / uç değme: mesafeye bak, işaret testi burada güvenilmez
    return (
        point_to_segment_distance(p1, q1, q2) < _EPS
        or point_to_segment_distance(p2, q1, q2) < _EPS
        or point_to_segment_distance(q1, p1, p2) < _EPS
        or point_to_segment_distance(q2, p1, p2) < _EPS
    )


def segment_distance(p1: Point, p2: Point, q1: Point, q2: Point) -> float:
    """İki doğru parçası arasındaki en kısa mesafe (kesişiyorlarsa 0)."""
    if segments_intersect(p1, p2, q1, q2):
        return 0.0
    return min(
        point_to_segment_distance(p1, q1, q2),
        point_to_segment_distance(p2, q1, q2),
        point_to_segment_distance(q1, p1, p2),
        point_to_segment_distance(q2, p1, p2),
    )


def _edges(ring: Ring) -> Iterable[tuple[Point, Point]]:
    n = len(ring)
    for i in range(n):
        yield ring[i], ring[(i + 1) % n]


def ring_to_ring_distance(a: Ring, b: Ring) -> float:
    """İki halkanın sınırları arasındaki en kısa mesafe (kesişiyorlarsa 0)."""
    best = math.inf
    for a1, a2 in _edges(a):
        for b1, b2 in _edges(b):
            best = min(best, segment_distance(a1, a2, b1, b2))
            if best <= _EPS:
                return 0.0
    return best


def distance_point_to_ring(point: Point, ring: Ring) -> float:
    """Noktadan halka *sınırına* en kısa mesafe (içeride/dışarıda olması fark etmez).

    Yerleşimde hızlı yol için var: bir panelin merkezi içerideyse ve sınırdan
    `setback + yarı köşegen` kadar uzaktaysa panelin tamamı kesinlikle içeride ve
    kenar mesafesi sağlanmıştır — pahalı kesişme/mesafe testine hiç girilmez.
    Simetrik olarak merkez dışarıda ve sınırdan yarı köşegenden uzaksa panel
    kesinlikle dışarıdadır. Yalnızca sınıra yakın paneller tam testi hak eder.
    """
    return min(point_to_segment_distance(point, a, b) for a, b in _edges(ring))


def ring_is_simple(ring: Ring) -> bool:
    """Halka öz-kesişimsiz mi.

    Kullanıcı haritada poligon çizerken kolayca kelebek şekli üretir; öz-kesişen
    bir halkada alan ve "içinde mi" sorusu anlamsızlaşır (ayakkabı bağı formülü
    kesişen bölgeyi negatif sayar, ışın atma testi kararsızlaşır). Yerleşimden
    önce burada durdurulur ki kullanıcı düzeltebilsin — sessizce yanlış m²
    raporlamaktan iyidir.
    """
    n = len(ring)
    if n < 3:
        return False
    edges = list(_edges(ring))
    for i in range(n):
        for j in range(i + 1, n):
            # Komşu kenarlar ortak köşede zaten değer; kapanış çifti de komşudur
            if j == i + 1 or (i == 0 and j == n - 1):
                continue
            if segments_intersect(*edges[i], *edges[j]):
                return False
    return True


def _shrink_convex(convex: Ring, relative: float = 1e-7) -> Ring:
    """Konveks halkayı merkezine doğru ihmal edilebilir ölçüde küçültür.

    Kesişme testinde "sınıra dayanmak" ile "içinden geçmek" ayrımı için gerekli:
    çatı kenarına tam dayanmış bir panelde halka kenarı panelin *sınırıyla*
    çakışır ve kapalı poligon testinde "içeride" sayılır. Mikron mertebesinde
    küçültmek çakışan kenarı dışarı atar, gerçek bir geçişte ise kırpılan uzunluk
    metre mertebesinde kaldığı için sonucu değiştirmez.
    """
    cx, cy = polygon_centroid(convex)
    scale = 1.0 - relative
    return tuple((cx + (x - cx) * scale, cy + (y - cy) * scale) for x, y in convex)


def _segment_length_inside_convex(p1: Point, p2: Point, convex: Ring) -> float:
    """Doğru parçasının konveks halka içinde kalan bölümünün uzunluğu.

    Cyrus–Beck kırpması: CCW konveks halkada iç bölge her yönlü kenarın solunda
    kaldığından kısıt `cross(a, b, p) ≥ 0`'dır. Parametre aralığı [0, 1] her
        kenar için daraltılır; boşalırsa parça tamamen dışarıdadır.
    """
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    total_length = math.hypot(dx, dy)
    if total_length < _EPS:
        return 0.0

    t0, t1 = 0.0, 1.0
    for a, b in _edges(convex):
        c1 = _cross(a, b, p1)
        c2 = _cross(a, b, p2)
        denom = c2 - c1
        if abs(denom) < _EPS:
            # Parça bu kenara paralel: tamamen dışarıdaysa kesişim boş
            if c1 < 0.0:
                return 0.0
            continue
        t_hit = -c1 / denom
        if denom > 0.0:  # yarım düzleme giriş
            t0 = max(t0, t_hit)
        else:  # çıkış
            t1 = min(t1, t_hit)
        if t0 > t1:
            return 0.0
    return (t1 - t0) * total_length


def convex_crossed_by_ring(convex: Ring, ring: Ring) -> bool:
    """Halkanın herhangi bir kenarı konveks bölgenin *içinden* geçiyor mu?

    Sınıra değmek geçiş sayılmaz (bkz. `_shrink_convex`).
    """
    probe = _shrink_convex(convex)
    return any(
        _segment_length_inside_convex(e1, e2, probe) > _EPS for e1, e2 in _edges(ring)
    )


def rect_fits_inside(rect: Ring, ring: Ring, setback_m: float) -> bool:
    """Dikdörtgen halkanın içinde ve her kenarından ≥ `setback_m` uzakta mı?

    Üç koşul aranır:

    1. Dikdörtgenin dört köşesi de halkanın içinde.
    2. Hiçbir halka kenarı dikdörtgenin içinden geçmiyor.
    3. Sınırlar arası mesafe ≥ `setback_m`.

    (1) ve (2) birlikte kapsamayı verir: dikdörtgenin bir kısmı dışarıda olsaydı
    iç ve dış bölümü ayıran sınır ancak bir halka kenarının dikdörtgeni kesmesiyle
    oluşabilirdi. Bu, tek tek köşe testinin klasik tuzağını — *"dört köşe içeride
    ama orta kısım çentiğin üstünden atlıyor"* — yakalar. U biçimli bir çatıda
    çentiğin iki kolunu birleştiren panel tam olarak bu durumdur.

    (2) ayrı bir yüklem olmak zorunda; (3)'e yüklenemez. `setback_m = 0` iken
    çentikten geçen panelin de kenara dayanmış panelin de sınır mesafesi 0'dır,
    yani mesafe bu ikisini ayırt edemez. Kenar mesafesi pratikte her zaman
    pozitif olsa da yüklemin doğruluğu ona bağlı bırakılmadı.
    """
    if not all(point_in_ring(corner, ring) for corner in rect):
        return False
    if convex_crossed_by_ring(rect, ring):
        return False
    return ring_to_ring_distance(rect, ring) >= setback_m - _EPS


def rect_clears_obstacle(rect: Ring, obstacle: Ring, clearance_m: float) -> bool:
    """Dikdörtgen engelden (baca, çatı penceresi, ağaç tabanı) yeterince uzakta mı?

    Üç kontrol tamamlar: dikdörtgen köşesi engelin içinde olmasın, engel köşesi
    dikdörtgenin içinde olmasın (biri diğerini tamamen yutabilir, o durumda
    hiçbir köşe karşı tarafta çıkmaz) ve sınırlar arası mesafe yeterli olsun.
    """
    if any(point_in_ring(corner, obstacle) for corner in rect):
        return False
    if any(point_in_ring(vertex, rect) for vertex in obstacle):
        return False
    return ring_to_ring_distance(rect, obstacle) >= clearance_m - _EPS


# --- Yardımcılar ----------------------------------------------------------------


def rotate(point: Point, angle_deg: float, about: Point = (0.0, 0.0)) -> Point:
    """Noktayı `about` etrafında saat yönünün tersine döndürür."""
    rad = math.radians(angle_deg)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    dx, dy = point[0] - about[0], point[1] - about[1]
    return (
        about[0] + dx * cos_a - dy * sin_a,
        about[1] + dx * sin_a + dy * cos_a,
    )


def rotate_ring(ring: Ring, angle_deg: float, about: Point = (0.0, 0.0)) -> Ring:
    return tuple(rotate(p, angle_deg, about) for p in ring)


def axis_aligned_rect(center: Point, width_m: float, height_m: float) -> Ring:
    """Eksene paralel dikdörtgen halkası (CCW)."""
    hw, hh = width_m / 2.0, height_m / 2.0
    cx, cy = center
    return (
        (cx - hw, cy - hh),
        (cx + hw, cy - hh),
        (cx + hw, cy + hh),
        (cx - hw, cy + hh),
    )
