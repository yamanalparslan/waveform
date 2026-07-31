"""Fizibilite geometrisi: yerel düzlem dönüşümü ve panel yerleşim yüklemleri.

`prospect/geometry.py` harici geometri kütüphanesi kullanmıyor (gerekçe orada
yazılı), dolayısıyla yüklemlerin doğruluğu tamamen bu testlere bakıyor.
Özellikle `rect_fits_inside`'ın "dört köşe içeride ama orta kısım çentikten
atlıyor" durumunu yakalaması kritik: kaçırılırsa çatıda olmayan panel raporlanır.
"""

import math

import pytest

from luminmind.prospect.geometry import (
    LatLon,
    LocalFrame,
    axis_aligned_rect,
    bounding_box,
    convex_crossed_by_ring,
    distance_point_to_ring,
    normalize_ring,
    point_in_ring,
    point_to_segment_distance,
    polygon_area_m2,
    polygon_centroid,
    polygon_perimeter_m,
    rect_clears_obstacle,
    rect_fits_inside,
    ring_is_simple,
    ring_to_ring_distance,
    rotate,
    segment_distance,
    segments_intersect,
    signed_area,
)

KONYA = LatLon(lat=37.87, lon=32.48)

# 10 × 10 m kare, CCW
SQUARE = ((0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0))

# U biçimli çatı: x ∈ (3, 7), y ∈ (4, 10) aralığı *dışarıdadır* (avlu/çentik).
U_SHAPE = (
    (0.0, 0.0),
    (10.0, 0.0),
    (10.0, 10.0),
    (7.0, 10.0),
    (7.0, 4.0),
    (3.0, 4.0),
    (3.0, 10.0),
    (0.0, 10.0),
)


# ------------------------------ Yerel düzlem ------------------------------


def test_local_frame_roundtrip_is_lossless():
    frame = LocalFrame.at(KONYA.lat, KONYA.lon)
    point = LatLon(lat=KONYA.lat + 0.001, lon=KONYA.lon - 0.002)

    back = frame.to_wgs84(frame.to_local(point))

    assert back.lat == pytest.approx(point.lat, abs=1e-12)
    assert back.lon == pytest.approx(point.lon, abs=1e-12)


def test_local_frame_origin_maps_to_zero():
    frame = LocalFrame.at(KONYA.lat, KONYA.lon)
    assert frame.to_local(KONYA) == (0.0, 0.0)


def test_local_frame_scales_match_wgs84_ellipsoid():
    """Konya enleminde 1° enlem ≈ 110,9 km, 1° boylam ≈ 88,0 km."""
    frame = LocalFrame.at(KONYA.lat, KONYA.lon)

    assert frame.m_per_deg_lat == pytest.approx(110_900.0, rel=1e-3)
    assert frame.m_per_deg_lon == pytest.approx(88_000.0, rel=1e-3)
    # Boylam ölçeği enlemle cos φ kadar daralır
    assert frame.m_per_deg_lon < frame.m_per_deg_lat


def test_local_frame_differs_from_spherical_approximation():
    """Elipsoid yarıçapı küresel `R = 6371 km` yaklaşımından binde bir sapar.

    Bu farkın *var olması* testin konusu: küresel sabite dönülürse 100 m'lik bir
    çatının alanı ~%0,2 kayar. Fark küçük ama bedava kaçınılabilir.
    """
    frame = LocalFrame.at(KONYA.lat, KONYA.lon)
    spherical = 6_371_000.0 * math.pi / 180.0

    assert frame.m_per_deg_lat != pytest.approx(spherical, rel=1e-4)
    assert frame.m_per_deg_lat == pytest.approx(spherical, rel=3e-3)


def test_local_frame_centered_on_uses_mean_position():
    points = [LatLon(lat=37.0, lon=32.0), LatLon(lat=38.0, lon=33.0)]
    frame = LocalFrame.centered_on(points)

    assert frame.origin_lat == pytest.approx(37.5)
    assert frame.origin_lon == pytest.approx(32.5)


def test_local_frame_centered_on_rejects_empty():
    with pytest.raises(ValueError, match="en az bir nokta"):
        LocalFrame.centered_on([])


def test_ring_roundtrip_preserves_area():
    """WGS84 → metre → WGS84 turunda çatı alanı korunmalı."""
    frame = LocalFrame.at(KONYA.lat, KONYA.lon)
    corners = [
        LatLon(lat=KONYA.lat, lon=KONYA.lon),
        LatLon(lat=KONYA.lat, lon=KONYA.lon + 0.0005),
        LatLon(lat=KONYA.lat + 0.0004, lon=KONYA.lon + 0.0005),
        LatLon(lat=KONYA.lat + 0.0004, lon=KONYA.lon),
    ]

    ring = frame.ring_to_local(corners)
    area = polygon_area_m2(ring)
    revisited = frame.ring_to_local(frame.ring_to_wgs84(ring))

    assert polygon_area_m2(revisited) == pytest.approx(area, rel=1e-12)
    # 0,0005° boylam × 0,0004° enlem ≈ 44 m × 44,4 m
    assert area == pytest.approx(44.0 * 44.36, rel=1e-2)


