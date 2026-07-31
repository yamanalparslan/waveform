"""2D panel yerleşimi: çatı/arazi poligonundan dizi geometrisine ve string planına.

Kullanıcı haritada bir poligon çizer; bu dosya oraya kaç panel sığdığını, hangi
sıra aralığıyla, hangi yönelimde ve nasıl bir elektriksel bölümlemeyle
yerleşeceğini üretir. Çıktı doğrudan `twin.plant_model.ArrayConfig`'e çevrilip
`twin.pipeline.run_chain`'e verilebilir — yerleşim önerir, simülasyon yargılar.

**Görselleştirme 2D'dir ve öyle kalır.** Paneller üstten görünüşte, uydu
görüntüsünün üstüne çizilen 2D dikdörtgenlerdir. Gölgeleme matematiği arka
planda 3D geometri kullanır (sıra yüksekliği, güneş yükseklik açısı) ama
kullanıcıya sunulan hiçbir yerde 3D sahne yoktur.

Üç tasarım kararı algoritmanın şeklini belirliyor:

**1. Izgara döndürülmez, taban değiştirilir.** Paneller çatı azimutuna hizalı
olmak zorunda; poligonu döndürüp eksene paralel ızgara kurmak yerine ızgara
doğrudan (satır boyu, eğim boyu) tabanında kuruluyor. Poligon hiç dönmez, tek
bir dönüşüm hatası kaynağı ortadan kalkar ve panel dikdörtgenleri yerel
çerçevede zaten doğru açıda çıkar.

**2. Eğik çatıda izdüşüm tuzağı.** Haritada çizilen poligon çatının *yatay
izdüşümü*dür; gerçek çatı yüzeyi eğim yönünde 1/cos(β) kadar uzundur. Panel
sayısı izdüşüm üzerinden hesaplanırsa 30° eğimde kapasite %13 eksik çıkar. Bu
yüzden panelin eğim yönündeki *izdüşüm derinliği* `d·cos β` kullanılır: paneller
izdüşüm düzleminde daha kısa görünür, dolayısıyla aynı izdüşüme daha çoğu sığar.

**3. Izgara fazı aranır.** Izgaranın nereden başladığı panel sayısını %5–15
değiştirir (kenara denk gelen yarım sıra kaybolur). Her yönelim için birkaç faz
ötelemesi denenip en çok panel veren seçilir. Ucuz ve kullanıcının elle
yapamayacağı bir iyileştirme.
"""

import logging
import math
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import timedelta
from enum import StrEnum

import numpy as np
import pandas as pd
from pvlib import solarposition

from luminmind.prospect.geometry import (
    Point,
    Ring,
    bounding_box,
    distance_point_to_ring,
    normalize_ring,
    point_in_ring,
    polygon_area_m2,
    rect_clears_obstacle,
    rect_fits_inside,
    ring_is_simple,
)
from luminmind.prospect.pvgis import TMY_REFERENCE_YEAR
from luminmind.twin.plant_model import ArrayConfig, MountType

logger = logging.getLogger(__name__)

# Kış gündönümünde gölgesiz çalışması istenen saat aralığı (gerçek güneş saati).
# Türkiye'de yerleşik tasarım ölçütü 09:00–15:00'tir; daha geniş bir pencere
# sıra aralığını hızla büyütür (güneş alçaldıkça gölge boyu 1/tan α ile ışınır)
# ve m² başına üretimi düşürür.
DEFAULT_SHADING_WINDOW = (9.0, 15.0)

# Sıra aralığı hesabında sayısal koruma: bu yükseklik altındaki güneş için gölge
# boyu ıraksar ve pencere ölçütü zaten o saatleri dışlar.
_MIN_DESIGN_ELEVATION_DEG = 3.0

# Izgara fazı araması — yönelim başına 3×3 deneme. Daha yoğun arama panel
# sayısını binde birler mertebesinde artırıyor, süreyi ise doğrusal büyütüyor.
_PHASE_STEPS = 3


