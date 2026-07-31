"""PVGIS (Avrupa Komisyonu, JRC) TMY ve ufuk profili istemcisi.

**Neden PVGIS.** Türkiye'nin tamamını kapsıyor (İstanbul'dan Ağrı'ya, 40–1633 m
rakımda doğrulandı), ücretsiz ve API anahtarı istemiyor, saatlik uydu ışınımı
(PVGIS-SARAH3) ile ERA5 meteorolojisini birlikte veriyor ve arazi ufkunu hesaba
katıyor. Google Solar API'nin Türkiye kapsamı doğrulanmadığı ve arazi santralini
hiç görmediği için fizibilite hesabının taşıyıcı veri kaynağı burasıdır.

**Tipik meteorolojik yıl.** TMY, çok yıllı kayıttan (2005–2023) her ay için
"tipik" olan gerçek ayın seçilip birleştirilmesiyle üretilen 8760 saatlik temsili
yıldır. Finansal projeksiyonun dayanağı budur.

Kolayca gözden kaçan üç nokta — üçü de sistematik hata kaynağı:

**1. Damga sözleşmesi ampirik olarak belirlendi, varsayılmadı.** PVGIS saatlik
damgası ne aralık başı ne aralık sonudur: değerin temsil ettiği an
`damga + irradiance_time_offset`'tir ve öteleme yanıtta konum bazında gelir
(≈0,17 sa, uydunun tarama zamanından). Açık gökyüzü saatlerinde
`GHI ≈ DNI·cos(z) + DHI` kapanışı dört konumda (İstanbul, Ağrı, Antalya, Konya)
aday ötelemelerle sınandı:

    öteleme       kapanış MAE
    0,00 sa        11–12 W/m²
    API ötelemesi   0,4–0,65 W/m²   ← doğru
    0,50 sa        21–24 W/m²

Yanlış öteleme iki kez zarar verir: güneş geometrisi kayar *ve*
`pipeline.reconcile_irradiance` kapanış toleransını aşan noktaların DNI/DHI'sını
Erbs korelasyonuyla değiştirir — yüksek kaliteli uydu DNI'si sessizce kaba bir
tahminle ezilir. Bu yüzden indeks istemcide öteleniyor ve zincire
`IrradianceStamp.INSTANT` ile giriliyor; `pipeline.py` değişmiyor.

**2. Ufuk gölgelemesi TMY'ye zaten dâhil** (`use_horizon: true`). `fetch_horizon`
ile gelen profil yalnızca gösterim/teşhis içindir; zincire ikinci kez sokulursa
arazi gölgesi çift sayılır.

**3. Basınç birimi.** PVGIS `SP` alanını Pa verir, `run_chain` ise hPa bekler
(içeride 100 ile çarpıyor). Bölme atlanırsa mutlak hava kütlesi 100 kat sapar.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx
import numpy as np
import pandas as pd

from luminmind.adapters.base import AdapterError
from luminmind.adapters.retry import request_with_retry
from luminmind.twin.pipeline import Site

logger = logging.getLogger(__name__)

PVGIS_BASE_URL = "https://re.jrc.ec.europa.eu"
PVGIS_API_VERSION = "v5_3"

# TMY satırları saatlik ortalamadır.
TMY_INTERVAL = timedelta(hours=1)

# TMY'nin ayları farklı yıllardan seçilir (Ocak 2018 + Şubat 2011 …). Kaynak yıl
# indekste bırakılırsa indeks her ay sınırında geriye atlar; monotonluk kaybolur
# ve `resample`/`ffill` sessizce yanlış sonuç verir. Tüm satırlar tek bir
# artık-olmayan referans yıla taşınır — pvlib `iotools.get_pvgis_tmy` da
# `coerce_year` ile aynısını yapar. Yıl seçimi yalnızca zaman denkleminde
# ihmal edilebilir bir fark yaratır.
TMY_REFERENCE_YEAR = 2019

_HOURS_PER_TMY = 8760

# PVGIS sütun adı → `twin.pipeline.run_chain` sözleşmesi.
# Kullanılmayan alanlar: IR(h) (yer seviyesi termal ışınım), WD10m (rüzgâr yönü).
_COLUMN_MAP: dict[str, str] = {
    "G(h)": "ghi",
    "Gb(n)": "dni",
    "Gd(h)": "dhi",
    "T2m": "temp_air",
    "WS10m": "wind_speed",
    "RH": "relative_humidity",
    "SP": "pressure",
}

_TIME_FIELD = "time(UTC)"

# Ufuk profilinde 3°'nin altındaki yükseklik pratikte düz araziye eşdeğerdir;
# kullanıcıya "ufuk engeli var" demek yanıltıcı olur.
_FLAT_HORIZON_DEG = 3.0


@dataclass(frozen=True)
class TmyDataset:
    """Tek konum için TMY hava verisi ve kaynak künyesi.

    `weather` doğrudan `run_chain`'e verilebilir: sütunlar ghi, dni, dhi,
    temp_air, wind_speed, relative_humidity, pressure (hPa) ve indeks
    ışınımın temsil ettiği andır (UTC).
    """

    weather: pd.DataFrame
    latitude: float
    longitude: float
    altitude_m: float
    radiation_db: str
    meteo_db: str
    year_min: int
    year_max: int
    horizon_included: bool
    irradiance_time_offset_h: float
    months_selected: tuple[tuple[int, int], ...]

    @property
    def site(self) -> Site:
        """Zincirin beklediği konum nesnesi; rakım PVGIS'in DEM değerinden gelir."""
        return Site(
            latitude=self.latitude, longitude=self.longitude, altitude_m=self.altitude_m
        )

    @property
    def annual_ghi_kwh_m2(self) -> float:
        """Yatay düzleme yıllık toplam ışınım. Saatlik ortalama × 1 sa = Wh/m²."""
        return float(self.weather["ghi"].sum()) / 1000.0

    @property
    def mean_temp_c(self) -> float:
        return float(self.weather["temp_air"].mean())

    @property
    def provenance(self) -> str:
        """Rapora basılacak tek satırlık veri künyesi."""
        return (
            f"PVGIS {PVGIS_API_VERSION} · {self.radiation_db} + {self.meteo_db} · "
            f"{self.year_min}–{self.year_max} TMY · rakım {self.altitude_m:.0f} m · "
            f"arazi ufku {'dahil' if self.horizon_included else 'hariç'}"
        )


