"""Tek hat şeması bileşenlerinin kayıp zinciri.

pvlib modül+invertör modelinin kapsamadığı, sayaç noktasına kadarki kayıplar
iki gruba ayrılır:

- **DC tarafı** (invertör girişinden önce): kirlilik, modül uyumsuzluğu (mismatch),
  DC kablolama, konnektörler, ışık kaynaklı bozunum (LID), etiket toleransı ve
  yaşa bağlı bozunum. Bunlar DC gücü doğrudan ölçekler; invertör kırpma
  eşiğinden **önce** uygulanmalıdır — aksi halde kırpma seviyesi yanlış çıkar.
- **AC tarafı** (invertör çıkışından sayaca): AC kablolama, OG trafo, saha
  erişilebilirliği (planlı bakım/kesinti beklentisi).

Kirlilik statik bir taban değerdir; gerçek kirlilik zamanla değişir ve yağışla
sıfırlanır. Dinamik kirlilik oranı `twin/soiling.py` tarafından üretilir ve
zincirdeki statik `soiling` terimini geçersiz kılar.

Varsayılanlar NREL PVWatts v6 tipik saha değerleridir; tesis bazında
kalibrasyonla (`twin/calibration.py`) revize edilir.
"""

from dataclasses import dataclass

import pandas as pd


def _factor(*losses: float) -> float:
    """Bağımsız kayıpları çarpımsal olarak birleştirir."""
    factor = 1.0
    for loss in losses:
        factor *= 1.0 - loss
    return factor


@dataclass(frozen=True)
class LossChain:
    # --- DC tarafı (invertör kırpmasından önce) ---
    # Statik kirlilik tabanı (dinamik model yoksa). Türkiye'de endüstriyel
    # çatılarda %4 gerçekçi; PVWatts varsayılanı %2 buralar için iyimser.
    soiling: float = 0.04
    mismatch: float = 0.02  # modül/string uyumsuzluğu
    dc_wiring: float = 0.02  # DC kablo omik kaybı
    connections: float = 0.005  # konnektör/klemens
    light_induced_degradation: float = 0.015  # LID (ilk saatlerde kalıcı)
    nameplate: float = 0.01  # etiket toleransı
    age_degradation: float = 0.0  # yaş × yıllık bozunum (0,5 %/yıl tipik)

    # --- AC tarafı (invertörden sayaca) ---
    ac_wiring: float = 0.01  # AC kablolama
    transformer: float = 0.015  # OG trafo (sayaç OG tarafında)
    availability: float = 0.015  # planlı kesinti/bakım/şebeke gidişi beklentisi (%1.5)
    other: float = 0.0  # ayarlanabilir ek kayıp

    @property
    def dc_factor(self) -> float:
        """İnvertör girişine kadar olan çarpımsal kalan oran."""
        return _factor(
            self.soiling,
            self.mismatch,
            self.dc_wiring,
            self.connections,
            self.light_induced_degradation,
            self.nameplate,
            self.age_degradation,
        )

    @property
    def dc_factor_without_soiling(self) -> float:
        """Dinamik kirlilik serisi kullanıldığında statik terim devre dışı kalır."""
        return self.dc_factor / (1.0 - self.soiling)

    @property
    def ac_factor(self) -> float:
        """İnvertör çıkışından sayaca kadar kalan oran."""
        return _factor(self.ac_wiring, self.transformer, self.availability, self.other)

    @property
    def net_factor(self) -> float:
        """Uçtan uca toplam kalan oran (raporlama/karşılaştırma için)."""
        return self.dc_factor * self.ac_factor

    def apply_ac(self, ac_power: pd.Series) -> pd.Series:
        """İnvertör AC çıkışını sayaç noktasındaki net güce indirger."""
        return ac_power * self.ac_factor

    def with_age(self, years: float, annual_rate: float = 0.005) -> "LossChain":
        """Tesis yaşına göre bozunum terimini doldurulmuş yeni bir zincir döndürür."""
        degradation = max(0.0, min(0.5, years * annual_rate))
        return LossChain(
            soiling=self.soiling,
            mismatch=self.mismatch,
            dc_wiring=self.dc_wiring,
            connections=self.connections,
            light_induced_degradation=self.light_induced_degradation,
            nameplate=self.nameplate,
            age_degradation=degradation,
            ac_wiring=self.ac_wiring,
            transformer=self.transformer,
            availability=self.availability,
            other=self.other,
        )