class Orientation(StrEnum):
    """Panelin sıra içindeki duruşu."""

    PORTRAIT = "portrait"  # uzun kenar eğim yönünde (dikey)
    LANDSCAPE = "landscape"  # uzun kenar sıra boyunca (yatay)


@dataclass(frozen=True)
class ModuleSpec:
    """Panel datasheet'i — mekanik ve elektriksel.

    Varsayılanlar 2026 Türkiye pazarında yaygın bir n-tipi TOPCon modülüdür.
    Gerçek teklif için EPC'nin kullandığı modülle değiştirilir; alanlar
    datasheet'ten birebir okunacak şekilde adlandırıldı.
    """

    name: str = "Jenerik 580 W TOPCon"
    width_m: float = 1.134  # kısa kenar
    height_m: float = 2.278  # uzun kenar
    pdc0_w: float = 580.0
    gamma_pdc: float = -0.0029  # Pmp sıcaklık katsayısı (1/°C)
    voc_stc_v: float = 52.0
    vmp_stc_v: float = 43.5
    beta_voc_per_c: float = -0.0025  # Voc sıcaklık katsayısı (bağıl, 1/°C)
    beta_vmp_per_c: float = -0.0030
    module_type: str = "monosi"
    bifaciality: float = 0.0  # tek yüzlü; çift yüzlüde 0,65–0,80

    @property
    def area_m2(self) -> float:
        return self.width_m * self.height_m

    def dimensions(self, orientation: Orientation) -> tuple[float, float]:
        """(sıra boyu genişlik, eğim boyu derinlik) — gerçek (eğik) ölçüler."""
        if orientation is Orientation.PORTRAIT:
            return self.width_m, self.height_m
        return self.height_m, self.width_m


@dataclass(frozen=True)
class InverterSpec:
    """İnvertör datasheet'i — string boyu bunun gerilim penceresinden çıkar."""

    name: str = "Jenerik 100 kW string"
    ac_kw: float = 100.0
    max_dc_voltage_v: float = 1100.0
    mppt_min_voltage_v: float = 200.0
    mppt_inputs: int = 10
    # Bir MPPT girişine paralel bağlanabilen string sayısı (kombinatör kutusu).
    # Modern 100 kW string invertörlerde 2 tipiktir. String sayısı bu kapasiteyi
    # aşarsa tasarım kâğıt üstünde kalır — kurulabilir olmaz.
    strings_per_mppt: int = 2
    eta_nom: float = 0.98
    target_dc_ac_ratio: float = 1.2

    @property
    def strings_per_inverter(self) -> int:
        return self.mppt_inputs * self.strings_per_mppt


@dataclass(frozen=True)
class Obstacle:
    """3D Engel — Harita üzerinde bir poligon ve yükseklik."""
    polygon: Ring
    height_m: float = 0.0

@dataclass(frozen=True)
class MountingSpec:
    """Montaj ve yerleşim kısıtları."""

    mount: MountType = MountType.ROOFTOP_TILTED
    tilt_deg: float = 15.0
    azimuth_deg: float = 180.0  # 180 = güney
    # Çatı kenarından bırakılacak mesafe (yangın erişimi, bakım yolu, parapet).
    setback_m: float = 0.6
    module_gap_m: float = 0.02  # sıra içinde panel arası (montaj kızağı)
    row_gap_m: float = 0.02  # yalnızca çatıya paralel montajda sıra arası
    ground_clearance_m: float = 0.5
    albedo: float = 0.20
    obstacles: tuple[Obstacle, ...] = ()  # baca, çatı penceresi, klima ünitesi
    obstacle_clearance_m: float = 0.5
    # Sıra aralığı elle verilirse gölgeleme ölçütü atlanır (EPC'nin kendi
    # kızak sisteminin sabit adımı olabilir).
    row_pitch_m: float | None = None
    shading_window: tuple[float, float] = DEFAULT_SHADING_WINDOW


