"""Üretici adaptörlerinin ortak arayüzü.

Her üretici (Huawei, SMA, …) bu ABC'yi implemente eder; platformun geri kalanı
yalnızca bu arayüzü tanır. Yeni bir üretici eklemek = yeni bir alt sınıf + config.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from types import TracebackType
from typing import ClassVar, Self

import httpx

from luminmind.core.schemas import DeviceMeta, PlantMeta, TelemetryPoint, Vendor


class AdapterError(Exception):
    """Adaptör seviyesinde kalıcı hata (retry'lar tükendi, beklenmeyen yanıt)."""


class AdapterAuthError(AdapterError):
    """Kimlik doğrulama başarısız (geçersiz kimlik bilgisi, yenilenemeyen token)."""


class VendorAdapter(ABC):
    """Bir üreticinin bulut API'sine asenkron erişim.

    Kullanım::

        async with HuaweiAdapter(base_url=..., username=..., system_code=...) as adapter:
            plants = await adapter.fetch_plants()
            points = await adapter.fetch_telemetry(plants[0].vendor_plant_id, since=...)

    Alt sınıflar kimlik doğrulamayı (token/oturum yaşam döngüsü dahil) kendileri
    yönetir; çağıran taraf `authenticate()`'i açıkça çağırmak zorunda değildir.
    """

    vendor: ClassVar[Vendor]

    def __init__(self, base_url: str, timeout_s: float = 30.0) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout_s)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._client.aclose()

    @abstractmethod
    async def authenticate(self) -> None:
        """Oturum aç / token al. Idempotent olmalı; gerekirse yeniler."""

    @abstractmethod
    async def fetch_plants(self) -> list[PlantMeta]:
        """Hesaba bağlı tüm tesislerin meta verisini döndürür."""

    @abstractmethod
    async def fetch_devices(self, vendor_plant_id: str) -> list[DeviceMeta]:
        """Bir tesisteki cihazları (invertörleri) döndürür."""

    @abstractmethod
    async def fetch_telemetry(
        self, vendor_plant_id: str, since: datetime
    ) -> list[TelemetryPoint]:
        """`since` sonrası ölçümleri kanonik modele normalize edilmiş halde döndürür."""