@dataclass(frozen=True)
class HorizonProfile:
    """Arazi ufuk profili — konumdan çevreye bakıldığında engelin yükseklik açısı.

    Azimut `pvlib` sözleşmesine çevrilmiştir: 0 = Kuzey, 90 = Doğu, 180 = Güney.
    PVGIS kendi `A` alanını güney referanslı verir (A = 0'da güneşin gündönümü
    öğle yüksekliği tam olarak 90 − φ ∓ 23,44 çıkıyor, bu şekilde doğrulandı);
    işaret PVGIS dokümantasyonuna göre batıya pozitiftir.

    **Bu profil zincire girmez.** TMY ışınımı arazi ufkunu zaten içeriyor
    (`use_horizon: true`); ikinci kez uygulamak gölgeyi çift sayar. Burada
    tutulma sebebi kullanıcıya "vadide misin, sırtta mısın" sorusunu
    gösterebilmek.
    """

    azimuth_deg: tuple[float, ...]
    elevation_deg: tuple[float, ...]

    @property
    def max_elevation_deg(self) -> float:
        return max(self.elevation_deg) if self.elevation_deg else 0.0

    @property
    def is_flat(self) -> bool:
        """Ufuk pratikte düz mü — kullanıcıya uyarı gösterilip gösterilmeyeceği."""
        return self.max_elevation_deg < _FLAT_HORIZON_DEG

    def elevation_at(self, azimuth_deg: float) -> float:
        """Verilen azimutta ufuk yüksekliği (dairesel doğrusal aradeğerleme)."""
        if not self.azimuth_deg:
            return 0.0
        return float(
            np.interp(
                azimuth_deg % 360.0,
                np.asarray(self.azimuth_deg, dtype=float),
                np.asarray(self.elevation_deg, dtype=float),
                period=360.0,
            )
        )


