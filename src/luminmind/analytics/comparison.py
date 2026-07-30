"""Beklenen (dijital ikiz) ile gerçek üretimin hizalanması ve sapma serisi.

Gece ve düşük ışınım noktaları filtrelenir: beklenen üretim eşiğin altındayken
yüzdesel sapma anlamsızdır (payda ~0). Sapma işareti: negatif = düşük performans.

**Eşik kapasiteye oranlıdır.** Önceki sürümde sabit 10 kW kullanılıyordu; bu
100 kWp'lik bir tesiste kapasitenin %10'u (aşırı agresif filtre, günün büyük
kısmı elenir), 5 MW'lık bir tesiste %0,2'si (filtre yok sayılır, şafak/alacakaranlık
gürültüsü anomali üretir) demekti. Aynı kod farklı ölçekteki tesislerde farklı
davranıyordu.

**Belirsizlik bandı.** Ensemble çalıştırmasında her nokta bir P10–P90 bandıyla
gelir. Gerçek üretim bandın içindeyse sapma hava tahmini belirsizliğiyle
açıklanır ve arıza kanıtı sayılmaz; `excess_deviation_pct` bandın *dışına*
taşan kısmı ölçer. Sınıflandırıcılar bu büyüklüğü kullanır.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from luminmind.core.aggregate import RawSample

# Analiz ızgarası: dijital ikiz bu adımda üretir, gerçek ölçümler buna oturtulur.
SLOT_MINUTES = 15

# Beklenen üretim, kapasitenin bu oranının altındayken sapma raporlanmaz.
DEFAULT_MIN_EXPECTED_FRACTION = 0.05
# Kapasite bilinmiyorsa geriye düşülecek mutlak eşik (kW).
FALLBACK_MIN_EXPECTED_KW = 10.0


@dataclass(frozen=True)
class DeviationSample:
    ts: datetime
    actual_kw: float
    expected_kw: float
    expected_p10_kw: float | None = None
    expected_p90_kw: float | None = None

    @property
    def deviation_pct(self) -> float:
        """(gerçek - beklenen) / beklenen × 100; negatif = eksik üretim."""
        return (self.actual_kw - self.expected_kw) / self.expected_kw * 100.0

    @property
    def within_band(self) -> bool:
        """Gerçek üretim tahmin belirsizliği bandının içinde mi?"""
        if self.expected_p10_kw is None or self.expected_p90_kw is None:
            return False
        return self.expected_p10_kw <= self.actual_kw <= self.expected_p90_kw

    @property
    def excess_deviation_pct(self) -> float:
        """Belirsizlik bandının dışına taşan sapma (%).

        Band yoksa ham sapmaya eşittir. Band varsa yalnızca bandın kenarını
        aşan kısım raporlanır — bandın içindeki fark hava belirsizliğidir,
        santralin suçu değil.
        """
        if self.expected_p10_kw is None or self.expected_p90_kw is None:
            return self.deviation_pct
        if self.actual_kw < self.expected_p10_kw:
            return (self.actual_kw - self.expected_p10_kw) / self.expected_kw * 100.0
        if self.actual_kw > self.expected_p90_kw:
            return (self.actual_kw - self.expected_p90_kw) / self.expected_kw * 100.0
        return 0.0


def floor_to_grid(ts: datetime, minutes: int = SLOT_MINUTES) -> datetime:
    """Zaman damgasını analiz ızgarasının başına yuvarlar (aşağı)."""
    discard = (ts.minute % minutes) * 60 + ts.second
    return (ts - timedelta(seconds=discard)).replace(microsecond=0)


def plant_actual_from_samples(
    samples: list[RawSample], grid_minutes: int = SLOT_MINUTES
) -> dict[str, dict[datetime, float]]:
    """Cihaz bazlı ham örneklerden ızgaraya oturtulmuş tesis toplamı AC güç serisi.

    **Izgaraya oturtmak zorunlu.** Üretici telemetrisi rastgele anlarda gelir
    (ör. `06:59:03`), dijital ikiz ise 15 dakikalık ızgarada üretir
    (`07:00:00`). Hizalama tam zaman damgası kesişimiyle yapıldığı için ham
    damgalarla **hiçbir nokta eşleşmez**: sapma serisi boş kalır, anomali
    motoru sonsuza kadar "sağlıklı" der, doğruluk skoru hep "yeterli veri yok"
    döner. Hatanın sessiz olması onu daha da tehlikeli kılıyor.

    Aynı cihazın bir ızgara aralığındaki birden çok örneği **ortalanır** (anlık
    güç ölçümünün aralık ortalaması); ardından cihazlar toplanır. Toplamak
    yanlış olurdu — 5 dakikalık çekimde her aralıkta ~3 örnek var ve toplam
    gücü üçe katlardı.
    """
    per_device: dict[str, dict[datetime, dict[str, list[float]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )
    for sample in samples:
        power = sample.fields.get("ac_power_kw")
        if power is None:
            continue
        slot = floor_to_grid(sample.ts, grid_minutes)
        per_device[sample.plant_id][slot][sample.inverter_id].append(power)

    totals: dict[str, dict[datetime, float]] = {}
    for plant_id, slots in per_device.items():
        series: dict[datetime, float] = {}
        for slot, devices in slots.items():
            series[slot] = sum(sum(values) / len(values) for values in devices.values())
        totals[plant_id] = dict(sorted(series.items()))
    return totals


def min_expected_kw(
    capacity_kw: float | None, fraction: float = DEFAULT_MIN_EXPECTED_FRACTION
) -> float:
    """Kapasiteye oranlı analiz tabanı; kapasite yoksa sabit değere düşer."""
    if capacity_kw is None or capacity_kw <= 0.0:
        return FALLBACK_MIN_EXPECTED_KW
    return capacity_kw * fraction


def build_deviation_series(
    actual: dict[datetime, float],
    expected: dict[datetime, float],
    min_expected_kw_threshold: float = FALLBACK_MIN_EXPECTED_KW,
    band: dict[datetime, tuple[float, float]] | None = None,
) -> list[DeviationSample]:
    """İki seriyi zaman damgası üzerinden hizalar; düşük ışınım noktalarını eler."""
    bands = band or {}
    samples: list[DeviationSample] = []
    for ts in sorted(actual.keys() & expected.keys()):
        if expected[ts] < min_expected_kw_threshold:
            continue
        low_high = bands.get(ts)
        samples.append(
            DeviationSample(
                ts=ts,
                actual_kw=actual[ts],
                expected_kw=expected[ts],
                expected_p10_kw=low_high[0] if low_high else None,
                expected_p90_kw=low_high[1] if low_high else None,
            )
        )
    return samples
