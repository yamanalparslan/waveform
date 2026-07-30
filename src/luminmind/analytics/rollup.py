"""Saha serilerini tesis toplamına indiren saf fonksiyonlar.

Ölçüm ve analiz saha seviyesinde yapılır (her fabrikanın kendi kapasitesi ve
modeli var), ama kullanıcı önce tesise bakar: "Tescom UPS İzmir bugün ne
üretti?". Bu modül o iki seviye arasındaki köprüdür.

Hepsi saf fonksiyon — veritabanı/Influx bilmez, bu yüzden tablo bazlı test
edilebilir. Toplama kuralları bilinçli olarak birbirinden farklı:

- **Güç ve enerji** toplanır (fiziksel olarak eklenebilir büyüklükler).
- **Tepe güç** toplanmaz, aynı zaman damgasındaki toplamın maksimumu alınır;
  sahaların ayrı ayrı tepelerini toplamak farklı saatlerdeki tepeleri
  birleştirip gerçekleşmemiş bir tesis tepesi üretir.
- **Performans oranı** ağırlıklı hesaplanır (toplam gerçek / toplam beklenen);
  sahaların PR'larının ortalaması küçük sahayı büyüğü kadar önemli sayardı.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

Series = Mapping[datetime, float]


def sum_series(series: Sequence[Series]) -> dict[datetime, float]:
    """Birden çok sahanın serisini zaman damgası bazında toplar.

    Yalnız bazı sahalarda bulunan damgalar da sonuca girer — eksik sahayı 0
    saymak, veri gecikmesini "üretim yok" gibi göstermekten daha az yanıltıcı
    değil ama alternatifi (damgayı tamamen atmak) tesis toplamında delik açar.
    Damga bazında hangi sahaların katkı verdiği `contributors` ile ölçülebilir.
    """
    totals: dict[datetime, float] = {}
    for single in series:
        for ts, value in single.items():
            totals[ts] = totals.get(ts, 0.0) + value
    return dict(sorted(totals.items()))


def contributors(series: Sequence[Series]) -> dict[datetime, int]:
    """Her zaman damgasında kaç sahanın veri verdiği — eksik veriyi görmek için."""
    counts: dict[datetime, int] = {}
    for single in series:
        for ts in single:
            counts[ts] = counts.get(ts, 0) + 1
    return dict(sorted(counts.items()))


def energy_kwh(series: Series, interval_hours: float = 0.25) -> float:
    """Güç serisinden enerji (kWh). 15 dk aralık → 0,25 saat.

    **Yalnız sayaç okunamadığında kullanılır.** Güç eğrisini integre etmek,
    çekilemeyen her pencereyi "üretim olmamış" saymak demektir: ingestion 48
    dakika duraksadığında (30.07.2026 sabahı böyle oldu) o süredeki enerji
    sessizce kayboluyor ve panel invertörün kendi sayacından düşük gösteriyordu.
    Sayaç varken `counter_energy_kwh` tercih edilir.
    """
    return sum(series.values()) * interval_hours


def counter_energy_kwh(
    readings: Sequence[tuple[datetime, float]], confirm_samples: int = 2
) -> float:
    """Enerji sayacı okumalarından pencere içi üretim (kWh).

    Yalnız **pozitif artışlar** toplanır. Bunun iki faydası var:

    * **Veri boşluğuna dayanıklı.** Sayaç kümülatif olduğu için iki okuma
      arasındaki fark, aradaki çekilemeyen dakikaların üretimini de taşır.
      Güç integralinin aksine boşluk enerjiyi silmez.
    * **Sıfırlanmaya dayanıklı.** Gün dönümünde sıfırlanan günlük sayaç da
      (Tescom `gunluk_uretim_kwh`), hiç sıfırlanmayan ömürlük sayaç da (SMA
      `totalYield`) aynı fonksiyonla doğru sonucu verir.

    **Arızalı okuma ile gerçek sıfırlanma ayrılır.** Tescom invertörü anlık
    olarak ulaşılamadığında sayacı 0 bildirip bir sonraki çevrimde gerçek
    değerine dönüyor (29.07.2026'da tek cihazda beş kez). Bu düşüşler ham
    hâliyle sıfırlanma sanılırsa dönüşteki tırmanış yeni üretim sayılır ve gün
    toplamı katlanır — o gün 1.085 kWh yerine 6.164 kWh çıkmıştı. Bu yüzden bir
    düşüş, ancak sonraki `confirm_samples` okuma da düşük kalırsa sıfırlanma
    kabul edilir; hemen eski seviyeye dönen tekil düşüş yok sayılır ve taban
    korunur.

    Sınır: pencerenin başında okuma yoksa (ör. gece hiç veri çekilememişse)
    günlük sayacın o ana kadarki değeri görülemez ve sonuç eksik kalır. Bu
    durumda güç integrali de aynı miktarda eksiktir — yani daha kötüsü değil.
    """
    values = [value for _, value in sorted(readings)]
    total = 0.0
    baseline: float | None = None
    for index, value in enumerate(values):
        if baseline is None:
            baseline = value
        elif value >= baseline:
            total += value - baseline
            baseline = value
        elif any(
            later >= baseline
            for later in values[index + 1 : index + 1 + confirm_samples]
        ):
            continue  # arızalı okuma: seviye geri geliyor, taban korunur
        else:
            baseline = value  # doğrulanmış sıfırlanma: yeni tabandan devam
    return total


def peak_kw(series: Series) -> float:
    """Serinin tepe gücü; boş seride 0."""
    return max(series.values(), default=0.0)


def performance_ratio(actual_kwh: float, expected_kwh: float, min_expected: float = 1.0) -> float:
    """Ağırlıklı performans oranı (%). Beklenen anlamsızsa 0 döner."""
    if expected_kwh < min_expected:
        return 0.0
    return actual_kwh / expected_kwh * 100.0


@dataclass(frozen=True)
class SiteRollup:
    """Tek sahanın gün özeti."""

    series_key: str
    name: str
    capacity_kwp: float | None
    actual_kwh: float
    expected_kwh: float
    peak_kw: float
    last_power_kw: float
    open_anomalies: int = 0

    @property
    def pr_pct(self) -> float:
        return performance_ratio(self.actual_kwh, self.expected_kwh)


@dataclass(frozen=True)
class PlantRollup:
    """Sahaların tesis düzeyinde toplamı."""

    sites: list[SiteRollup]
    actual_kwh: float
    expected_kwh: float
    peak_kw: float
    last_power_kw: float
    capacity_kwp: float
    open_anomalies: int

    @property
    def pr_pct(self) -> float:
        return performance_ratio(self.actual_kwh, self.expected_kwh)

    @property
    def site_count(self) -> int:
        return len(self.sites)


def roll_up(
    sites: Sequence[SiteRollup],
    actual_by_site: Mapping[str, Series] | None = None,
) -> PlantRollup:
    """Saha özetlerini tesis özetine indirger.

    `actual_by_site` verilirse tesis tepesi **eş zamanlı** toplamdan hesaplanır;
    verilmezse sahaların tepeleri toplanır (üst sınır, gerçekte oluşmamış
    olabilir). Doğru sayı istendiğinde seriyi geçmek gerekir.
    """
    if actual_by_site:
        combined = sum_series(list(actual_by_site.values()))
        plant_peak = peak_kw(combined)
    else:
        plant_peak = sum(s.peak_kw for s in sites)

    return PlantRollup(
        sites=list(sites),
        actual_kwh=round(sum(s.actual_kwh for s in sites), 3),
        expected_kwh=round(sum(s.expected_kwh for s in sites), 3),
        peak_kw=round(plant_peak, 3),
        last_power_kw=round(sum(s.last_power_kw for s in sites), 3),
        capacity_kwp=round(sum(s.capacity_kwp or 0.0 for s in sites), 2),
        open_anomalies=sum(s.open_anomalies for s in sites),
    )
