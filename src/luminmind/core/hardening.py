"""Dışa açık çalıştırma için güvenlik sertleştirmesi.

Panel yerel ağda çalışırken zararsız olan varsayılanlar, internete açıldığı anda
doğrudan güvenlik açığına dönüşür: `changeme` JWT anahtarı token üretmeyi
herkese açar, `admin/admin` seed parolası paneli herkese açar, `secure`
bayrağı olmayan çerez oturumu ağı dinleyene verir.

Bu modül üç şey yapar:

1. **Başlangıç denetimi** (`check_production_settings`) — `LM_ENV` dev değilken
   varsayılan sırlarla açılmayı reddeder. Sessizce güvensiz çalışmak, hiç
   çalışmamaktan kötüdür.
2. **Güvenlik başlıkları** (`SecurityHeadersMiddleware`) — HSTS, çerçeveleme
   ve MIME sniffing korumaları, dar bir CSP.
3. **Giriş hız sınırı** (`LoginRateLimiter`) — parola deneme saldırısını
   yavaşlatır. Tek süreçli dağıtım için bellek içi kayan pencere yeterlidir;
   çok süreçli dağıtımda Redis tabanlı bir sayaca taşınmalıdır.
"""

import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from luminmind.config import Settings

# Depoda örnek olarak duran, üretimde asla kalmaması gereken değerler.
INSECURE_DEFAULTS = {
    "jwt_secret": "changeme-dev-secret",
    "influx_token": "changeme",
    "credentials_enc_key": "changeme",
}

_MIN_SECRET_LENGTH = 24


def check_production_settings(settings: Settings) -> list[str]:
    """Üretim için engelleyici sorunları listeler (boş liste = temiz)."""
    problems: list[str] = []
    for field_name, insecure in INSECURE_DEFAULTS.items():
        value = str(getattr(settings, field_name, "") or "")
        if not value:
            problems.append(f"{field_name.upper()} boş — üretimde zorunlu")
        elif value == insecure:
            problems.append(f"{field_name.upper()} hâlâ varsayılan ('{insecure}') değerinde")

    if len(settings.jwt_secret) < _MIN_SECRET_LENGTH:
        problems.append(
            f"JWT_SECRET en az {_MIN_SECRET_LENGTH} karakter olmalı "
            f"(şu an {len(settings.jwt_secret)})"
        )
    if settings.lm_public_url and not settings.lm_public_url.startswith("https://"):
        problems.append(
            "LM_PUBLIC_URL https:// ile başlamalı — oturum çerezi TLS olmadan taşınamaz"
        )
    if settings.is_public and not settings.cookie_secure:
        problems.append(
            "LM_COOKIE_SECURE=false — genel erişimde oturum çerezi korumasız kalır"
        )
    return problems


def enforce_production_settings(settings: Settings) -> None:
    """Üretim modunda güvensiz konfigürasyonla açılmayı engeller."""
    if settings.lm_env.lower() in {"dev", "development", "test"}:
        return
    problems = check_production_settings(settings)
    if problems:
        listed = "\n  - ".join(problems)
        raise RuntimeError(
            "LuminMind güvensiz yapılandırmayla başlatılamaz "
            f"(LM_ENV={settings.lm_env}):\n  - {listed}\n"
            "Düzeltip yeniden başlatın veya geliştirme için LM_ENV=dev kullanın."
        )


def client_ip(request: Request) -> str:
    """Ters vekil/tünel arkasındaki gerçek istemci IP'si.

    Cloudflare Tunnel `CF-Connecting-IP` başlığını kendisi doldurur ve dışarıdan
    gelen aynı adlı başlığı ezer; bu yüzden güvenilirdir. Doğrudan yayında
    `X-Forwarded-For`'un ilk adresine düşülür.
    """
    cloudflare = request.headers.get("cf-connecting-ip")
    if cloudflare:
        return cloudflare.strip()
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _clock(now: float | None) -> float:
    """`now or time.monotonic()` yazmak yanlış: 0.0 geçerli bir andır ama
    falsy olduğu için sessizce gerçek saate düşer."""
    return time.monotonic() if now is None else now


@dataclass
class LoginRateLimiter:
    """IP başına kayan pencere içinde başarısız giriş denemesi sayacı."""

    max_attempts: int = 8
    window_s: float = 900.0
    _attempts: dict[str, deque[float]] = field(
        default_factory=lambda: defaultdict(deque), repr=False
    )

    def _prune(self, key: str, now: float) -> deque[float]:
        bucket = self._attempts[key]
        while bucket and now - bucket[0] > self.window_s:
            bucket.popleft()
        return bucket

    def is_blocked(self, key: str, now: float | None = None) -> bool:
        return len(self._prune(key, _clock(now))) >= self.max_attempts

    def register_failure(self, key: str, now: float | None = None) -> None:
        moment = _clock(now)
        self._prune(key, moment).append(moment)

    def reset(self, key: str) -> None:
        """Başarılı girişten sonra sayacı sıfırlar."""
        self._attempts.pop(key, None)

    def retry_after_s(self, key: str, now: float | None = None) -> int:
        moment = _clock(now)
        bucket = self._prune(key, moment)
        if not bucket:
            return 0
        return max(1, int(self.window_s - (moment - bucket[0])))


# Arayüzün gerçekten çektiği dış kaynaklar. Harita sayfaları Leaflet'i unpkg'den,
# döşemeleri CARTO/OpenStreetMap'ten alıyor; bu liste daralırsa harita sessizce
# boş açılır. Politikayı kod yerine bu iki demetten türetiyoruz ki kaynak
# eklendiğinde tek yerde güncellensin.
CSP_SCRIPT_SOURCES = ("https://unpkg.com",)
CSP_STYLE_SOURCES = ("https://unpkg.com",)
CSP_IMAGE_SOURCES = (
    "https://*.basemaps.cartocdn.com",
    "https://*.tile.openstreetmap.org",
    "https://unpkg.com",
)


def build_csp() -> str:
    """İçerik güvenliği politikası.

    `script-src` içindeki `'unsafe-inline'` istenmeyen ama şu an zorunlu:
    harita başlatma kodu `map.html` ve `plant_form.html` içinde satır içi
    duruyor. Kaldırmanın yolu bu blokları statik bir .js dosyasına taşımak (ya
    da nonce üretmek); o yapıldığında buradan tek kelime silinir. Jinja2
    otomatik kaçış açık olduğu için XSS yüzeyi yine de dar.
    """
    script = " ".join(("'self'", "'unsafe-inline'", *CSP_SCRIPT_SOURCES))
    style = " ".join(("'self'", "'unsafe-inline'", *CSP_STYLE_SOURCES))
    image = " ".join(("'self'", "data:", *CSP_IMAGE_SOURCES))
    return "; ".join(
        (
            "default-src 'self'",
            f"img-src {image}",
            f"style-src {style}",
            f"script-src {script}",
            "connect-src 'self'",
            "frame-ancestors 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            "object-src 'none'",
        )
    )


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Tarayıcı tarafı savunma başlıkları (CSP, HSTS, çerçeveleme, sniffing)."""

    CSP = build_csp()

    def __init__(self, app: Callable[..., Awaitable[None]], hsts: bool = True) -> None:
        super().__init__(app)
        self.hsts = hsts

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        headers = response.headers
        headers.setdefault("Content-Security-Policy", self.CSP)
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        if self.hsts:
            headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response