def _as_float(value: Any) -> float:
    """None → NaN. Sıfıra düşürmek eksik veriyi geceden ayırt edilemez kılardı."""
    if value is None:
        return float("nan")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _require(payload: dict[str, Any], *path: str) -> Any:
    """İç içe alanı okur; yoksa `AdapterError`. Sessiz `None` yayılmasını engeller."""
    node: Any = payload
    for key in path:
        if not isinstance(node, dict) or key not in node:
            raise AdapterError(f"PVGIS yanıtında beklenen alan yok: {'.'.join(path)}")
        node = node[key]
    return node


def _parse_tmy_index(rows: Sequence[dict[str, Any]], offset_h: float) -> pd.DatetimeIndex:
    """`YYYYMMDD:HHMM` damgalarını referans yıla taşıyıp ötelemeyi uygular.

    Kaynak yıl atılır (bkz. `TMY_REFERENCE_YEAR`); dönen indeks ışınımın temsil
    ettiği andır, damganın kendisi değil.
    """
    stamps: list[pd.Timestamp] = []
    for row in rows:
        raw = row.get(_TIME_FIELD)
        if not isinstance(raw, str) or len(raw) < 13 or raw[8] != ":":
            raise AdapterError(f"PVGIS TMY damgası çözümlenemedi: {raw!r}")
        try:
            month, day = int(raw[4:6]), int(raw[6:8])
            hour, minute = int(raw[9:11]), int(raw[11:13])
            stamps.append(
                pd.Timestamp(
                    year=TMY_REFERENCE_YEAR,
                    month=month,
                    day=day,
                    hour=hour,
                    minute=minute,
                    tz="UTC",
                )
            )
        except ValueError as exc:  # 29 Şubat referans yılda yok → artık yıl sızmış
            raise AdapterError(f"PVGIS TMY damgası geçersiz: {raw!r} ({exc})") from exc

    index = pd.DatetimeIndex(stamps) + pd.Timedelta(hours=offset_h)
    if not index.is_monotonic_increasing or index.has_duplicates:
        raise AdapterError(
            "PVGIS TMY satırları referans yıla taşındıktan sonra monotonik değil; "
            "ay sıralaması beklenenden farklı"
        )
    return index


def parse_tmy(payload: dict[str, Any]) -> TmyDataset:
    """TMY JSON yanıtını `TmyDataset`'e çevirir (ağ erişimi gerektirmez)."""
    location = _require(payload, "inputs", "location")
    meteo = _require(payload, "inputs", "meteo_data")
    rows = _require(payload, "outputs", "tmy_hourly")
    if not isinstance(rows, list) or not rows:
        raise AdapterError("PVGIS TMY yanıtı boş")
    if len(rows) != _HOURS_PER_TMY:
        raise AdapterError(
            f"PVGIS TMY {_HOURS_PER_TMY} satır olmalı, {len(rows)} geldi — "
            "yıllık toplamlar bu varsayıma dayanıyor"
        )

    offset_h = _as_float(location.get("irradiance_time_offset"))
    if np.isnan(offset_h):
        # Öteleme yoksa geometri ~10 dk kayar; sessizce sürdürmek yerine uyaralım.
        logger.warning("PVGIS yanıtında irradiance_time_offset yok; öteleme uygulanmıyor")
        offset_h = 0.0

    index = _parse_tmy_index(rows, offset_h)
    columns: dict[str, list[float]] = {name: [] for name in _COLUMN_MAP.values()}
    for row in rows:
        for source, target in _COLUMN_MAP.items():
            columns[target].append(_as_float(row.get(source)))

    weather = pd.DataFrame(columns, index=index, dtype=float)
    # PVGIS Pa verir, zincir hPa bekler (içeride 100 ile çarpıyor).
    weather["pressure"] = weather["pressure"] / 100.0

    months = tuple(
        (int(entry["month"]), int(entry["year"]))
        for entry in payload.get("outputs", {}).get("months_selected", [])
        if isinstance(entry, dict) and "month" in entry and "year" in entry
    )

    return TmyDataset(
        weather=weather,
        latitude=_as_float(location.get("latitude")),
        longitude=_as_float(location.get("longitude")),
        altitude_m=_as_float(location.get("elevation")),
        radiation_db=str(meteo.get("radiation_db", "?")),
        meteo_db=str(meteo.get("meteo_db", "?")),
        year_min=int(meteo.get("year_min", 0)),
        year_max=int(meteo.get("year_max", 0)),
        horizon_included=bool(meteo.get("use_horizon", False)),
        irradiance_time_offset_h=offset_h,
        months_selected=months,
    )


