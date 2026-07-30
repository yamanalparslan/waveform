"""Açık pvlib zinciri: hava verisi → sayaç noktasındaki AC güç.

pvlib `ModelChain` yerine adımlar burada açıkça kurulur. Gerekçe: ModelChain
sıra-arası (self) gölgelenmeyi, ayrıştırılmış IAM bileşenlerini, dinamik
kirliliği ve invertör kırpmasının DC kayıplarından **sonra** uygulanmasını
modelleyemiyor. Bunların hepsi tesis ölçeğinde birkaç puanlık sistematik hata
kaynağı ve sistematik hata, sapma tabanlı anomali tespitinde doğrudan sahte
alarma dönüşüyor.

Zincir (her adım bir öncekinin çıktısını tüketir):

1. **Güneş geometrisi** — ışınım aralık ortalaması olduğundan geometri aralığın
   *orta noktasında* hesaplanır (`IrradianceStamp`).
2. **Işınım tutarlılığı** — GHI ≈ DHI + DNI·cos(z) kapanışı denetlenir; kapanış
   bozuksa veya DNI/DHI eksikse GHI'dan Erbs ayrıştırmasıyla yeniden türetilir.
   GHI en güvenilir ölçüdür, referans odur.
3. **Düzleme taşıma (transposition)** — arazi santrallerinde `infinite_sheds`
   (sıra-arası gölgelenme dahil), çatıda Perez.
4. **Optik kayıplar** — direkt/gökyüzü difüz/yer yansımalı difüz bileşenlerine
   ayrı ayrı IAM, ardından spektral düzeltme ve dinamik kirlilik.
5. **Hücre sıcaklığı** — rüzgar modül yüksekliğine indirgenir (10 m ölçümü
   doğrudan kullanmak sistematik olarak fazla soğutur), SAPM + Prilliman
   geçici rejim düzeltmesi (15 dk veri için anlamlı).
6. **DC güç** — PVWatts, ardından DC kayıp yığını.
7. **İnvertör** — DC girişi kırpılır (gerçek santral davranışı), PVWatts verim
   eğrisi, ardından AC kayıp yığını.

Gece noktaları (`apparent_zenith ≥ 90°`) hesap dışı bırakılır: hem gereksiz
sayısal uyarı üretirler hem de anlamsızdırlar. Çıktıda 0 olarak yer alırlar.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta

import numpy as np
import pandas as pd
from pvlib import atmosphere, inverter, irradiance, pvsystem, solarposition, spectrum, tracking
from pvlib import iam as pv_iam
from pvlib.bifacial import infinite_sheds
from pvlib.temperature import prilliman, sapm_cell

from luminmind.twin.components import LossChain
from luminmind.twin.plant_model import ArrayConfig, MountType
from luminmind.twin.weather import IrradianceStamp

logger = logging.getLogger(__name__)

# Güneş bu yüksekliğin altındayken model çalıştırılmaz (kırılma/ufuk belirsiz).
NIGHT_ZENITH_DEG = 90.0

# Işınım kapanış testi: |GHI − (DHI + DNI·cos z)| bu iki eşiği de aşarsa
# DNI/DHI güvenilmez sayılır ve GHI'dan yeniden türetilir.
_CLOSURE_REL_TOL = 0.15
_CLOSURE_ABS_TOL_WM2 = 20.0

# Fiziksel üst sınır: berrak gökyüzünde bile GHI, dünya dışı ışınımın yatay
# bileşeninin bu katını aşamaz. Aşan değerler ölçüm/tahmin hatasıdır.
_GHI_SANITY_FACTOR = 1.10

_PW_RANGE = (0.1, 8.0)  # spectral_factor_firstsolar geçerlilik aralığı
_AIRMASS_RANGE = (0.58, 10.0)
_SPECTRAL_MODULE_TYPES = frozenset({"cdte", "monosi", "xsi", "multisi", "polysi", "cigs", "asi"})


@dataclass(frozen=True)
class Site:
    """Tesisin coğrafi konumu."""

    latitude: float
    longitude: float
    altitude_m: float = 0.0


@dataclass(frozen=True)
class ChainResult:
    """Zincirin tüm ara ürünleri — teşhis ve kalibrasyon bu serileri kullanır."""

    ac_w: pd.Series
    dc_w: pd.Series
    dc_potential_w: pd.Series  # kırpma öncesi DC (kırpma kaybını ölçmek için)
    poa_global: pd.Series
    effective_irradiance: pd.Series
    cell_temp_c: pd.Series
    soiling_ratio: pd.Series
    shaded_fraction: pd.Series
    daytime: pd.Series
    # Işınım verisi mevcut mu — False olan noktalar için beklenen üretim
    # *bilinmiyor*dur; sıfır olarak raporlanmamalı, hiç raporlanmamalıdır.
    irradiance_valid: pd.Series

    @property
    def clipping_loss_w(self) -> pd.Series:
        """İnvertör kırpması nedeniyle kaybedilen DC güç."""
        return (self.dc_potential_w - self.dc_w).clip(lower=0.0)


def effective_times(
    index: pd.DatetimeIndex, stamp: IrradianceStamp, interval: timedelta
) -> pd.DatetimeIndex:
    """Işınım aralığının temsil ettiği anı (orta nokta) döndürür.

    Open-Meteo damgası aralık sonudur: `t` değeri `[t−Δ, t)` ortalamasıdır, yani
    temsil ettiği an `t − Δ/2`'dir. Geometriyi damgada hesaplamak sabah/akşam
    kenarlarında yarım aralıklık sistematik faz kayması yaratır.
    """
    half = interval / 2
    if stamp is IrradianceStamp.INTERVAL_END:
        return index - half
    if stamp is IrradianceStamp.INTERVAL_START:
        return index + half
    return index


def solar_position(
    index: pd.DatetimeIndex,
    site: Site,
    stamp: IrradianceStamp = IrradianceStamp.INTERVAL_END,
    interval: timedelta = timedelta(minutes=15),
    temp_air: pd.Series | None = None,
    pressure_hpa: pd.Series | None = None,
) -> tuple[pd.DataFrame, pd.DatetimeIndex]:
    """Güneş konumunu aralık orta noktasında hesaplar, orijinal damgaya indeksler.

    İkinci dönen değer geometrinin hesaplandığı gerçek zaman ekseni; dünya dışı
    ışınım ve Erbs ayrıştırması gibi gün-içi konumdan türeyen büyüklükler de
    aynı eksende hesaplanmalıdır.
    """
    solar_index = effective_times(index, stamp, interval)
    kwargs: dict[str, object] = {}
    if temp_air is not None and temp_air.notna().any():
        kwargs["temperature"] = float(temp_air.mean(skipna=True))
    if pressure_hpa is not None and pressure_hpa.notna().any():
        kwargs["pressure"] = float(pressure_hpa.mean(skipna=True)) * 100.0  # hPa → Pa
    with np.errstate(all="ignore"):
        solpos = solarposition.get_solarposition(
            solar_index, site.latitude, site.longitude, altitude=site.altitude_m, **kwargs
        )
    solpos.index = index
    return solpos, solar_index


def daytime_mask(solpos: pd.DataFrame) -> pd.Series:
    mask = solpos["apparent_zenith"] < NIGHT_ZENITH_DEG
    return mask.fillna(False).astype(bool)


def reconcile_irradiance(
    weather: pd.DataFrame, solpos: pd.DataFrame, solar_index: pd.DatetimeIndex
) -> pd.DataFrame:
    """Işınım üçlüsünü tutarlı hale getirir; eksik/çelişkili DNI-DHI'yı yeniden türetir.

    Dönen çerçevede `ghi`, `dni`, `dhi` ve `irradiance_valid` (bool) bulunur.
    `irradiance_valid=False` olan noktalar (GHI eksik) beklenen üretimden
    dışlanır — sıfır üretim olarak raporlanmaz, hiç raporlanmaz.
    """
    index = weather.index
    zenith = solpos["apparent_zenith"]
    cos_zenith = np.cos(np.radians(zenith)).clip(lower=0.0)

    ghi_raw = pd.to_numeric(weather.get("ghi"), errors="coerce")
    valid = ghi_raw.notna()
    ghi = ghi_raw.fillna(0.0).clip(lower=0.0)

    # Fiziksel tavan: dünya dışı ışınımın yatay bileşeni × pay
    with np.errstate(all="ignore"):
        extra = irradiance.get_extra_radiation(solar_index)
    extra = pd.Series(np.asarray(extra, dtype=float), index=index)
    ghi_ceiling = (extra * cos_zenith * _GHI_SANITY_FACTOR).clip(lower=0.0)
    over = ghi > ghi_ceiling
    if bool(over.any()):
        logger.debug("clipped %d GHI samples above physical ceiling", int(over.sum()))
    ghi = ghi.where(~over, ghi_ceiling)

    dni = pd.to_numeric(weather.get("dni"), errors="coerce")
    dhi = pd.to_numeric(weather.get("dhi"), errors="coerce")

    closure = dhi + dni * cos_zenith
    residual = (closure - ghi).abs()
    tolerance = np.maximum(ghi * _CLOSURE_REL_TOL, _CLOSURE_ABS_TOL_WM2)
    inconsistent = dni.isna() | dhi.isna() | (residual > tolerance)
    inconsistent = inconsistent.fillna(True).astype(bool) & valid

    if bool(inconsistent.any()):
        # Gün sırası ndarray olarak verilir: DatetimeIndex geçilirse pvlib içeride
        # o eksende bir Series üretir ve `ghi` ile hizalanma indeks birleşimine
        # dönüşür (geometri ekseni damga ekseninden yarım aralık kaymış durumda).
        with np.errstate(all="ignore"):
            derived = irradiance.erbs(ghi, zenith, np.asarray(solar_index.dayofyear))
        dni = dni.where(~inconsistent, derived["dni"])
        dhi = dhi.where(~inconsistent, derived["dhi"])
        logger.debug("re-derived DNI/DHI for %d samples via Erbs", int(inconsistent.sum()))

    frame = pd.DataFrame(
        {
            "ghi": ghi,
            "dni": dni.fillna(0.0).clip(lower=0.0),
            "dhi": dhi.fillna(0.0).clip(lower=0.0),
            "dni_extra": extra,
            "irradiance_valid": valid,
        },
        index=index,
    )
    return frame


def surface_orientation(
    array: ArrayConfig, solpos: pd.DataFrame
) -> tuple[pd.Series, pd.Series]:
    """Modül yüzeyinin eğim/azimut serisi (izleyicide zamana bağlı)."""
    index = solpos.index
    if array.mount is not MountType.SINGLE_AXIS_TRACKER:
        tilt = pd.Series(float(array.tilt_deg), index=index, dtype=float)
        azimuth = pd.Series(float(array.azimuth_deg), index=index, dtype=float)
        return tilt, azimuth

    with np.errstate(all="ignore"):
        tracker = tracking.singleaxis(
            solpos["apparent_zenith"],
            solpos["azimuth"],
            axis_tilt=array.axis_tilt_deg,
            axis_azimuth=array.axis_azimuth_deg,
            max_angle=array.max_tracker_angle_deg,
            backtrack=array.backtrack,
            gcr=array.gcr,
        )
    # Güneş ufkun altındayken izleyici açısı tanımsız → yatay park pozisyonu
    tilt = tracker["surface_tilt"].fillna(0.0).astype(float)
    azimuth = tracker["surface_azimuth"].fillna(float(array.axis_azimuth_deg)).astype(float)
    return tilt, azimuth


def poa_components(
    array: ArrayConfig,
    solpos: pd.DataFrame,
    irrad: pd.DataFrame,
    surface_tilt: pd.Series,
    surface_azimuth: pd.Series,
    airmass: pd.Series,
) -> pd.DataFrame:
    """Düzlem üstü ışınımı bileşenlerine ayrılmış olarak döndürür.

    Arazi santralinde `infinite_sheds` kullanılır: sonsuz paralel sıra
    geometrisiyle sıra-arası gölgelenmeyi ve arka yüz kazancını hesaplar.
    Çatıda tek düzlem varsayımı geçerlidir → Perez transpozisyonu.
    """
    index = solpos.index
    zenith = solpos["apparent_zenith"]
    azimuth = solpos["azimuth"]

    with np.errstate(all="ignore"):
        if array.models_row_shading:
            result = infinite_sheds.get_irradiance(
                surface_tilt=surface_tilt,
                surface_azimuth=surface_azimuth,
                solar_zenith=zenith,
                solar_azimuth=azimuth,
                gcr=array.gcr,
                height=array.row_height_m,
                pitch=array.row_pitch_m,
                ghi=irrad["ghi"],
                dhi=irrad["dhi"],
                dni=irrad["dni"],
                albedo=array.albedo,
                model="haydavies",
                dni_extra=irrad["dni_extra"],
                bifaciality=array.bifaciality,
                npoints=100,
            )
            frame = pd.DataFrame(
                {
                    "poa_direct": result["poa_front_direct"],
                    "poa_sky_diffuse": result["poa_front_sky_diffuse"],
                    "poa_ground_diffuse": result["poa_front_ground_diffuse"],
                    "poa_back": result["poa_back"],
                    "shaded_fraction": result["shaded_fraction_front"],
                },
                index=index,
            )
        else:
            total = irradiance.get_total_irradiance(
                surface_tilt=surface_tilt,
                surface_azimuth=surface_azimuth,
                solar_zenith=zenith,
                solar_azimuth=azimuth,
                dni=irrad["dni"],
                ghi=irrad["ghi"],
                dhi=irrad["dhi"],
                dni_extra=irrad["dni_extra"],
                airmass=airmass,
                albedo=array.albedo,
                model="perez",
            )
            frame = pd.DataFrame(
                {
                    "poa_direct": total["poa_direct"],
                    "poa_sky_diffuse": total["poa_sky_diffuse"],
                    "poa_ground_diffuse": total["poa_ground_diffuse"],
                    "poa_back": 0.0,
                    "shaded_fraction": 0.0,
                },
                index=index,
            )

    frame = frame.fillna(0.0).clip(lower=0.0)
    frame["poa_global"] = (
        frame["poa_direct"]
        + frame["poa_sky_diffuse"]
        + frame["poa_ground_diffuse"]
        + frame["poa_back"] * array.bifaciality
    )
    return frame


def incidence_angle(solpos: pd.DataFrame, tilt: pd.Series, azimuth: pd.Series) -> pd.Series:
    with np.errstate(all="ignore"):
        aoi = irradiance.aoi(tilt, azimuth, solpos["apparent_zenith"], solpos["azimuth"])
    return pd.Series(np.asarray(aoi, dtype=float), index=solpos.index).fillna(90.0)


def optical_factors(tilt: pd.Series, aoi: pd.Series) -> pd.DataFrame:
    """Bileşen bazlı IAM (yansıma) katsayıları.

    Direkt ışınım için fiziksel IAM; difüz bileşenler için Marion'un açısal
    integrasyonu. Tek bir IAM'i tüm ışınıma uygulamak difüz payı sistematik
    olarak fazla cezalandırır (difüz her açıdan gelir).
    """
    with np.errstate(all="ignore"):
        beam = pd.Series(np.asarray(pv_iam.physical(aoi), dtype=float), index=aoi.index)
        # Sabit eğimde tek değer yeter; izleyicide eğim zamanla değişir
        unique_tilts = np.unique(np.round(tilt.to_numpy(dtype=float), 1))
        if unique_tilts.size == 1:
            diffuse = pv_iam.marion_diffuse("physical", float(unique_tilts[0]))
            sky = pd.Series(float(np.asarray(diffuse["sky"]).ravel()[0]), index=tilt.index)
            ground = pd.Series(float(np.asarray(diffuse["ground"]).ravel()[0]), index=tilt.index)
        else:
            diffuse = pv_iam.marion_diffuse("physical", tilt.to_numpy(dtype=float))
            sky = pd.Series(np.asarray(diffuse["sky"], dtype=float), index=tilt.index)
            ground = pd.Series(np.asarray(diffuse["ground"], dtype=float), index=tilt.index)

    return pd.DataFrame(
        {
            "iam_beam": beam.fillna(0.0).clip(lower=0.0, upper=1.0),
            "iam_sky": sky.fillna(1.0).clip(lower=0.0, upper=1.0),
            "iam_ground": ground.fillna(1.0).clip(lower=0.0, upper=1.0),
        },
        index=aoi.index,
    )


def spectral_factor(
    array: ArrayConfig,
    temp_air: pd.Series,
    relative_humidity: pd.Series,
    airmass_absolute: pd.Series,
) -> pd.Series:
    """Spektral uyumsuzluk düzeltmesi (First Solar modeli).

    Bağıl nem yoksa düzeltme uygulanmaz (1,0) — uydurma bir nem varsaymak,
    düzeltmeyi hiç uygulamamaktan daha büyük hata üretir.
    """
    index = airmass_absolute.index
    neutral = pd.Series(1.0, index=index, dtype=float)
    if array.module_type not in _SPECTRAL_MODULE_TYPES:
        logger.debug("unknown module_type %s; spectral correction skipped", array.module_type)
        return neutral
    if relative_humidity.isna().all() or temp_air.isna().all():
        return neutral

    with np.errstate(all="ignore"):
        pw = atmosphere.gueymard94_pw(temp_air, relative_humidity)
    pw = pd.Series(np.asarray(pw, dtype=float), index=index)
    if pw.isna().all():
        return neutral
    pw = pw.fillna(pw.median()).clip(*_PW_RANGE)
    am = airmass_absolute.fillna(_AIRMASS_RANGE[1]).clip(*_AIRMASS_RANGE)

    with np.errstate(all="ignore"):
        factor = spectrum.spectral_factor_firstsolar(pw, am, module_type=array.module_type)
    series = pd.Series(np.asarray(factor, dtype=float), index=index)
    return series.fillna(1.0).clip(lower=0.8, upper=1.2)


def wind_at_module_height(wind_10m: pd.Series, height_m: float, exponent: float) -> pd.Series:
    """10 m rüzgarını modül yüksekliğine indirger (Hellmann üstel yasası).

    10 m'deki hızı doğrudan SAPM'e vermek modülü sistematik olarak fazla soğutur;
    hücre sıcaklığı düşük çıkar ve beklenen üretim yazın birkaç puan fazla olur.
    """
    height = max(height_m, 0.5)
    return (wind_10m.fillna(1.0).clip(lower=0.0) * (height / 10.0) ** exponent).astype(float)


def cell_temperature(
    array: ArrayConfig,
    poa_global: pd.Series,
    temp_air: pd.Series,
    wind_10m: pd.Series,
    interval: timedelta,
) -> pd.Series:
    """Hücre sıcaklığı: SAPM sabit rejim + Prilliman geçici rejim düzeltmesi."""
    params = array.temperature_model_params
    ambient = temp_air.astype(float).ffill().bfill()
    if ambient.isna().all():
        ambient = pd.Series(20.0, index=poa_global.index, dtype=float)
    wind = wind_at_module_height(wind_10m, array.row_height_m, array.wind_shear_exponent)

    with np.errstate(all="ignore"):
        steady = sapm_cell(
            poa_global.fillna(0.0),
            ambient,
            wind,
            a=params["a"],
            b=params["b"],
            deltaT=params["deltaT"],
        )
    steady = pd.Series(np.asarray(steady, dtype=float), index=poa_global.index)

    # Prilliman yalnızca saat-altı çözünürlükte anlamlıdır: modülün termal
    # ataleti nedeniyle sıcaklık ani ışınım değişimini gecikmeli izler.
    if interval < timedelta(hours=1) and len(steady) > 2:
        with np.errstate(all="ignore"):
            transient = prilliman(steady, wind)
        steady = pd.Series(np.asarray(transient, dtype=float), index=poa_global.index).fillna(
            steady
        )
    return steady.fillna(ambient)


def dc_power(
    array: ArrayConfig, effective_irradiance: pd.Series, cell_temp: pd.Series, dc_factor: float
) -> pd.Series:
    """PVWatts DC gücü, DC kayıp yığını uygulanmış (W)."""
    with np.errstate(all="ignore"):
        raw = pvsystem.pvwatts_dc(
            effective_irradiance.fillna(0.0).clip(lower=0.0),
            cell_temp,
            pdc0=array.dc_capacity_w,
            gamma_pdc=array.gamma_pdc,
        )
    series = pd.Series(np.asarray(raw, dtype=float), index=effective_irradiance.index)
    return (series.fillna(0.0).clip(lower=0.0) * dc_factor).astype(float)


def ac_power(array: ArrayConfig, dc_w: pd.Series) -> tuple[pd.Series, pd.Series]:
    """İnvertör çıkışı (W) ve kırpılmış DC girişi.

    DC girişi önce invertörün girdi limitine kırpılır — gerçek santral
    davranışı budur. pvlib'in PVWatts invertör bağıntısı ζ = pdc/pdc0 > 1
    bölgesinde geçerli değildir (negatif verim üretip 0'a düşer), bu yüzden
    kırpma çağrıdan önce yapılmak zorundadır.
    """
    limit = array.inverter_pdc0_w
    clipped = dc_w.clip(upper=limit)
    with np.errstate(all="ignore"):
        ac = inverter.pvwatts(clipped, pdc0=limit, eta_inv_nom=array.inverter_eta_nom)
    series = pd.Series(np.asarray(ac, dtype=float), index=dc_w.index)
    return series.fillna(0.0).clip(lower=0.0), clipped


def run_chain(
    array: ArrayConfig,
    site: Site,
    weather: pd.DataFrame,
    losses: LossChain,
    soiling: pd.Series | None = None,
    stamp: IrradianceStamp = IrradianceStamp.INTERVAL_END,
    interval: timedelta = timedelta(minutes=15),
) -> ChainResult:
    """Tek dizi için tüm zinciri çalıştırır.

    `weather` sütunları: ghi, dni, dhi, temp_air, wind_speed ve isteğe bağlı
    relative_humidity, pressure. `soiling` verilirse zincirdeki statik kirlilik
    terimi devre dışı kalır.
    """
    index = pd.DatetimeIndex(weather.index)
    temp_air = pd.to_numeric(weather.get("temp_air"), errors="coerce")
    wind = pd.to_numeric(weather.get("wind_speed"), errors="coerce")
    humidity = pd.to_numeric(
        weather.get("relative_humidity", pd.Series(np.nan, index=index)), errors="coerce"
    )
    pressure = pd.to_numeric(
        weather.get("pressure", pd.Series(np.nan, index=index)), errors="coerce"
    )

    solpos, solar_index = solar_position(index, site, stamp, interval, temp_air, pressure)
    day = daytime_mask(solpos)
    irrad = reconcile_irradiance(weather, solpos, solar_index)
    valid = irrad["irradiance_valid"].astype(bool)

    with np.errstate(all="ignore"):
        relative_airmass = atmosphere.get_relative_airmass(solpos["apparent_zenith"])
        pressure_pa = pressure * 100.0
        pressure_pa = pressure_pa.fillna(atmosphere.alt2pres(site.altitude_m))
        absolute_airmass = atmosphere.get_absolute_airmass(relative_airmass, pressure_pa)
    absolute_airmass = pd.Series(np.asarray(absolute_airmass, dtype=float), index=index)

    tilt, azimuth = surface_orientation(array, solpos)
    poa = poa_components(array, solpos, irrad, tilt, azimuth, absolute_airmass)
    aoi = incidence_angle(solpos, tilt, azimuth)
    optics = optical_factors(tilt, aoi)

    if soiling is None:
        soiling_series = pd.Series(1.0, index=index, dtype=float)
        dc_factor = losses.dc_factor
    else:
        soiling_series = soiling.reindex(index).ffill().bfill().fillna(1.0).clip(0.0, 1.0)
        dc_factor = losses.dc_factor_without_soiling

    spectral = spectral_factor(array, temp_air, humidity, absolute_airmass)

    effective = (
        poa["poa_direct"] * optics["iam_beam"]
        + poa["poa_sky_diffuse"] * optics["iam_sky"]
        + poa["poa_ground_diffuse"] * optics["iam_ground"]
        + poa["poa_back"] * array.bifaciality * optics["iam_sky"]
    ) * spectral * soiling_series
    effective = effective.clip(lower=0.0)

    cell_temp = cell_temperature(array, poa["poa_global"], temp_air, wind, interval)

    usable = day & valid
    effective = effective.where(usable, 0.0)

    dc_potential = dc_power(array, effective, cell_temp, dc_factor)
    ac_w, dc_clipped = ac_power(array, dc_potential)
    ac_w = losses.apply_ac(ac_w).where(usable, 0.0)

    return ChainResult(
        ac_w=ac_w,
        dc_w=dc_clipped.where(usable, 0.0),
        dc_potential_w=dc_potential.where(usable, 0.0),
        poa_global=poa["poa_global"].where(usable, 0.0),
        effective_irradiance=effective,
        cell_temp_c=cell_temp,
        soiling_ratio=soiling_series,
        shaded_fraction=poa["shaded_fraction"].where(usable, 0.0),
        daytime=day,
        irradiance_valid=valid,
    )
