"""Tek hat şeması bileşenlerinin kayıp zinciri.

pvlib modül+invertör modelinin kapsamadığı, sayaç noktasına kadarki kayıplar:
DC/AC kablolama, trafo ve diğer sabit kayıplar. Katsayılar tesis bazında
konfigüre edilebilir (Faz 2 `inverters.efficiency_curve` / tesis meta verisi);
varsayılanlar tipik saha değerleridir.
"""

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class LossChain:
    cable_loss: float = 0.01  # AC/DC kablolama
    transformer_loss: float = 0.015  # OG trafo (Köhler sayaç OG tarafında)
    other_loss: float = 0.0  # ayarlanabilir ek kayıp (kirlilik tabanı vb.)

    @property
    def net_factor(self) -> float:
        return (1 - self.cable_loss) * (1 - self.transformer_loss) * (1 - self.other_loss)

    def apply(self, ac_power: pd.Series) -> pd.Series:
        """İnvertör AC çıkışını sayaç noktasındaki net güce indirger."""
        return ac_power * self.net_factor