@dataclass(frozen=True)
class PanelPlacement:
    """Yerleştirilmiş tek panel — yerel çerçevede 2D dikdörtgen."""

    rect: Ring
    row: int
    col: int
    string_index: int = -1  # -1 = henüz string atanmadı


@dataclass(frozen=True)
class StringPlan:
    """Elektriksel bölümleme.

    String boyu invertörün gerilim penceresiyle sınırlıdır ve *sahanın kendi
    sıcaklık uçlarından* türetilir (TMY'nin en soğuk/en sıcak saatleri) — sabit
    bir "−10 °C" varsayımı Antalya'da gereksiz kısa string, Ağrı'da tehlikeli
    uzun string üretirdi.
    """

    modules_per_string: int
    strings: int
    modules_min: int  # gerilim penceresinin alt sınırı
    modules_max: int  # üst sınırı
    trimmed_modules: int  # tam string'e sığmadığı için yerleşimden çıkarılan
    inverter_count: int
    mppt_capacity: int  # invertörlerin alabileceği azami string sayısı
    design_min_ambient_c: float
    design_max_cell_c: float

    @property
    def total_modules(self) -> int:
        return self.modules_per_string * self.strings

    @property
    def mppt_shortfall(self) -> int:
        """MPPT girişi yetmeyen string sayısı; 0 değilse invertör seçimi uygun değil.

        Eksiklik invertör *ekleyerek* kapatılmıyor bilerek: 1 MW'lık merkezi
        invertörle 2,8 MWp'lik sahada bir invertör daha eklemek DC/AC oranını
        0,94'ten 0,71'e düşürüp yatırım maliyetini şişirir ve kırpma kaybını
        olduğundan az gösterir — yani finansal modeli sessizce bozar. Doğru
        cevap "bu invertör bu sahaya uymuyor" demektir; karar EPC'nin.
        """
        return max(0, self.strings - self.mppt_capacity)


