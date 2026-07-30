"""Dinamik kirlilik (soiling) modeli.

Kirlilik sabit bir kayıp değildir: kuru günlerde birikir, yeterli yağışta
sıfırlanır. Sabit %2 varsayımı yazın Konya/İzmir gibi kurak bölgelerde kaybı
belirgin şekilde eksik tahmin eder ve bu eksik, dijital ikizin sapmasında
"kirlilik anomalisi" olarak görünür — yani model kendi eksiğini arıza sanar.

Kimber (2006) modeli kullanılır: birim zamanda sabit oranda birikim, eşiği
aşan yağışta temizlenme, bir tavan değerinde doyma. `pvlib.soiling.kimber`
kayıp oranı döndürür; buradaki fonksiyonlar **geçirgenlik oranı** (1 − kayıp)
döndürür çünkü zincire çarpımsal girer.

`base_ratio` kalıcı bir kirlilik tabanı için ayrılmıştır (örn. hiç yıkanmayan
tesis). Varsayılan 1,0'dır ve **kalibrasyon tarafından doldurulmaz**: kalıcı
sistematik sapma `twin/calibration.py` içindeki `scale` katsayısında toplanır.
İkisini birden öğrenmek aynı bilgiyi iki kez uygulamak olurdu.
"""

from dataclasses import dataclass

import pandas as pd
from pvlib.soiling import kimber


@dataclass(frozen=True)
class SoilingConfig:
    """Kirlilik birikim/temizlenme parametreleri."""

    daily_loss_rate: float = 0.0015  # gün başına birikim (Kimber varsayılanı)
    cleaning_threshold_mm: float = 6.0  # 24 saatte bu yağış temizler
    grace_period_days: int = 14  # yağış sonrası yeniden birikim gecikmesi
    max_loss: float = 0.20  # doyma seviyesi
    base_ratio: float = 1.0  # kalibrasyondan gelen kalıcı taban (≤1)

    def __post_init__(self) -> None:
        if not 0.0 < self.base_ratio <= 1.0:
            raise ValueError(f"base_ratio must be in (0, 1], got {self.base_ratio}")
        if not 0.0 <= self.max_loss < 1.0:
            raise ValueError(f"max_loss must be in [0, 1), got {self.max_loss}")


def soiling_ratio(
    precipitation_mm: pd.Series, config: SoilingConfig | None = None
) -> pd.Series:
    """Yağış serisinden zamanla değişen geçirgenlik oranı (0–1) üretir.

    Seri boşsa veya yağış verisi hiç yoksa sabit `base_ratio` döndürülür —
    yani model, veri yokluğunda kalibre edilmiş tabana geri düşer.
    """
    config = config or SoilingConfig()
    if precipitation_mm.empty:
        return precipitation_mm.astype(float)

    raw = pd.to_numeric(precipitation_mm, errors="coerce")
    if raw.isna().all():
        # Yağış *verisi* yok (sıfır yağış değil) → yalnızca kalibre edilmiş taban.
        # Bu ayrım önemli: kuru hava bilgidir, veri yokluğu bilgi değildir.
        return pd.Series(config.base_ratio, index=precipitation_mm.index, dtype=float)

    rain = raw.fillna(0.0).astype(float).clip(lower=0.0)
    loss = kimber(
        rain,
        cleaning_threshold=config.cleaning_threshold_mm,
        soiling_loss_rate=config.daily_loss_rate,
        grace_period=config.grace_period_days,
        max_soiling=config.max_loss,
    )
    ratio = (1.0 - loss.clip(lower=0.0, upper=config.max_loss)) * config.base_ratio
    ratio.name = "soiling_ratio"
    return ratio.astype(float)


def constant_ratio(index: pd.Index, static_loss: float) -> pd.Series:
    """Yağış verisi olmayan tesisler için sabit oran serisi."""
    return pd.Series(1.0 - static_loss, index=index, dtype=float, name="soiling_ratio")