# ------------------------------ Poligon büyüklükleri ------------------------------


def test_signed_area_sign_follows_winding():
    assert signed_area(SQUARE) == pytest.approx(100.0)
    assert signed_area(tuple(reversed(SQUARE))) == pytest.approx(-100.0)
    assert polygon_area_m2(tuple(reversed(SQUARE))) == pytest.approx(100.0)


def test_signed_area_of_degenerate_ring_is_zero():
    assert signed_area(((0.0, 0.0), (1.0, 1.0))) == 0.0


def test_u_shape_area_excludes_notch():
    """Toplam 10×10 = 100 m², çentik 4×6 = 24 m² → 76 m²."""
    assert polygon_area_m2(U_SHAPE) == pytest.approx(76.0)


def test_perimeter_closes_the_ring():
    """Son köşeden ilkine dönüş de sayılmalı — atlanırsa çevre bir kenar eksik çıkar."""
    assert polygon_perimeter_m(SQUARE) == pytest.approx(40.0)


def test_centroid_of_square_is_its_middle():
    assert polygon_centroid(SQUARE) == pytest.approx((5.0, 5.0))


def test_centroid_falls_back_to_vertex_mean_when_degenerate():
    """Sıfır alanlı halkada alan merkezi tanımsız; köşe ortalaması dönmeli."""
    collapsed = ((0.0, 0.0), (2.0, 2.0), (4.0, 4.0))
    assert polygon_centroid(collapsed) == pytest.approx((2.0, 2.0))


def test_bounding_box_covers_all_vertices():
    assert bounding_box(U_SHAPE) == (0.0, 0.0, 10.0, 10.0)


# ------------------------------ normalize_ring ------------------------------


def test_normalize_ring_makes_winding_counterclockwise():
    normalized = normalize_ring(tuple(reversed(SQUARE)))
    assert signed_area(normalized) > 0.0


def test_normalize_ring_drops_repeated_closing_point():
    """Harita kütüphaneleri poligonu ilk noktayla kapatır; ikinci kez saymamalıyız."""
    closed = SQUARE + (SQUARE[0],)
    assert len(normalize_ring(closed)) == 4


def test_normalize_ring_drops_consecutive_duplicates():
    doubled = ((0.0, 0.0), (0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0))
    assert len(normalize_ring(doubled)) == 4


# ------------------------------ point_in_ring ------------------------------


def test_point_in_ring_inside_and_outside():
    assert point_in_ring((5.0, 5.0), SQUARE)
    assert not point_in_ring((15.0, 5.0), SQUARE)
    assert not point_in_ring((-1.0, 5.0), SQUARE)


def test_point_in_ring_is_stable_at_vertex_height():
    """Yarı-açık kural: köşe hizasındaki yatay ışın iki kez sayılmamalı.

    y = 0 tam alt kenarın (ve iki köşenin) hizası. Kural bozulursa aynı yükseklikte
    içerideki ve dışarıdaki noktalar birbirine karışır.
    """
    assert point_in_ring((5.0, 0.0), SQUARE)
    assert not point_in_ring((-1.0, 0.0), SQUARE)
    assert not point_in_ring((11.0, 0.0), SQUARE)


def test_point_in_ring_sees_the_notch_as_outside():
    assert point_in_ring((5.0, 2.0), U_SHAPE)  # tabanda, içeride
    assert not point_in_ring((5.0, 7.0), U_SHAPE)  # çentikte, dışarıda
    assert point_in_ring((1.0, 7.0), U_SHAPE)  # sol kolda, içeride
    assert point_in_ring((9.0, 7.0), U_SHAPE)  # sağ kolda, içeride


# ------------------------------ Mesafe ve kesişim ------------------------------


def test_point_to_segment_distance_clamps_to_endpoints():
    a, b = (0.0, 0.0), (10.0, 0.0)

    assert point_to_segment_distance((5.0, 3.0), a, b) == pytest.approx(3.0)
    # İzdüşüm parçanın dışına düşüyor → uca olan mesafe
    assert point_to_segment_distance((-4.0, 3.0), a, b) == pytest.approx(5.0)
    assert point_to_segment_distance((14.0, 3.0), a, b) == pytest.approx(5.0)


