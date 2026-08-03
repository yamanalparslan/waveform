"""Kurulmamış santralin üretim simülasyonu: TMY + yerleşim → 25 yıllık enerji.

Fizik `twin.pipeline.run_chain`'den gelir; bu dosya onu TMY'ye bağlar, IEC 61724
performans göstergelerini üretir, kayıp şelalesini çıkarır ve bozunumla 25 yıla
projeksiyon yapar.

**Neden zincir bir kez koşuluyor.** 25 yılın her biri için zinciri baştan
çalıştırmak güneş geometrisini, transpozisyonu ve termal modeli 25 kez tekrar
hesaplamak demek — saniyeler mertebesinde ve interaktif bir araçta hissedilir.
Bozunum ise yalnızca `LossChain.dc_factor`'ü ölçekleyen bir skalerdir. Bu yüzden
zincir bir kez koşulup DC serisi bozunum oranıyla yeniden ölçekleniyor, ardından
kırpma ve invertör modeli her yıl için tekrar uygulanıyor.

Kırpmanın tekrar uygulanması şart, çünkü bozunum ve kırpma *doğrusal
etkileşir*: DC/AC oranı yüksek bir sahada ilk yıllarda öğle saatleri kırpılır ve
bozunumun bir kısmı kırpılan tepeden yenir — yani ilk yıllarda üretim
bozunumdan daha yavaş düşer. Çıktıyı tek bir skalerle ölçeklemek bu etkiyi yok
sayıp ilk yılların gelirini olduğundan az gösterirdi.

**P50 ve P90.** TMY bir P50 kestirimidir. Finansmanda P90 istenir; burada
parametrik olarak üretiliyor (bkz. `YieldUncertainty`) — veriden türetilmiş bir
güven aralığı değil, bileşenleri açıkça yazılmış bir varsayım.
"""

import logging
import math
from dataclasses import dataclass, replace
from datetime import timedelta

import pandas as pd

from luminmind.prospect.layout import LayoutResult
from luminmind.prospect.pvgis import TMY_INTERVAL, TmyDataset
from luminmind.twin.components import LossChain
from luminmind.twin.pipeline import ChainResult, ac_power, run_chain
from luminmind.twin.plant_model import ArrayConfig, MountType
from luminmind.twin.weather import IrradianceStamp

logger = logging.getLogger(__name__)

G_STC_WM2 = 1000.0

# Varsayılan proje ömrü — modül üreticilerinin performans garantisi bu mertebede.
DEFAULT_LIFETIME_YEARS = 25

# Yıllık bozunum. İlk yılın LID kaybı `LossChain.light_induced_degradation`
# tarafından ayrıca taşınıyor, bu yüzden buradaki oran saf yaşlanmadır.
DEFAULT_ANNUAL_DEGRADATION = 0.005

# Ross katsayısı (K·m²/W): hücre sıcaklığının ışınımla artış hızı. String
# boyutlandırmada azami hücre sıcaklığını kestirmek için kullanılır — tam termal
# model dizi konfigürasyonunu ister, dizi konfigürasyonu ise string planını,
# yani döngü oluşur. Bu yaklaşım döngüyü kırar.
_ROSS_COEFFICIENT = {
    MountType.ROOFTOP: 0.056,  # çatıya paralel, arka yüz havalanmıyor
    MountType.ROOFTOP_TILTED: 0.030,
    MountType.FIXED_GROUND: 0.030,
    MountType.SINGLE_AXIS_TRACKER: 0.030,
}

# Ross yaklaşımına eklenen güvenlik payı (°C). Konya TMY'sinde yaklaşım 64,2 °C
# verirken tam termal zincir 68,6 °C buluyor: yaklaşım yatay ışınım kullandığı
# ve geçici rejim ısınmasını (Prilliman) görmediği için sistematik olarak alçak
# kalıyor. String boyutlandırmada bu yönde hata, MPPT alt sınırını olduğundan
# rahat göstererek gereğinden kısa string'e izin verir; pay ekleyip güvenli
# tarafta kalıyoruz.
_CELL_TEMP_MARGIN_C = 5.0