@dataclass(frozen=True)
class LayoutResult:
    """Yerleşim sonucu — 2D çizim, kapasite ve simülasyon girdisi."""

    placements: tuple[PanelPlacement, ...]
    orientation: Orientation
    module: ModuleSpec
    inverter: InverterSpec
    mounting: MountingSpec
    row_pitch_m: float
    collector_width_m: float  # panelin eğim yönündeki gerçek (eğik) uzunluğu
    gcr: float
    area_m2: float  # poligon alanı (yatay izdüşüm)
    string_plan: StringPlan
    rows: int

    @property
    def module_count(self) -> int:
        return len(self.placements)

    @property
    def dc_capacity_kwp(self) -> float:
        return self.module_count * self.module.pdc0_w / 1000.0

    @property
    def ac_capacity_kw(self) -> float:
        return self.string_plan.inverter_count * self.inverter.ac_kw

    @property
    def module_area_m2(self) -> float:
        return self.module_count * self.module.area_m2

    @property
    def surface_area_m2(self) -> float:
        """Panellerin üzerine oturduğu gerçek yüzey alanı.

        Çatıya *paralel* montajda kullanıcının çizdiği poligon eğimli çatının
        yatay izdüşümüdür; gerçek çatı yüzeyi 1/cos β kadar büyüktür. Doluluğu
        izdüşüme oranlamak %100'ü aşan değerler üretir (30° eğimde %115'e kadar)
        ve teknik olarak doğru olsa da kullanıcıya hata gibi görünür. Açılı
        montajda poligon zaten düz zemindir, dönüşüm yapılmaz.
        """
        if self.mounting.mount is MountType.ROOFTOP:
            return self.area_m2 / math.cos(math.radians(self.mounting.tilt_deg))
        return self.area_m2

    @property
    def area_utilisation(self) -> float:
        """Panel alanı / yüzey alanı. Kullanıcının "çatım ne kadar doldu" sorusu."""
        surface = self.surface_area_m2
        return self.module_area_m2 / surface if surface > 0 else 0.0

    @property
    def specific_density_wp_m2(self) -> float:
        surface = self.surface_area_m2
        return self.dc_capacity_kwp * 1000.0 / surface if surface > 0 else 0.0

    @property
    def dc_ac_ratio(self) -> float:
        """Gerçekleşen DC/AC oranı — invertör adedi tam sayıya yuvarlandığı için
        hedeften sapar; kırpma kaybını ve yatırım maliyetini bu belirler."""
        ac = self.ac_capacity_kw
        return self.dc_capacity_kwp / ac if ac > 0 else 0.0

    def to_array_config(self, inverter_ac_kw: float | None = None) -> ArrayConfig:
        """Simülasyona girecek dizi konfigürasyonu.

        `modules_per_string × strings` çarpımı gerçek panel sayısına *eşittir*;
        PVWatts toplu bir DC modeli olduğu için bölünmenin kendisi fiziğe girmez
        (yalnızca çarpım `dc_capacity_w`'ye gider), ama kapasitenin tam olması
        şart — bir panel kaymak bile 25 yıllık üretim toplamına taşır.
        """
        return ArrayConfig(
            tilt_deg=self.mounting.tilt_deg,
            azimuth_deg=self.mounting.azimuth_deg,
            modules_per_string=self.string_plan.modules_per_string,
            strings=self.string_plan.strings,
            module_pdc0_w=self.module.pdc0_w,
            gamma_pdc=self.module.gamma_pdc,
            inverter_ac_kw=inverter_ac_kw if inverter_ac_kw is not None else self.ac_capacity_kw,
            dc_ac_ratio=self.inverter.target_dc_ac_ratio,
            inverter_eta_nom=self.inverter.eta_nom,
            mount=self.mounting.mount,
            gcr=self.gcr,
            collector_width_m=self.collector_width_m,
            ground_clearance_m=self.mounting.ground_clearance_m,
            albedo=self.mounting.albedo,
            bifaciality=self.module.bifaciality,
            module_type=self.module.module_type,
        )


# --- Sıra aralığı ---------------------------------------------------------------


def required_row_pitch(
    latitude: float,
    longitude: float,
    slant_m: float,
    tilt_deg: float,
    azimuth_deg: float,
    window: tuple[float, float] = DEFAULT_SHADING_WINDOW,
) -> float:
    """Kış gündönümünde verilen pencerede gölgesiz kalması için gereken sıra aralığı.

    Ön sıranın üst kenarının gölgesi arka sıranın alt kenarına ulaşmamalıdır:

        aralık ≥ d·cos β + d·sin β · cos(γ_güneş − γ_dizi) / tan(α)

    Kapalı formül tek bir "kritik saat" için yazılıp bırakılmıyor; pencere
    boyunca 5 dakikalık adımlarla tarayıp en büyüğü alıyoruz. Doğu/batıya dönük
    dizilerde kritik an öğle değildir ve tek noktaya bakan hesap yetersiz aralık
    üretir.

    Gerçek güneş saati boylamdan ve zaman denkleminden türetilir; UTC saati
    doğrudan kullanmak boylamı 30°'den uzak sahalarda pencereyi kaydırırdı.
    """
    day = pd.Timestamp(year=TMY_REFERENCE_YEAR, month=12, day=21, tz="UTC")
    times = pd.date_range(day, day + timedelta(days=1), freq="5min", inclusive="left")

    eot_min = solarposition.equation_of_time_spencer71(times.dayofyear)
    utc_hours = times.hour + times.minute / 60.0
    solar_time = utc_hours + longitude / 15.0 + np.asarray(eot_min, dtype=float) / 60.0

    in_window = (solar_time >= window[0]) & (solar_time <= window[1])
    if not bool(in_window.any()):
        raise ValueError(f"gölgeleme penceresi boş: {window}")

    selected = times[in_window]
    solpos = solarposition.get_solarposition(selected, latitude, longitude)
    elevation = solpos["apparent_elevation"].to_numpy(dtype=float)
    sun_azimuth = solpos["azimuth"].to_numpy(dtype=float)

    usable = elevation > _MIN_DESIGN_ELEVATION_DEG
    if not bool(usable.any()):
        # Kutupsal durum; Türkiye'de gerçekleşmez ama sessizce 0 dönmeyelim.
        raise ValueError("gölgeleme penceresinde güneş ufkun yeterince üstünde değil")

    tilt_rad = math.radians(tilt_deg)
    row_projection = slant_m * math.cos(tilt_rad)
    row_rise = slant_m * math.sin(tilt_rad)

    delta_azimuth = np.radians(sun_azimuth[usable] - azimuth_deg)
    # Güneş dizinin arkasındayken (cos < 0) sıra gölgesi arkaya değil öne düşer.
    reach = np.cos(delta_azimuth).clip(min=0.0) / np.tan(np.radians(elevation[usable]))
    return float(row_projection + row_rise * float(reach.max()))