def test_point_to_segment_distance_handles_zero_length_segment():
    assert point_to_segment_distance((3.0, 4.0), (0.0, 0.0), (0.0, 0.0)) == pytest.approx(5.0)


def test_segments_intersect_crossing_and_disjoint():
    assert segments_intersect((0.0, 0.0), (10.0, 10.0), (0.0, 10.0), (10.0, 0.0))
    assert not segments_intersect((0.0, 0.0), (1.0, 1.0), (5.0, 5.0), (6.0, 6.0))


def test_segments_intersect_detects_touching_endpoint():
    """Uç değme kesişimdir: kenara *dayanan* panel de sınırı paylaşır."""
    assert segments_intersect((0.0, 0.0), (5.0, 0.0), (5.0, 0.0), (5.0, 5.0))


def test_segments_intersect_detects_collinear_overlap():
    """Eşdoğrusal örtüşmede çapraz çarpım işaret testi güvenilmez; mesafeye düşülür."""
    assert segments_intersect((0.0, 0.0), (10.0, 0.0), (4.0, 0.0), (6.0, 0.0))


def test_segment_distance_is_zero_when_crossing():
    assert segment_distance((0.0, 0.0), (10.0, 10.0), (0.0, 10.0), (10.0, 0.0)) == 0.0


def test_segment_distance_between_parallel_segments():
    assert segment_distance(
        (0.0, 0.0), (10.0, 0.0), (0.0, 2.5), (10.0, 2.5)
    ) == pytest.approx(2.5)


def test_ring_to_ring_distance_measures_boundaries():
    """Halka *içinde* olmak mesafeyi sıfırlamaz — ölçülen sınırlar arası boşluk."""
    inner = axis_aligned_rect((5.0, 5.0), 2.0, 2.0)
    assert ring_to_ring_distance(inner, SQUARE) == pytest.approx(4.0)


def test_ring_to_ring_distance_is_zero_when_boundaries_touch():
    touching = axis_aligned_rect((5.0, 1.0), 2.0, 2.0)  # alt kenarı y = 0'a dayanıyor
    assert ring_to_ring_distance(touching, SQUARE) == pytest.approx(0.0)


def test_distance_point_to_ring_ignores_inside_outside():
    """Sınıra mesafe; yerleşimdeki hızlı yol bu simetriye dayanıyor."""
    assert distance_point_to_ring((5.0, 5.0), SQUARE) == pytest.approx(5.0)
    assert distance_point_to_ring((5.0, 13.0), SQUARE) == pytest.approx(3.0)


# ------------------------------ ring_is_simple ------------------------------


def test_ring_is_simple_accepts_convex_and_concave():
    assert ring_is_simple(SQUARE)
    assert ring_is_simple(U_SHAPE)


def test_ring_is_simple_rejects_bowtie():
    """Kullanıcı haritada kolayca kelebek çizer; alan hesabı orada anlamsızlaşır."""
    bowtie = ((0.0, 0.0), (10.0, 10.0), (10.0, 0.0), (0.0, 10.0))
    assert not ring_is_simple(bowtie)


def test_ring_is_simple_rejects_too_few_vertices():
    assert not ring_is_simple(((0.0, 0.0), (1.0, 1.0)))


# ------------------------------ convex_crossed_by_ring ------------------------------


def test_convex_crossed_by_ring_detects_interior_crossing():
    rect = axis_aligned_rect((5.0, 8.5), 8.0, 1.0)  # x: 1→9, y: 8→9
    assert convex_crossed_by_ring(rect, U_SHAPE)


def test_convex_crossed_by_ring_ignores_boundary_contact():
    """Çatı kenarına tam dayanmış panel "geçiyor" sayılmamalı (bkz. `_shrink_convex`)."""
    flush = axis_aligned_rect((5.0, 1.0), 4.0, 2.0)  # alt kenarı y = 0'da
    assert not convex_crossed_by_ring(flush, SQUARE)


# ------------------------------ rect_fits_inside ------------------------------


def test_rect_fits_inside_accepts_panel_with_clearance():
    rect = axis_aligned_rect((5.0, 5.0), 4.0, 4.0)
    assert rect_fits_inside(rect, SQUARE, setback_m=0.6)


def test_rect_fits_inside_rejects_panel_violating_setback():
    """Kenardan 0,2 m'de duran panel 0,6 m kenar mesafesini sağlamaz."""
    rect = axis_aligned_rect((5.0, 1.2), 4.0, 2.0)  # alt kenar y = 0,2
    assert rect_fits_inside(rect, SQUARE, setback_m=0.1)
    assert not rect_fits_inside(rect, SQUARE, setback_m=0.6)