@dataclass(frozen=True)
class YieldUncertainty:
    """Yıllık üretim belirsizliğinin bileşenleri (bağıl standart sapma).

    Bileşenler bağımsız kabul edilip karesel toplanır. Değerler sektörde
    yerleşik mertebelerdir, bu sahanın verisinden türetilmemiştir — raporda
    öyle sunulmamalı.
    """

    interannual_variability: float = 0.04  # yıllar arası ışınım değişkenliği
    irradiance_model: float = 0.03  # uydu ışınımının kendi hatası
    system_model: float = 0.03  # transpozisyon, termal, invertör modeli
    soiling_availability: float = 0.02  # kirlilik ve erişilebilirlik sapması

    @property
    def combined(self) -> float:
        return math.sqrt(
            self.interannual_variability**2
            + self.irradiance_model**2
            + self.system_model**2
            + self.soiling_availability**2
        )

    def percentile_factor(self, exceedance: float) -> float:
        """P(exceedance) için P50'ye uygulanacak çarpan (ör. P90 → ~0,92).

        Normal dağılım varsayılır. P90 "yılların %90'ında bu değerin üstünde
        üretilir" demektir, yani P50'nin altındadır.
        """
        from scipy.stats import norm  # yerel içe alma: modül yükünü hafif tutar

        z = float(norm.ppf(1.0 - exceedance))
        return 1.0 + z * self.combined


@dataclass(frozen=True)
class LossStage:
    """Kayıp şelalesinde tek adım."""

    label: str
    energy_kwh: float  # bu adımdan *sonra* kalan enerji
    loss_kwh: float  # bu adımda kaybedilen
    loss_pct: float  # referans enerjiye oranla


@dataclass(frozen=True)
class YearProjection:
    year: int  # 1 = ilk işletme yılı
    energy_kwh: float
    degradation_factor: float  # ilk yıla göre kalan oran


@dataclass(frozen=True)
class SimulationResult:
    """Simülasyon çıktısı — rapor ve finansal motorun tek girdisi."""

    layout: LayoutResult
    losses: LossChain
    uncertainty: YieldUncertainty
    # Sıfır yaşlı ("as-new") yıllık üretim. Özgül üretim ve performans oranı
    # sektör sözleşmesi gereği bu değerden hesaplanır. **Gelir hesabında
    # kullanılmaz** — `projection[0]` yıl ortası yaşı (0,5 yıl) içerdiği için
    # ondan ≈%0,25 düşüktür ve nakit akışının doğru girdisi odur. İkisini
    # karıştırmak 25 yıllık geliri sistematik olarak yukarı kaydırır.
    year_one_kwh: float
    monthly_kwh: tuple[float, ...]  # 12 eleman, Ocak → Aralık
    poa_kwh_m2: float
    ghi_kwh_m2: float
    max_cell_temp_c: float
    mean_cell_temp_c: float
    clipping_loss_kwh: float
    mean_shaded_fraction: float
    waterfall: tuple[LossStage, ...]
    projection: tuple[YearProjection, ...]
    provenance: str

    @property
    def dc_capacity_kwp(self) -> float:
        return self.layout.dc_capacity_kwp

    @property
    def specific_yield_kwh_kwp(self) -> float:
        """Özgül üretim (kWh/kWp/yıl) — sahaları kıyaslamanın tek sayısı."""
        capacity = self.dc_capacity_kwp
        return self.year_one_kwh / capacity if capacity > 0 else 0.0

    @property
    def performance_ratio(self) -> float:
        """IEC 61724 performans oranı: PR = Y_f / Y_r.

        `analytics.rollup.performance_ratio` ile *aynı şey değil*: oradaki oran
        gerçek üretimin dijital ikizin beklentisine bölümüdür (izleme
        göstergesi). Buradaki ise üretimin düzlem üstü ışınımdan türeyen
        referans verime bölümüdür — kurulmamış santralde ölçüm yok, kıyas
        ışınımın kendisidir.
        """
        reference_yield = self.poa_kwh_m2 / (G_STC_WM2 / 1000.0)
        capacity = self.dc_capacity_kwp
        if reference_yield <= 0 or capacity <= 0:
            return 0.0
        return (self.year_one_kwh / capacity) / reference_yield

    @property
    def lifetime_kwh(self) -> float:
        return sum(p.energy_kwh for p in self.projection)

    def percentile_kwh(self, exceedance: float = 0.90) -> float:
        return self.year_one_kwh * self.uncertainty.percentile_factor(exceedance)


