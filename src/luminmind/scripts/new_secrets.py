"""Üretim sırlarını üretir: `python -m luminmind.scripts.new_secrets`

Paneli internete açmadan önce `.env` dosyasındaki tüm `changeme` değerleri
gerçek rastgele sırlarla değiştirilmelidir. Bu betik kopyalanmaya hazır satırlar
basar; hiçbir dosyayı kendiliğinden değiştirmez (mevcut kurulumun anahtarını
yanlışlıkla döndürmek, şifreli üretici kimlik bilgilerini okunamaz hale getirir).

**Dikkat:** `CREDENTIALS_ENC_KEY` sonradan değiştirilirse `vendor_credentials`
tablosundaki şifreli kayıtlar çözülemez; üretici kimlik bilgilerini yeniden
girmeniz gerekir.
"""

import secrets

from luminmind.core.security import generate_enc_key

_ALPHABET_NOTE = "URL/YAML güvenli karakterler (kaçış sorunu çıkarmaz)"


def token(length: int = 48) -> str:
    """URL-güvenli rastgele dize (yaklaşık `length` karakter)."""
    return secrets.token_urlsafe(length)[:length]


def main() -> None:
    print("# .env içine kopyalayın — her satır tek seferlik üretildi")
    print(f"# ({_ALPHABET_NOTE})")
    print()
    print(f"JWT_SECRET={token(48)}")
    print(f"POSTGRES_PASSWORD={token(32)}")
    print(f"INFLUX_TOKEN={token(48)}")
    print(f"INFLUX_PASSWORD={token(24)}")
    print(f"SEED_ADMIN_PASSWORD={token(20)}")
    print(f"CREDENTIALS_ENC_KEY={generate_enc_key()}")
    print()
    print("# CREDENTIALS_ENC_KEY'i mevcut bir kurulumda DEĞİŞTİRMEYİN:")
    print("# kayıtlı üretici kimlik bilgileri çözülemez hale gelir.")


if __name__ == "__main__":
    main()
