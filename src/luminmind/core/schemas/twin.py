"""Dijital ikiz çıktısının kanonik modeli (Influx `twin_expected` ile eşleşir)."""

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

# v1: pvlib ModelChain + PVWatts, kırpmasız, sabit kayıp.
# v2: açık zincir — invertör kırpması, sıra-arası gölgelenme, bileşen bazlı IAM,
#     spektral düzeltme, dinamik kirlilik, aralık-ortası güneş geometrisi.
TWIN_MODEL_VERSION = "physical-v2"


class TwinPoint(BaseModel):
    """Bir tesisin bir zaman damgası için beklenen (teorik) üretimi.

    `expected_ac_kw` merkezi tahmindir (ensemble varsa medyan). `p10`/`p90`
    ensemble yayılımından gelen belirsizlik bandıdır; tek modelli çalışmada
    boştur. Anomali tespiti bandı kullanır: banda düşen sapma arıza değildir,
    hava tahmini belirsizliğidir.
    """

    model_config = ConfigDict(frozen=True)

    plant_id: str
    ts: datetime
    expected_ac_kw: float
    expected_ac_kw_p10: float | None = None
    expected_ac_kw_p90: float | None = None
    poa_irradiance_wm2: float | None = None
    cell_temp_c: float | None = None
    clipping_loss_kw: float | None = None
    soiling_ratio: float | None = None
    # Tahmin ufku (gün): 0 = bugün (nowcast), 1 = yarın, 2 = öbür gün
    horizon_days: int = 0
    model_version: str = TWIN_MODEL_VERSION

    @field_validator("ts")
    @classmethod
    def _ts_must_be_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _band_must_bracket_center(self) -> "TwinPoint":
        low, high = self.expected_ac_kw_p10, self.expected_ac_kw_p90
        if low is not None and high is not None and low > high:
            raise ValueError("p10 must not exceed p90")
        return self

    @property
    def uncertainty_kw(self) -> float | None:
        """Yarı bant genişliği (kW); ensemble yoksa None."""
        if self.expected_ac_kw_p10 is None or self.expected_ac_kw_p90 is None:
            return None
        return (self.expected_ac_kw_p90 - self.expected_ac_kw_p10) / 2.0
