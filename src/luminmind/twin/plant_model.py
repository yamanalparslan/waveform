"""Tesis elektrik modelinin konfigürasyonu (pvlib girdileri).

Panel datasheet parametreleri (`pv_arrays.module_params`) PVWatts DC modeline
eşlenir — jenerik ve az parametreli olduğundan datasheet'i henüz elimizde
olmayan tesisler için de çalışır. Gerçek modül/invertör parametreleri geldiğinde
yalnızca `ArrayConfig` alanları doldurulur (PLAN.md açık soru #3).

Önceki sürüm pvlib `ModelChain` kullanıyordu. ModelChain sıra-arası (self)
gölgelenmeyi, kirlilik durumunu ve ayrı ayrı IAM bileşenlerini modelleyemediği
için zincir `twin/pipeline.py` içinde açıkça kuruldu; bu dosya yalnızca
konfigürasyon ve türetilmiş geometriyi taşır.

**İnvertör kırpması:** `inverter_ac_kw` boşsa DC kapasite `dc_ac_ratio`'ya
bölünerek türetilir. Eski sürümde invertörün DC girdi limiti tüm dizi
kapasitesine eşitlenmişti; bu, DC/AC oranını 1,0 yapıp kırpmayı tamamen yok
ediyordu — gerçek santral öğlen kırparken ikiz kırpmadığı için her açık günde
sahte "eksik üretim" üretiliyordu.
"""

import math
from dataclasses import dataclass
from enum import StrEnum

from pvlib.temperature import TEMPERATURE_MODEL_PARAMETERS

# Türkiye'deki tipik saha kurulumu: 1,15–1,25 bandı. Datasheet gelene kadar
# invertör AC anma gücü bu oranla türetilir.
DEFAULT_DC_AC_RATIO = 1.2


class MountType(StrEnum):
    """Montaj tipi — termal model ve sıra-arası gölgelenmeyi belirler."""

    FIXED_GROUND = "fixed_ground"  # sabit açılı arazi santrali (sıralı diziler)
    ROOFTOP = "rooftop"  # eğimli çatıya paralel (yakın montaj, tek düzlem)
    # Düz çatıya ballastlı açılı diziler. `ROOFTOP`'tan ayrı bir tip olması
    # gerekiyor çünkü sıralar aralıklıdır ve birbirini gölgeler: `ROOFTOP` ile
    # modellenirse sıra-arası gölgelenme hiç hesaplanmaz ve düz sanayi çatısı
    # sistematik olarak iyimser çıkar. Termal olarak açık kasa gibi davranır
    # (iki yüzü de havalanır), ama rüzgar kentsel sınır tabakasındadır.
    ROOFTOP_TILTED = "rooftop_tilted"
    SINGLE_AXIS_TRACKER = "single_axis_tracker"  # tek eksenli izleyici


_TEMPERATURE_MODEL_KEY = {
    MountType.FIXED_GROUND: "open_rack_glass_glass",
    MountType.ROOFTOP: "close_mount_glass_glass",
    MountType.ROOFTOP_TILTED: "open_rack_glass_glass",
    MountType.SINGLE_AXIS_TRACKER: "open_rack_glass_glass",
}

# Rüzgar hızı 10 m'de ölçülür; SAPM termal modeli modül yüksekliğindeki hızı
# bekler. Hellmann üstel yasası ile indirgenir (açık arazi α≈0,14, kentsel 0,25).
_WIND_EXPONENT = {
    MountType.FIXED_GROUND: 0.14,
    MountType.ROOFTOP: 0.25,
    MountType.ROOFTOP_TILTED: 0.25,
    MountType.SINGLE_AXIS_TRACKER: 0.14,
}