def estimate_max_cell_temp(tmy: TmyDataset, mount: MountType) -> float:
    """TMY'den azami hücre sıcaklığı kestirimi (Ross yaklaşımı).

    `T_hücre ≈ T_hava + k · G_yatay + pay`. Yatay ışınım kullanılıyor çünkü bu
    kestirim düzlem üstü ışınımdan *önce*, string boyutlandırma aşamasında
    gerekiyor. Eğik düzlemde yazın ışınım yataya yakındır, dolayısıyla azami
    değer için kabul edilebilir bir vekildir; kalan sapma
    `_CELL_TEMP_MARGIN_C` ile güvenli tarafa alınır.
    """
    k = _ROSS_COEFFICIENT[mount]
    cell = tmy.weather["temp_air"] + k * tmy.weather["ghi"]
    return float(cell.max()) + _CELL_TEMP_MARGIN_C


def design_temperatures(tmy: TmyDataset, mount: MountType) -> tuple[float, float]:
    """String boyutlandırmanın iki uç sıcaklığı: (en düşük ortam, en yüksek hücre).

    Sahanın kendi TMY'sinden gelir. Sabit bir "−10 °C" varsayımı Antalya'da
    gereksiz kısa string (kapasite kaybı), Ağrı'da ise invertörün azami DC
    gerilimini aşan tehlikeli uzun string üretirdi.
    """
    return float(tmy.weather["temp_air"].min()), estimate_max_cell_temp(tmy, mount)


def _energy_kwh(power_w: pd.Series, interval: timedelta = TMY_INTERVAL) -> float:
    """Güç serisini enerjiye çevirir. Saatlik ortalama × 1 sa = Wh."""
    hours = interval.total_seconds() / 3600.0
    return float(power_w.sum()) * hours / 1000.0


def _build_waterfall(
    chain: ChainResult,
    losses: LossChain,
    dc_capacity_kwp: float,
) -> tuple[LossStage, ...]:
    """IEC 61724 mantığında kayıp şelalesi.

    Referans enerji düzlem üstü ışınımın STC'ye oranından türer; her adım bir
    öncekinden ne kadar götürdüğünü gösterir. Adımların ürettiği toplam kayıp
    referans ile net enerjinin farkına *eşittir* — şelale kapanır, aksi halde
    "kayıp nerede" sorusu cevapsız kalır.
    """
    # (kWh/m²) × kWp ÷ (1 kW/m²) = kWh. G_STC 1 kW/m² olduğu için bölen birimdir.
    reference_kwh = _energy_kwh(chain.poa_global) * dc_capacity_kwp
    optical_kwh = _energy_kwh(chain.effective_irradiance) * dc_capacity_kwp

    # `dc_potential_w` içinde DC kayıp yığını uygulanmış durumda; skaler olduğu
    # için bölünerek geri alınır ve yalnızca sıcaklık etkisi kalan enerji bulunur.
    dc_factor = losses.dc_factor
    dc_loss_kwh = _energy_kwh(chain.dc_potential_w)
    thermal_kwh = dc_loss_kwh / dc_factor if dc_factor > 0 else 0.0
    clipped_kwh = _energy_kwh(chain.dc_w)
    ac_factor = losses.ac_factor
    inverter_kwh = _energy_kwh(chain.ac_w) / ac_factor if ac_factor > 0 else 0.0
    net_kwh = _energy_kwh(chain.ac_w)

    steps = [
        ("Düzlem üstü ışınım (referans)", reference_kwh),
        ("Optik: gölge, IAM, spektrum, kirlilik", optical_kwh),
        ("Hücre sıcaklığı", thermal_kwh),
        ("DC kayıpları: uyumsuzluk, kablo, LID, etiket", dc_loss_kwh),
        ("İnvertör kırpması", clipped_kwh),
        ("İnvertör dönüşümü", inverter_kwh),
        ("AC kayıpları: kablo, trafo, erişilebilirlik", net_kwh),
    ]
    stages: list[LossStage] = []
    previous = reference_kwh
    for label, remaining in steps:
        loss = previous - remaining
        stages.append(
            LossStage(
                label=label,
                energy_kwh=remaining,
                loss_kwh=loss,
                loss_pct=100.0 * loss / reference_kwh if reference_kwh > 0 else 0.0,
            )
        )
        previous = remaining
    return tuple(stages)


