from cryptography.fernet import Fernet

from luminmind.core.security import (
    decrypt_payload,
    encrypt_payload,
    generate_enc_key,
    hash_password,
    verify_password,
)


def test_password_hash_roundtrip():
    stored = hash_password("s3cret")
    assert stored.startswith("scrypt$")
    assert "s3cret" not in stored
    assert verify_password("s3cret", stored)
    assert not verify_password("wrong", stored)


def test_password_hash_is_salted():
    assert hash_password("same") != hash_password("same")


def test_verify_rejects_malformed_stored_value():
    assert not verify_password("x", "not-a-hash")
    assert not verify_password("x", "md5$deadbeef")


def test_fernet_roundtrip_with_generated_key():
    key = generate_enc_key()
    token = encrypt_payload('{"access_token": "abc"}', key)
    assert b"abc" not in token
    assert decrypt_payload(token, key) == '{"access_token": "abc"}'


def test_fernet_accepts_non_fernet_key_via_derivation():
    token = encrypt_payload("data", "plain-passphrase")
    assert decrypt_payload(token, "plain-passphrase") == "data"


def test_generated_key_is_valid_fernet_key():
    Fernet(generate_enc_key().encode())  # raise etmemeli