@dataclass(frozen=True)
class ArrayConfig:
    """Bir PV dizisinin modele giren parametreleri (pv_arrays satırıyla eşleşir)."""

    tilt_deg: float
    azimuth_deg: float  # 180 = güney
    modules_per_string: int
    strings: int
    module_pdc0_w: float = 550.0  # STC modül gücü
    gamma_pdc: float = -0.0035  # sıcaklık katsayısı (1/°C)

    # İnvertör
    inverter_ac_kw: float | None = None  # AC anma gücü; None → dc_capacity/dc_ac_ratio
    dc_ac_ratio: float = DEFAULT_DC_AC_RATIO
    inverter_eta_nom: float = 0.96

    # Montaj geometrisi
    mount: MountType = MountType.FIXED_GROUND
    gcr: float = 0.40  # ground coverage ratio (kollektör genişliği / sıra aralığı)
    collector_width_m: float = 2.3  # dikey doğrultuda tek sıranın genişliği
    ground_clearance_m: float = 1.0  # alt kenarın yerden yüksekliği
    albedo: float = 0.20  # toprak/çim; kar veya beton için tesis bazında ayarlanır
    bifaciality: float = 0.0  # 0 = monofasiyel; bifasiyel modüllerde 0,65–0,80

    # İzleyici (yalnızca SINGLE_AXIS_TRACKER)
    axis_tilt_deg: float = 0.0
    axis_azimuth_deg: float = 180.0
    max_tracker_angle_deg: float = 60.0
    backtrack: bool = True

    # Spektral düzeltme için modül teknolojisi (pvlib firstsolar katsayı seti)
    module_type: str = "monosi"

    def __post_init__(self) -> None:
        if not 0.0 < self.gcr <= 1.0:
            raise ValueError(f"gcr must be in (0, 1], got {self.gcr}")
        if self.dc_ac_ratio <= 0:
            raise ValueError(f"dc_ac_ratio must be positive, got {self.dc_ac_ratio}")
        if not 0.0 <= self.albedo <= 1.0:
            raise ValueError(f"albedo must be in [0, 1], got {self.albedo}")

    @property
    def dc_capacity_w(self) -> float:
        return self.module_pdc0_w * self.modules_per_string * self.strings

    @property
    def inverter_ac_capacity_w(self) -> float:
        """İnvertörün AC anma gücü (kırpma seviyesi)."""
        if self.inverter_ac_kw is not None:
            return self.inverter_ac_kw * 1000.0
        return self.dc_capacity_w / self.dc_ac_ratio

    @property
    def inverter_pdc0_w(self) -> float:
        """pvlib `inverter.pvwatts` için DC girdi limiti = AC anma / nominal verim."""
        return self.inverter_ac_capacity_w / self.inverter_eta_nom

    @property
    def row_pitch_m(self) -> float:
        """Sıra aralığı — infinite_sheds gölgelenme modelinin girdisi."""
        return self.collector_width_m / self.gcr

    @property
    def row_height_m(self) -> float:
        """Kollektör orta noktasının yerden yüksekliği (infinite_sheds `height`)."""
        rise = self.collector_width_m * math.sin(math.radians(self.tilt_deg)) / 2.0
        return self.ground_clearance_m + rise

    @property
    def models_row_shading(self) -> bool:
        """Yalnızca çatıya *paralel* montajda sıra-arası gölgelenme yoktur.

        Eğimli çatıya yatırılan paneller tek düzlemdedir, birbirini gölgelemez.
        Düz çatıya açılı kurulan diziler (`ROOFTOP_TILTED`) ise araziyle aynı
        geometriye sahiptir ve gölgelenir.
        """
        return self.mount is not MountType.ROOFTOP

    @property
    def temperature_model_params(self) -> dict[str, float]:
        params: dict[str, float] = TEMPERATURE_MODEL_PARAMETERS["sapm"][
            _TEMPERATURE_MODEL_KEY[self.mount]
        ]
        return params

    @property
    def wind_shear_exponent(self) -> float:
        return _WIND_EXPONENT[self.mount]


def default_array_for_capacity(
    dc_capacity_kwp: float,
    tilt_deg: float = 25.0,
    azimuth_deg: float = 180.0,
    ac_capacity_kw: float | None = None,
    mount: MountType = MountType.FIXED_GROUND,
) -> ArrayConfig:
    """Datasheet'i bilinmeyen tesis için kapasiteden jenerik dizi türetir."""
    total_modules = max(1, round(dc_capacity_kwp * 1000 / 550.0))
    modules_per_string = min(25, total_modules)
    strings = max(1, math.ceil(total_modules / modules_per_string))
    return ArrayConfig(
        tilt_deg=tilt_deg,
        azimuth_deg=azimuth_deg,
        modules_per_string=modules_per_string,
        strings=strings,
        inverter_ac_kw=ac_capacity_kw,
        mount=mount,
    )