def _project_lifetime(
    array: ArrayConfig,
    chain: ChainResult,
    losses: LossChain,
    lifetime_years: int,
    annual_degradation: float,
) -> tuple[YearProjection, ...]:
    """Bozunumu DC tarafına uygulayıp kırpma ve invertörü her yıl yeniden çözer.

    `chain.dc_potential_w` içinde `losses.dc_factor` zaten uygulanmış durumda;
    skaler olduğu için bölünerek geri alınır ve yıla özgü oranla yeniden
    çarpılır. Ardından kırpma + invertör + AC kayıpları `pipeline.ac_power` ve
    `LossChain.apply_ac` ile — yani yıl-1 ile *aynı kod yoluyla* — uygulanır.
    """
    base_factor = losses.dc_factor
    if base_factor <= 0:
        return ()
    bare_dc_w = chain.dc_potential_w / base_factor

    projections: list[YearProjection] = []
    first_year_kwh = 0.0
    for year in range(1, lifetime_years + 1):
        # Yıl ortası yaş: yıllık enerji o yılın ortalama bozunumuyla hesaplanır
        aged = losses.with_age(year - 0.5, annual_degradation)
        dc_w = bare_dc_w * aged.dc_factor
        ac_w, _ = ac_power(array, dc_w)
        energy = _energy_kwh(aged.apply_ac(ac_w))
        if year == 1:
            first_year_kwh = energy
        projections.append(
            YearProjection(
                year=year,
                energy_kwh=energy,
                degradation_factor=energy / first_year_kwh if first_year_kwh > 0 else 0.0,
            )
        )
    return tuple(projections)


def compute_external_shading(
    layout: LayoutResult,
    tmy: TmyDataset,
) -> pd.Series | None:
    """3B engellerin yüksekliğine ve güneş açısına göre kaba gölge oranı hesaplar.

    Gerçek 3B ışın izleme (raycasting) yerine, her saatin güneş yüksekliği (zenith)
    üzerinden engelin gölge alanını tahmin eder ve toplam PV alanına böler.
    """
    import numpy as np
    from pvlib.solarposition import get_solarposition

    from luminmind.prospect.geometry import polygon_area_m2

    obstacles = layout.mounting.obstacles
    if not any(obs.height_m > 0 for obs in obstacles):
        return None

    # Yaklaşık PV alanı (m²) — %21 verim varsayımı (1 kWp ≈ 4.76 m²)
    array_area = layout.dc_capacity_kwp / 0.21
    if array_area <= 0:
        return None

    index = pd.DatetimeIndex(tmy.weather.index)
    solpos = get_solarposition(
        index,
        tmy.site.latitude,
        tmy.site.longitude,
        tmy.site.altitude_m,
    )

    zenith = solpos["apparent_zenith"].values
    # Güneş çok alçakken (zenith > 85) gölge boyu sonsuza gider; 85'te kesiyoruz (tan(85) ≈ 11.4)
    z_rad = np.radians(np.clip(zenith, 0, 85.0))
    tan_z = np.tan(z_rad)

    total_shaded_area = np.zeros(len(index))

    for obs in obstacles:
        if obs.height_m <= 0:
            continue
        # Engelin etkin genişliği olarak alanının karekökü (yaklaşık bir küp/prizma varsayımı)
        obs_area = polygon_area_m2(obs.polygon)
        width = math.sqrt(obs_area) if obs_area > 0 else 1.0

        shadow_length = obs.height_m * tan_z
        total_shaded_area += width * shadow_length

    shaded_fraction = total_shaded_area / array_area
    shaded_fraction = np.clip(shaded_fraction, 0.0, 1.0)
    shaded_fraction[zenith > 89.0] = 0.0

    return pd.Series(shaded_fraction, index=index)


