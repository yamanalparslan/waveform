"""Parola hash'leme (stdlib scrypt) ve üretici kimlik bilgisi şifreleme (Fernet).

Üretici API token'ları PostgreSQL `vendor_credentials.encrypted_payload` alanında
her zaman Fernet ile şifreli saklanır; anahtar `CREDENTIALS_ENC_KEY` env'den gelir
(PLAN.md §8). Parolalar için ek bağımlılık yerine stdlib `hashlib.scrypt` kullanılır.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from cryptography.fernet import Fernet

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt_hex), n=int(n), r=int(r), p=int(p)
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


class TokenError(ValueError):
    """Geçersiz, süresi dolmuş veya yanlış tipte JWT."""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def create_jwt(claims: dict[str, Any], secret: str, ttl_s: int, token_type: str) -> str:
    """HS256 JWT üretir (stdlib — ek bağımlılık yok). `type` claim'i access/refresh ayırır."""
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    now = int(time.time())
    payload = _b64url(
        json.dumps(
            {**claims, "type": token_type, "iat": now, "exp": now + ttl_s},
            separators=(",", ":"),
        ).encode()
    )
    signing_input = f"{header}.{payload}"
    signature = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(signature)}"


def decode_jwt(token: str, secret: str, expected_type: str) -> dict[str, Any]:
    """İmza + süre + tip doğrulaması yapar; geçersizse TokenError fırlatır."""
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
    except ValueError as exc:
        raise TokenError("malformed token") from exc
    signing_input = f"{header_b64}.{payload_b64}"
    expected_sig = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(expected_sig, _b64url_decode(signature_b64)):
        raise TokenError("invalid signature")
    try:
        claims: dict[str, Any] = json.loads(_b64url_decode(payload_b64))
    except (ValueError, UnicodeDecodeError) as exc:
        raise TokenError("malformed payload") from exc
    if int(claims.get("exp", 0)) < int(time.time()):
        raise TokenError("token expired")
    if claims.get("type") != expected_type:
        raise TokenError(f"expected {expected_type} token")
    return claims


def generate_enc_key() -> str:
    """Yeni bir Fernet anahtarı üretir (CREDENTIALS_ENC_KEY için)."""
    return Fernet.generate_key().decode()


def _fernet(key: str) -> Fernet:
    # Ham 32-byte olmayan anahtarları da kabul et: SHA-256 ile normalize edilir.
    try:
        return Fernet(key.encode())
    except ValueError:
        derived = base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest())
        return Fernet(derived)


def encrypt_payload(plaintext: str, key: str) -> bytes:
    return _fernet(key).encrypt(plaintext.encode())


def decrypt_payload(token: bytes, key: str) -> str:
    return _fernet(key).decrypt(token).decode()
