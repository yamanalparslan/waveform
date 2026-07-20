"""Uygulama ayarları: tüm konfigürasyon tek tip-güvenli Settings sınıfında toplanır."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Genel
    lm_env: str = "dev"
    lm_log_level: str = "INFO"

    # PostgreSQL
    postgres_dsn: str = "postgresql+asyncpg://luminmind:changeme@localhost:5432/luminmind"

    # InfluxDB — url boşsa ingestion yalnızca loglar (Influx'sız dev/test modu)
    influx_url: str = ""
    influx_org: str = "luminmind"
    influx_token: str = ""

    # Üretici token şifreleme (Fernet anahtarı; üretimde zorunlu)
    credentials_enc_key: str = ""

    # Redis / Celery
    redis_url: str = "redis://localhost:6379/0"
    ingestion_interval_minutes: int = 15

    # Üretici API'leri — mock modda gerçek kimlik bilgisi gerekmez
    lm_use_mock_vendors: bool = True
    huawei_base_url: str = ""
    huawei_username: str = ""
    huawei_system_code: str = ""
    sma_base_url: str = ""
    sma_client_id: str = ""
    sma_client_secret: str = ""

    # EPİAŞ Şeffaflık — servis hesabı gelene kadar mock fiyatlar kullanılır
    lm_use_mock_prices: bool = True
    epias_base_url: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