def test_rect_fits_inside_rejects_partially_outside_panel():
    rect = axis_aligned_rect((9.5, 5.0), 4.0, 2.0)  # x: 7,5 → 11,5
    assert not rect_fits_inside(rect, SQUARE, setback_m=0.0)


def test_rect_fits_inside_rejects_panel_bridging_a_notch():
    """Dört köşe içeride ama orta kısım çentiğin üstünden atlıyor.

    U biçimli çatının iki kolunu birleştiren panel: köşeleri kollarda (içeride),
    gövdesi avluda (dışarıda). Yalnızca köşe testi yapılırsa bu panel kabul
    edilir ve rapor çatıda fiziksel olarak olmayan bir paneli sayar.
    """
    rect = axis_aligned_rect((5.0, 8.5), 8.0, 1.0)  # x: 1→9, y: 8→9

    assert all(point_in_ring(corner, U_SHAPE) for corner in rect), "kurulum: dört köşe içeride"
    assert not rect_fits_inside(rect, U_SHAPE, setback_m=0.0)


def test_rect_fits_inside_accepts_panel_within_one_arm():
    rect = axis_aligned_rect((1.5, 7.0), 2.0, 4.0)  # sol kolun içinde
    assert rect_fits_inside(rect, U_SHAPE, setback_m=0.4)


def test_rect_fits_inside_notch_rejection_does_not_rely_on_setback():
    """Kenar mesafesi 0 iken de çentikten geçen panel reddedilmeli.

    `setback_m = 0` iken çentikten geçen panelin ve kenara dayanmış panelin sınır
    mesafesi eşittir (ikisi de 0); ayrımı yapan `convex_crossed_by_ring`.
    """
    bridging = axis_aligned_rect((5.0, 8.5), 8.0, 1.0)
    flush = axis_aligned_rect((1.5, 2.0), 2.0, 4.0)

    assert not rect_fits_inside(bridging, U_SHAPE, setback_m=0.0)
    assert rect_fits_inside(flush, U_SHAPE, setback_m=0.0)


# ------------------------------ rect_clears_obstacle ------------------------------


def test_rect_clears_obstacle_when_far_enough():
    chimney = axis_aligned_rect((2.0, 2.0), 1.0, 1.0)
    panel = axis_aligned_rect((6.0, 6.0), 2.0, 2.0)
    assert rect_clears_obstacle(panel, chimney, clearance_m=0.5)


def test_rect_clears_obstacle_rejects_overlap():
    chimney = axis_aligned_rect((5.0, 5.0), 1.0, 1.0)
    panel = axis_aligned_rect((5.4, 5.0), 2.0, 2.0)
    assert not rect_clears_obstacle(panel, chimney, clearance_m=0.0)


def test_rect_clears_obstacle_rejects_engulfed_obstacle():
    """Panel bacayı tamamen yutuyorsa hiçbir köşe karşı tarafta çıkmaz.

    Bu yüzden iki yönlü köşe testi gerekiyor: engelin köşeleri de panelin içinde
    olmamalı. Tek yönlü test bu paneli kabul ederdi.
    """
    chimney = axis_aligned_rect((5.0, 5.0), 0.4, 0.4)
    panel = axis_aligned_rect((5.0, 5.0), 3.0, 3.0)

    assert not rect_clears_obstacle(panel, chimney, clearance_m=0.0)


def test_rect_clears_obstacle_respects_clearance_margin():
    chimney = axis_aligned_rect((5.0, 5.0), 1.0, 1.0)  # y: 4,5 → 5,5
    panel = axis_aligned_rect((5.0, 6.5), 1.0, 1.0)  # y: 6,0 → 7,0, boşluk 0,5 m

    assert rect_clears_obstacle(panel, chimney, clearance_m=0.4)
    assert not rect_clears_obstacle(panel, chimney, clearance_m=0.6)


# ------------------------------ Yardımcılar ------------------------------


def test_rotate_is_counterclockwise():
    assert rotate((1.0, 0.0), 90.0) == pytest.approx((0.0, 1.0), abs=1e-12)


def test_rotate_about_a_pivot():
    assert rotate((2.0, 1.0), 180.0, about=(1.0, 1.0)) == pytest.approx((0.0, 1.0), abs=1e-12)


def test_axis_aligned_rect_dimensions_and_winding():
    rect = axis_aligned_rect((5.0, 5.0), 4.0, 2.0)

    assert bounding_box(rect) == (3.0, 4.0, 7.0, 6.0)
    assert polygon_area_m2(rect) == pytest.approx(8.0)
    assert signed_area(rect) > 0.0, "CCW olmalı — geometri yüklemleri bunu varsayıyor"
