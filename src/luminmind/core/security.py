"""Parola hash'leme (stdlib scrypt) ve üretici kimlik bilgisi şifreleme (Fernet).

Üretici API token'ları PostgreSQL `vendor_credentials.encrypted_payload` alanında
her zaman Fernet ile şifreli saklanır; anahtar `CREDENTIALS_ENC_KEY` env'den gelir
(PLAN.md §8). Parolalar için ek bağımlılık yerine stdlib `hashlib.scrypt` kullanılır.
"""

import base64
import hashlib
import hmac
import secrets

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