def parse_horizon(payload: dict[str, Any]) -> HorizonProfile:
    """Ufuk profili yanıtını azimut/yükseklik çiftlerine çevirir."""
    rows = _require(payload, "outputs", "horizon_profile")
    if not isinstance(rows, list) or not rows:
        raise AdapterError("PVGIS ufuk profili boş")

    pairs: list[tuple[float, float]] = []
    for row in rows:
        south_ref = _as_float(row.get("A"))
        elevation = _as_float(row.get("H_hor"))
        if np.isnan(south_ref) or np.isnan(elevation):
            continue
        # PVGIS: A = 0 güney, batıya pozitif → pvlib: 0 kuzey, doğuya pozitif
        pairs.append(((180.0 + south_ref) % 360.0, elevation))

    if not pairs:
        raise AdapterError("PVGIS ufuk profilinde geçerli nokta yok")
    pairs.sort()
    return HorizonProfile(
        azimuth_deg=tuple(p[0] for p in pairs),
        elevation_deg=tuple(p[1] for p in pairs),
    )


class PvgisClient:
    """PVGIS `v5_3` istemcisi (TMY + ufuk profili).

    Anahtar gerektirmez. Yanıt ~1,3 MB olduğundan varsayılan zaman aşımı
    Open-Meteo istemcisinden uzun tutulmuştur.
    """

    def __init__(self, base_url: str = PVGIS_BASE_URL, timeout_s: float = 60.0) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout_s)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "PvgisClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        response = await request_with_retry(
            self._client, "GET", f"/api/{PVGIS_API_VERSION}/{path}", params=params
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise AdapterError(f"PVGIS {path} yanıtı JSON değil: {response.text[:200]}") from exc
        if not isinstance(payload, dict):
            raise AdapterError(f"PVGIS {path} yanıtı sözlük değil")
        return payload

    async def fetch_tmy(self, latitude: float, longitude: float) -> TmyDataset:
        """Konum için tipik meteorolojik yılı çeker.

        PVGIS kapsama dışı koordinatlarda (deniz, Avrupa/Afrika/Asya penceresi
        dışı) 4xx döner; `request_with_retry` bunu `AdapterError`'a çevirir.
        """
        payload = await self._get_json(
            "tmy", {"lat": latitude, "lon": longitude, "outputformat": "json"}
        )
        dataset = parse_tmy(payload)
        logger.info(
            "PVGIS TMY alındı: %.4f,%.4f · %.0f kWh/m²/yıl · %s",
            latitude,
            longitude,
            dataset.annual_ghi_kwh_m2,
            dataset.provenance,
        )
        return dataset

    async def fetch_horizon(self, latitude: float, longitude: float) -> HorizonProfile:
        """Arazi ufuk profilini çeker (gösterim amaçlı; zincire girmez)."""
        payload = await self._get_json(
            "printhorizon", {"lat": latitude, "lon": longitude, "outputformat": "json"}
        )
        return parse_horizon(payload)