def _resolve_row_pitch(
    latitude: float,
    longitude: float,
    slant_m: float,
    projected_depth_m: float,
    mounting: MountingSpec,
) -> float:
    """Montaj tipine göre sıra aralığı."""
    if mounting.row_pitch_m is not None:
        return mounting.row_pitch_m

    if mounting.mount is MountType.ROOFTOP:
        # Çatıya paralel: paneller tek düzlemde, aralarında yalnızca kızak boşluğu.
        # Boşluk da çatı düzleminde ölçüldüğü için izdüşüme cos β ile iner.
        return projected_depth_m + mounting.row_gap_m * math.cos(
            math.radians(mounting.tilt_deg)
        )

    pitch = required_row_pitch(
        latitude,
        longitude,
        slant_m,
        mounting.tilt_deg,
        mounting.azimuth_deg,
        mounting.shading_window,
    )
    # GCR = eğik uzunluk / aralık tanımı gereği aralık en az eğik uzunluk kadar
    # olmalı; aksi halde gcr > 1 çıkar ve infinite_sheds modeli anlamsızlaşır
    # (ArrayConfig da doğrulamada reddeder).
    return max(pitch, slant_m * 1.01)


# --- String boyutlandırma -------------------------------------------------------


def plan_strings(
    module_count: int,
    module: ModuleSpec,
    inverter: InverterSpec,
    min_ambient_c: float,
    max_cell_c: float,
) -> StringPlan:
    """Panel sayısını invertörün gerilim penceresine uyan eşit string'lere böler.

    Üst sınır güvenlik kısıtıdır: en soğuk anda açık devre gerilimi invertörün
    azami DC gerilimini aşarsa cihaz zarar görür. Soğukta hücre sıcaklığı ortam
    sıcaklığına eşit kabul edilir (ışınım yokken ısınma yok) — sahanın TMY'sindeki
    en düşük ortam sıcaklığı kullanılır.

    Alt sınır çalışabilirlik kısıtıdır: en sıcak anda maksimum güç noktası
    gerilimi MPPT alt sınırının altına düşerse invertör diziyi izleyemez.

    Aralıktaki boylardan *tam bölen* tercih edilir; artık panel yerleşimden
    çıkarılır. Gerçek tasarımcı da string boyunu panel sayısına göre ayarlar,
    tersi değil.

    Üçüncü bir kısıt var ve atlanması kolay: string sayısı invertörlerin MPPT
    giriş kapasitesini aşamaz. Yalnızca "en az artık" ölçütüyle seçildiğinde
    525 panel 35×15'e bölünüyor, oysa `strings_per_mppt=1` olan bir invertörde
    3 cihazın 30 girişi var — tasarım kâğıt üstünde kalıyor. Kapasiteye sığan
    boylar arasından seçim yapılır; hiçbiri sığmazsa en uzun boy alınır ve
    eksiklik `mppt_shortfall` ile raporlanır (invertör *eklenmez*, bkz. oradaki
    gerekçe).
    """
    voc_cold = module.voc_stc_v * (1.0 + module.beta_voc_per_c * (min_ambient_c - 25.0))
    vmp_hot = module.vmp_stc_v * (1.0 + module.beta_vmp_per_c * (max_cell_c - 25.0))

    modules_max = int(inverter.max_dc_voltage_v // voc_cold)
    modules_min = max(1, math.ceil(inverter.mppt_min_voltage_v / vmp_hot))

    if modules_max < modules_min:
        raise ValueError(
            f"invertör modülle uyumsuz: soğukta {voc_cold:.1f} V/modül ile en çok "
            f"{modules_max} modül seri bağlanabilir, MPPT alt sınırı için en az "
            f"{modules_min} gerekiyor"
        )
    capped_max = min(modules_max, module_count) if module_count else modules_max
    if capped_max < modules_min or module_count == 0:
        return StringPlan(
            modules_per_string=0,
            strings=0,
            modules_min=modules_min,
            modules_max=modules_max,
            trimmed_modules=module_count,
            inverter_count=0,
            mppt_capacity=0,
            design_min_ambient_c=min_ambient_c,
            design_max_cell_c=max_cell_c,
        )

    lengths = range(modules_min, capped_max + 1)

    def fewest_orphans(pool: Iterable[int]) -> int:
        """En az artık bırakan boy; eşitlikte daha uzun (daha az string, az kablo)."""
        return min(pool, key=lambda n: (module_count % n, -n))

    # İnvertör sayısı DC/AC hedefinden gelir ve otoriterdir: kırpma kaybını ve
    # yatırım maliyetini o belirliyor, dolayısıyla string bölünmesi uğruna
    # değiştirilmez.
    inverter_count = max(
        1,
        math.ceil(
            module_count
            * module.pdc0_w
            / 1000.0
            / (inverter.ac_kw * inverter.target_dc_ac_ratio)
        ),
    )
    capacity = inverter_count * inverter.strings_per_inverter
    fitting = [n for n in lengths if module_count // n <= capacity]
    best_len = fewest_orphans(fitting) if fitting else capped_max

    strings = module_count // best_len
    trimmed = module_count - strings * best_len
    return StringPlan(
        modules_per_string=best_len,
        strings=strings,
        modules_min=modules_min,
        modules_max=modules_max,
        trimmed_modules=trimmed,
        inverter_count=inverter_count,
        mppt_capacity=capacity,
        design_min_ambient_c=min_ambient_c,
        design_max_cell_c=max_cell_c,
    )


# --- Paketleme ------------------------------------------------------------------


def _basis(azimuth_deg: float) -> tuple[Point, Point]:
    """(sıra boyu, eğim boyu) birim vektörleri — yerel çerçevede (x=doğu, y=kuzey).

    `along_slope` dizinin baktığı yöne (azimut) işaret eder; sıralar bu yönde
    ilerler. `along_row` ona diktir. İkili sağ el sistemidir (çapraz çarpım +1),
    böylece bu tabanda CCW sıralanan köşeler yerel çerçevede de CCW kalır —
    `geometry` yüklemleri CCW sarım varsayıyor.
    """
    rad = math.radians(azimuth_deg)
    along_slope = (math.sin(rad), math.cos(rad))
    along_row = (math.cos(rad), -math.sin(rad))
    return along_row, along_slope


def _panel_rect(
    center: Point, along_row: Point, along_slope: Point, width_m: float, depth_m: float
) -> Ring:
    half_w, half_d = width_m / 2.0, depth_m / 2.0
    corners = [
        (-half_w, -half_d),
        (half_w, -half_d),
        (half_w, half_d),
        (-half_w, half_d),
    ]
    return normalize_ring(
        tuple(
            (
                center[0] + u * along_row[0] + v * along_slope[0],
                center[1] + u * along_row[1] + v * along_slope[1],
            )
            for u, v in corners
        )
    )


def _accepts(
    rect: Ring,
    center: Point,
    ring: Ring,
    mounting: MountingSpec,
    half_diagonal: float,
) -> bool:
    """Panel kabul edilir mi — hızlı yol önce, tam yüklem yalnızca sınıra yakında."""
    inside = point_in_ring(center, ring)
    boundary_distance = distance_point_to_ring(center, ring)
    if not inside and boundary_distance > half_diagonal:
        return False  # merkez dışarıda ve uzak → panelin tamamı dışarıda
    fast_accept = inside and boundary_distance >= mounting.setback_m + half_diagonal
    if not fast_accept and not rect_fits_inside(rect, ring, mounting.setback_m):
        return False

    for obstacle in mounting.obstacles:
        if distance_point_to_ring(center, obstacle.polygon) > (
            half_diagonal + mounting.obstacle_clearance_m
        ) and not point_in_ring(center, obstacle.polygon):
            continue  # engelden yeterince uzak
        if not rect_clears_obstacle(rect, obstacle.polygon, mounting.obstacle_clearance_m):
            return False
    return True


def _pack_phase(
    ring: Ring,
    mounting: MountingSpec,
    along_row: Point,
    along_slope: Point,
    panel_width_m: float,
    projected_depth_m: float,
    row_pitch_m: float,
    phase_row: float,
    phase_slope: float,
) -> list[PanelPlacement]:
    """Tek ızgara fazı için paneli yerleştirir."""
    col_step = panel_width_m + mounting.module_gap_m
    half_diagonal = math.hypot(panel_width_m, projected_depth_m) / 2.0

    # Poligonun taban üzerindeki izdüşüm aralıkları
    row_coords = [p[0] * along_row[0] + p[1] * along_row[1] for p in ring]
    slope_coords = [p[0] * along_slope[0] + p[1] * along_slope[1] for p in ring]
    row_min, row_max = min(row_coords), max(row_coords)
    slope_min, slope_max = min(slope_coords), max(slope_coords)

    placements: list[PanelPlacement] = []
    row_index = 0
    slope_center = slope_min + projected_depth_m / 2.0 + phase_slope
    while slope_center - projected_depth_m / 2.0 <= slope_max:
        col_index = 0
        row_center = row_min + panel_width_m / 2.0 + phase_row
        while row_center - panel_width_m / 2.0 <= row_max:
            center = (
                row_center * along_row[0] + slope_center * along_slope[0],
                row_center * along_row[1] + slope_center * along_slope[1],
            )
            rect = _panel_rect(
                center, along_row, along_slope, panel_width_m, projected_depth_m
            )
            if _accepts(rect, center, ring, mounting, half_diagonal):
                placements.append(PanelPlacement(rect=rect, row=row_index, col=col_index))
            col_index += 1
            row_center += col_step
        row_index += 1
        slope_center += row_pitch_m
    return placements


def pack_panels(
    ring: Ring,
    latitude: float,
    longitude: float,
    min_ambient_c: float,
    max_cell_c: float,
    module: ModuleSpec | None = None,
    inverter: InverterSpec | None = None,
    mounting: MountingSpec | None = None,
) -> LayoutResult:
    """Poligona en çok panel sığdıran yerleşimi üretir.

    `ring` yerel çerçevede (metre) ve yatay izdüşümdedir — `geometry.LocalFrame`
    ile WGS84'ten çevrilir. `min_ambient_c` / `max_cell_c` string boyutlandırma
    içindir ve sahanın TMY'sinden gelir.

    Yönelim (dikey/yatay) ve ızgara fazı üzerinden arama yapılır; ölçüt panel
    sayısıdır. Eşitlikte yatay yönelim tercih edilir: aynı panel sayısında sıra
    yüksekliği daha az olur, bu da rüzgâr yükünü ve (açılı montajda) gereken sıra
    aralığını düşürür.
    """
    module = module or ModuleSpec()
    inverter = inverter or InverterSpec()
    mounting = mounting or MountingSpec()

    ring = normalize_ring(ring)
    if len(ring) < 3:
        raise ValueError("poligon en az 3 köşe içermeli")
    if not ring_is_simple(ring):
        raise ValueError(
            "poligon kendisiyle kesişiyor; alan ve kapsama hesabı anlamsız olur — "
            "çizimi düzeltmek gerek"
        )
    area = polygon_area_m2(ring)
    if area <= 0.0:
        raise ValueError("poligon alanı sıfır")

    along_row, along_slope = _basis(mounting.azimuth_deg)
    cos_tilt = math.cos(math.radians(mounting.tilt_deg))

    best: tuple[int, Orientation, float, float, list[PanelPlacement]] | None = None
    for orientation in (Orientation.LANDSCAPE, Orientation.PORTRAIT):
        panel_width, slant = module.dimensions(orientation)
        projected_depth = slant * cos_tilt
        pitch = _resolve_row_pitch(latitude, longitude, slant, projected_depth, mounting)

        for row_step in range(_PHASE_STEPS):
            for slope_step in range(_PHASE_STEPS):
                phase_row = -(panel_width + mounting.module_gap_m) * row_step / _PHASE_STEPS
                phase_slope = -pitch * slope_step / _PHASE_STEPS
                placements = _pack_phase(
                    ring,
                    mounting,
                    along_row,
                    along_slope,
                    panel_width,
                    projected_depth,
                    pitch,
                    phase_row,
                    phase_slope,
                )
                if best is None or len(placements) > best[0]:
                    best = (len(placements), orientation, pitch, slant, placements)

    assert best is not None  # döngü en az bir kez çalışır
    count, orientation, pitch, slant, placements = best

    plan = plan_strings(count, module, inverter, min_ambient_c, max_cell_c)
    # Artık paneller yerleşimden çıkarılır ki raporlanan kapasite kurulabilir
    # tasarımla birebir örtüşsün. Sıra/kolon sırası korunur: aynı string'in
    # panelleri fiziksel olarak komşu olur, bu da gölgelenmede uyumsuzluk
    # kaybını azaltır (string akımı en kötü modülüyle sınırlıdır).
    ordered = sorted(placements, key=lambda p: (p.row, p.col))[: plan.total_modules]
    assigned = tuple(
        replace(item, string_index=index // plan.modules_per_string)
        for index, item in enumerate(ordered)
    ) if plan.modules_per_string else ()

    # Dolu sıra sayısı — ızgara fazı kaydığında kenardaki sıra tamamen boş
    # kalabilir, `max(row) + 1` onu da sayıp kullanıcıya yanlış sıra sayısı verir.
    rows = len({p.row for p in assigned})
    gcr = 1.0 if mounting.mount is MountType.ROOFTOP else min(0.99, slant / pitch)

    logger.info(
        "yerleşim: %d panel (%s), %.1f kWp, aralık %.2f m, gcr %.2f, doluluk %.0f%%",
        len(assigned),
        orientation,
        len(assigned) * module.pdc0_w / 1000.0,
        pitch,
        gcr,
        100.0 * len(assigned) * module.area_m2 / area,
    )
    return LayoutResult(
        placements=assigned,
        orientation=orientation,
        module=module,
        inverter=inverter,
        mounting=mounting,
        row_pitch_m=pitch,
        collector_width_m=slant,
        gcr=gcr,
        area_m2=area,
        string_plan=plan,
        rows=rows,
    )


def layout_bounds(result: LayoutResult) -> tuple[float, float, float, float]:
    """Tüm panelleri kapsayan sınır kutusu — 2D çizimde görünümü ortalamak için."""
    if not result.placements:
        return (0.0, 0.0, 0.0, 0.0)
    corners: list[Point] = [c for p in result.placements for c in p.rect]
    return bounding_box(tuple(corners))
