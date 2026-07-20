"""Uygulama ayarları: tüm konfigürasyon tek tip-güvenli Settings sınıfında toplanır."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Genel
    lm_env: str = "dev"
    lm_log_level: str = "INFO"

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