def simulate(
    tmy: TmyDataset,
    layout: LayoutResult,
    losses: LossChain | None = None,
    uncertainty: YieldUncertainty | None = None,
    lifetime_years: int = DEFAULT_LIFETIME_YEARS,
    annual_degradation: float = DEFAULT_ANNUAL_DEGRADATION,
) -> SimulationResult:
    """Yerleşim + TMY → yıllık üretim, göstergeler, kayıp şelalesi ve projeksiyon.

    `losses` verilmezse PVWatts tipik saha değerleri kullanılır — kurulmamış
    santralde kalibrasyon yok, bu yüzden kayıp varsayımları raporda açıkça
    gösterilmeli (`LossChain` alanları birebir yazdırılabilir).
    """
    losses = losses or LossChain()
    uncertainty = uncertainty or YieldUncertainty()
    array = layout.to_array_config()

    ext_shading = compute_external_shading(layout, tmy)

    # PVGIS indeksi ışınımın temsil ettiği andır (bkz. prospect.pvgis) — bu
    # yüzden aralık ortası düzeltmesi *yapılmaz*, damga anlıktır.
    chain = run_chain(
        array=array,
        site=tmy.site,
        weather=tmy.weather,
        losses=losses,
        stamp=IrradianceStamp.INSTANT,
        interval=TMY_INTERVAL,
        external_shading=ext_shading,
    )

    year_one = _energy_kwh(chain.ac_w)
    monthly = tuple(
        _energy_kwh(chain.ac_w[chain.ac_w.index.month == month]) for month in range(1, 13)
    )
    daytime = chain.daytime & chain.irradiance_valid

    result = SimulationResult(
        layout=layout,
        losses=losses,
        uncertainty=uncertainty,
        year_one_kwh=year_one,
        monthly_kwh=monthly,
        # `poa_global` W/m² olduğundan aynı toplama kWh/m² verir
        poa_kwh_m2=_energy_kwh(chain.poa_global),
        ghi_kwh_m2=tmy.annual_ghi_kwh_m2,
        max_cell_temp_c=float(chain.cell_temp_c.max()),
        mean_cell_temp_c=float(chain.cell_temp_c[daytime].mean()),
        clipping_loss_kwh=_energy_kwh(chain.clipping_loss_w),
        mean_shaded_fraction=float(chain.shaded_fraction[daytime].mean()),
        waterfall=_build_waterfall(chain, losses, layout.dc_capacity_kwp),
        projection=_project_lifetime(
            array, chain, losses, lifetime_years, annual_degradation
        ),
        provenance=tmy.provenance,
    )
    logger.info(
        "simülasyon: %.1f kWp → %.0f MWh/yıl, özgül %.0f kWh/kWp, PR %.3f, kırpma %.1f MWh",
        layout.dc_capacity_kwp,
        year_one / 1000.0,
        result.specific_yield_kwh_kwp,
        result.performance_ratio,
        result.clipping_loss_kwh / 1000.0,
    )
    return result


def compare_tilts(
    tmy: TmyDataset,
    layout: LayoutResult,
    tilts: tuple[float, ...] = (10.0, 15.0, 20.0, 25.0, 30.0, 35.0),
) -> tuple[tuple[float, float], ...]:
    """Farklı eğimler için özgül üretim — optimum eğimi göstermek için.

    Yalnızca eğim değiştirilir; yerleşim (dolayısıyla panel sayısı) sabit
    tutulur. Gerçek optimum eğim panel sayısını da değiştirir (dik açı → geniş
    sıra aralığı → az panel) ve o karşılaştırma finansal motorda, NPV üzerinden
    yapılmalıdır. Bu fonksiyon "aynı sistemi kaç derece yatırmalıyım" sorusunu
    cevaplar, "kaç panel koymalıyım" sorusunu değil.
    """
    results: list[tuple[float, float]] = []
    for tilt in tilts:
        candidate = replace(layout, mounting=replace(layout.mounting, tilt_deg=tilt))
        chain = run_chain(
            array=candidate.to_array_config(),
            site=tmy.site,
            weather=tmy.weather,
            losses=LossChain(),
            stamp=IrradianceStamp.INSTANT,
            interval=TMY_INTERVAL,
        )
        energy = _energy_kwh(chain.ac_w)
        capacity = candidate.dc_capacity_kwp
        results.append((tilt, energy / capacity if capacity > 0 else 0.0))
    return tuple(results)
