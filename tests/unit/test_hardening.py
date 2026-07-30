"""Dışa açık çalıştırma sertleştirmesi: sır denetimi, çerez bayrağı, hız sınırı."""

import pytest

from luminmind.config import Settings
from luminmind.core.hardening import (
    LoginRateLimiter,
    build_csp,
    check_production_settings,
    client_ip,
    enforce_production_settings,
)

SAFE = {
    "jwt_secret": "x" * 48,
    "influx_token": "y" * 48,
    "credentials_enc_key": "z" * 44,
}


class FakeRequest:
    """Sadece başlık ve istemci bilgisi taşıyan minimal istek nesnesi."""

    def __init__(self, headers: dict[str, str], host: str | None = "10.0.0.1"):
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.client = type("C", (), {"host": host})() if host else None


# ------------------------------ Sır denetimi ------------------------------


def test_default_secrets_are_reported():
    problems = check_production_settings(Settings())
    joined = " ".join(problems)
    assert "JWT_SECRET" in joined
    assert "CREDENTIALS_ENC_KEY" in joined


def test_clean_settings_pass():
    assert check_production_settings(Settings(**SAFE)) == []


def test_short_jwt_secret_is_rejected():
    problems = check_production_settings(Settings(**{**SAFE, "jwt_secret": "kisa"}))
    assert any("en az" in p for p in problems)


def test_public_url_must_be_https():
    problems = check_production_settings(
        Settings(**SAFE, lm_public_url="http://ges.example.com")
    )
    assert any("https" in p for p in problems)


def test_dev_env_never_blocks_startup():
    # Geliştirme akışı bozulmamalı: varsayılan sırlarla dev modda çalışılabilir
    enforce_production_settings(Settings(lm_env="dev"))


def test_production_refuses_insecure_secrets():
    with pytest.raises(RuntimeError, match="güvensiz yapılandırma"):
        enforce_production_settings(Settings(lm_env="prod"))


def test_production_starts_with_clean_secrets():
    enforce_production_settings(
        Settings(lm_env="prod", lm_public_url="https://ges.example.com", **SAFE)
    )


# ------------------------------ Çerez bayrağı ------------------------------


def test_cookie_secure_derives_from_public_url():
    assert Settings().cookie_secure is False  # yerel HTTP geliştirme
    assert Settings(lm_public_url="https://ges.example.com").cookie_secure is True


def test_cookie_secure_can_be_overridden():
    forced = Settings(lm_public_url="https://ges.example.com", lm_cookie_secure=False)
    assert forced.cookie_secure is False
    # ...ama üretim denetimi bunu yakalar
    assert any("LM_COOKIE_SECURE" in p for p in check_production_settings(forced))


def test_allowed_hosts_include_public_hostname():
    settings = Settings(lm_public_url="https://ges.example.com", lm_allowed_hosts="localhost")
    assert set(settings.allowed_hosts) == {"localhost", "ges.example.com"}


def test_allowed_hosts_empty_by_default():
    assert Settings().allowed_hosts == []


# ------------------------------ İstemci IP ------------------------------


def test_cloudflare_header_wins():
    request = FakeRequest({"CF-Connecting-IP": "203.0.113.9", "X-Forwarded-For": "1.2.3.4"})
    assert client_ip(request) == "203.0.113.9"  # type: ignore[arg-type]


def test_forwarded_for_takes_first_address():
    request = FakeRequest({"X-Forwarded-For": "203.0.113.9, 70.41.3.18"})
    assert client_ip(request) == "203.0.113.9"  # type: ignore[arg-type]


def test_direct_connection_uses_socket_peer():
    assert client_ip(FakeRequest({}, host="192.168.1.5")) == "192.168.1.5"  # type: ignore[arg-type]


def test_missing_client_is_not_fatal():
    assert client_ip(FakeRequest({}, host=None)) == "unknown"  # type: ignore[arg-type]


# ------------------------------ Giriş hız sınırı ------------------------------


def test_limiter_blocks_after_max_attempts():
    limiter = LoginRateLimiter(max_attempts=3, window_s=900.0)
    for _ in range(3):
        assert not limiter.is_blocked("ip")
        limiter.register_failure("ip")
    assert limiter.is_blocked("ip")


def test_limiter_is_per_source():
    limiter = LoginRateLimiter(max_attempts=2)
    limiter.register_failure("a")
    limiter.register_failure("a")
    assert limiter.is_blocked("a")
    assert not limiter.is_blocked("b")


def test_successful_login_resets_counter():
    limiter = LoginRateLimiter(max_attempts=2)
    limiter.register_failure("ip")
    limiter.reset("ip")
    limiter.register_failure("ip")
    assert not limiter.is_blocked("ip")


def test_window_expires_old_attempts():
    limiter = LoginRateLimiter(max_attempts=2, window_s=60.0)
    limiter.register_failure("ip", now=0.0)
    limiter.register_failure("ip", now=10.0)
    assert limiter.is_blocked("ip", now=30.0)
    assert not limiter.is_blocked("ip", now=100.0)  # pencere kaydı


def test_retry_after_counts_down():
    limiter = LoginRateLimiter(max_attempts=1, window_s=600.0)
    limiter.register_failure("ip", now=0.0)
    assert limiter.retry_after_s("ip", now=100.0) == pytest.approx(500, abs=1)
    assert limiter.retry_after_s("bos", now=100.0) == 0


# ------------------------------ CSP ------------------------------


def test_csp_allows_the_resources_the_map_actually_uses():
    """Politika daralırsa harita sessizce boş açılır — regresyon kilidi."""
    csp = build_csp()
    assert "https://unpkg.com" in csp  # Leaflet JS + CSS
    assert "https://*.basemaps.cartocdn.com" in csp  # koyu tema döşemeleri
    assert "https://*.tile.openstreetmap.org" in csp  # uydu/sokak döşemeleri


def test_csp_keeps_the_hard_restrictions():
    csp = build_csp()
    assert "frame-ancestors 'none'" in csp  # clickjacking
    assert "object-src 'none'" in csp
    assert "form-action 'self'" in csp  # form kaçırma
    assert "base-uri 'self'" in csp
    assert "default-src 'self'" in csp


def test_csp_template_sources_match_reality():
    """Şablonlarda geçen dış origin'ler allowlist'te olmalı."""
    import re
    from pathlib import Path

    templates = Path("src/luminmind/web/templates")
    origins = set()
    for path in templates.glob("*.html"):
        for match in re.findall(r"https://([a-z0-9.*{}-]+)/", path.read_text(encoding="utf-8")):
            if "w3.org" in match:
                continue
            origins.add(match.replace("{s}", "*"))

    csp = build_csp()
    for origin in origins:
        assert origin in csp, f"{origin} CSP'de yok — o sayfa tarayıcıda kırılır"
