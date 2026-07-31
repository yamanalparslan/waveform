"""Uygulama ayarları: tüm konfigürasyon tek tip-güvenli Settings sınıfında toplanır.

Ayarların okunduğu dosya `LM_ENV_FILE` ile değiştirilebilir; boş verilirse hiç
dosya okunmaz. Bu, testlerin geliştiricinin `.env` dosyasından etkilenmemesi
için gerekli: aksi halde makinede `.env` olup olmamasına göre testler farklı
sonuç verir (ve gerçek kimlik bilgileri sessizce teste sızar).
"""

import os
from functools import lru_cache
from urllib.parse import urlparse

from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = os.environ.get("LM_ENV_FILE", ".env") or None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore"
    )

    # Genel
    lm_env: str = "dev"
    lm_log_level: str = "INFO"

    # Şebekeye satış tarifesi (₺/kWh) — arayüzdeki parasal karşılıkların varsayılanı.
    # Tesis kaydında `feed_in_tariff_try_kwh` doluysa o değer önceliklidir.
    lm_default_tariff_try_kwh: float = 2.9

    # PostgreSQL
    postgres_dsn: str = "postgresql+asyncpg://luminmind:changeme@localhost:5432/luminmind"

    # InfluxDB — url boşsa ingestion yalnızca loglar (Influx'sız dev/test modu)
    influx_url: str = ""
    influx_org: str = "luminmind"
    influx_token: str = ""

    # Üretici token şifreleme (Fernet anahtarı; üretimde zorunlu)
    credentials_enc_key: str = ""

    # Auth (JWT)
    jwt_secret: str = "changeme-dev-secret"
    jwt_access_ttl_min: int = 30
    jwt_refresh_ttl_min: int = 43200  # 30 gün

    # --- Dışa açık çalıştırma ---
    # Panelin genel adresi (ör. https://ges.example.com). Doldurulduğunda
    # uygulama "internete açık" kabul edilir: TLS ve çerez korumaları zorunlu
    # hale gelir, izinli host listesi buradan türetilir.
    lm_public_url: str = ""
    # Oturum çerezine `Secure` bayrağı. Boş bırakılırsa LM_PUBLIC_URL'den
    # türetilir: genel adres varsa açık, yerel HTTP geliştirmede kapalı.
    # Elle true/false yazmak bu türetmeyi ezer.
    lm_cookie_secure: bool | None = None
    # Panel oturum süresi (dk). Telefondan izlemede 30 dk sürekli çıkış demek;
    # varsayılan 1 gün, çerez HttpOnly + Secure olduğu için makul bir denge.
    lm_session_ttl_min: int = 1440
    # Giriş denemesi hız sınırı (parola deneme saldırısına karşı).
    lm_login_max_attempts: int = 8
    lm_login_window_min: int = 15
    # Ek izinli host adları (virgülle). LM_PUBLIC_URL'deki host otomatik eklenir.
    lm_allowed_hosts: str = ""

    # Redis / Celery
    redis_url: str = "redis://localhost:6379/0"
    ingestion_interval_minutes: int = 15
    # Çekim durduğunda (host uykusu, container yeniden başlatma, ağ kesintisi)
    # kaçırılan aralık üreticinin geçmiş uç noktasından geri doldurulur. Üst
    # sınır iki şeye karşı koruma: uzun bir duruştan sonra tek çevrimde
    # onbinlerce nokta çekmek ve üreticinin geçmiş penceresini aşmak.
    ingestion_backfill_max_hours: int = 12

    # --- Dijital ikiz / tahmin ---
    # D+0 dışında kaç gün ileriye tahmin üretilsin (arbitraj ve planlama için).
    lm_twin_horizon_days: int = 2
    # Çok modelli hava tahmini → P10/P50/P90 belirsizlik bandı. Kapatılırsa
    # tek deterministik model kullanılır ve band yazılmaz.
    lm_twin_ensemble: bool = True
    # Virgülle ayrılmış Open-Meteo model listesi; boşsa DEFAULT_ENSEMBLE_MODELS.
    lm_twin_ensemble_models: str = ""
    # Kalibrasyonun baktığı geçmiş pencere ve açık/kapalı durumu.
    lm_twin_calibration_enabled: bool = True
    lm_twin_calibration_window_days: int = 14

    # Üretici API'leri — mock modda gerçek kimlik bilgisi gerekmez
    lm_use_mock_vendors: bool = True
    huawei_base_url: str = ""
    huawei_username: str = ""
    huawei_system_code: str = ""
    sma_base_url: str = ""
    sma_client_id: str = ""
    sma_client_secret: str = ""

    # Tescom UPS inverter API (İzmir fabrikası — gerçek PV verisi)
    tescom_base_url: str = ""
    tescom_api_key: str = ""
    tescom_plant_id: str = "tescom-izmir"
    tescom_plant_name: str = "Tescom İzmir GES"
    tescom_latitude: float = 38.42
    tescom_longitude: float = 27.14
    tescom_dc_capacity_kwp: float = 0.0  # 0 → dijital ikiz kapasiteyi cihaz sayısından türetir
    tescom_timezone: str = "Europe/Istanbul"
    # API'deki `fabrika_id` → saha anahtarı / ad / kurulu güç eşlemesi.
    # Biçim: "fabrika_id:saha_anahtari:Ad:kWp" (virgülle ayrılmış).
    # Saha anahtarı Influx `plant_id` etiketidir; geçmiş veriyle eşleşmesi için
    # mevcut etiket değerleri korunur (tescom-izmir-uretim / -mekanik).
    tescom_factories: str = (
        "uretim:tescom-izmir-uretim:Üretim Fabrikası:400,"
        "mekanik:tescom-izmir-mekanik:Mekanik Fabrika:250"
    )

    # EPİAŞ Şeffaflık — servis hesabı gelene kadar mock fiyatlar kullanılır
    lm_use_mock_prices: bool = True
    epias_base_url: str = ""

    # --- Kurulum öncesi fizibilite ---
    # Çatı poligonu uydu görüntüsünün üzerine çizilir; sokak haritasında çatı
    # sınırı görünmediği için ayrı bir tile katmanı gerekiyor. Varsayılan Esri
    # World Imagery: anahtar istemiyor ve Türkiye'yi yüksek çözünürlükte kapsıyor.
    #
    # LİSANS UYARISI: bu uç noktayı ticari bir SaaS içinde kullanmak atıf
    # dışında abonelik gerektirebilir. Ayar olarak duruyor ki lisans netleşince
    # (ya da Mapbox/Bing anahtarı alınınca) kod değişmeden değiştirilebilsin.
    lm_satellite_tile_url: str = (
        "https://server.arcgisonline.com/ArcGIS/rest/services/"
        "World_Imagery/MapServer/tile/{z}/{y}/{x}"
    )
    lm_satellite_attribution: str = "Görüntü © Esri, Maxar, Earthstar Geographics"
    lm_satellite_max_zoom: int = 19

    @property
    def tescom_factory_sites(self) -> dict[str, tuple[str, str, float | None]]:
        """`fabrika_id` → (saha anahtarı, ad, kWp). Bozuk girdiler sessizce atlanır."""
        sites: dict[str, tuple[str, str, float | None]] = {}
        for entry in self.tescom_factories.split(","):
            parts = [p.strip() for p in entry.split(":")]
            if len(parts) < 3 or not parts[0] or not parts[1]:
                continue
            capacity: float | None = None
            if len(parts) >= 4 and parts[3]:
                try:
                    capacity = float(parts[3])
                except ValueError:
                    capacity = None
            sites[parts[0]] = (parts[1], parts[2], capacity)
        return sites

    @property
    def is_public(self) -> bool:
        """Panel internete açık mı — sertleştirme kararları buna bakar."""
        return bool(self.lm_public_url)

    @property
    def cookie_secure(self) -> bool:
        """Oturum çerezinin `Secure` bayrağı (açıkça verilmemişse türetilir)."""
        return self.is_public if self.lm_cookie_secure is None else self.lm_cookie_secure

    @property
    def allowed_hosts(self) -> list[str]:
        """TrustedHostMiddleware için host listesi; boşsa doğrulama yapılmaz."""
        hosts = [h.strip() for h in self.lm_allowed_hosts.split(",") if h.strip()]
        if self.lm_public_url:
            host = urlparse(self.lm_public_url).hostname
            if host and host not in hosts:
                hosts.append(host)
        return hosts


@lru_cache
def get_settings() -> Settings:
    return Settings()
